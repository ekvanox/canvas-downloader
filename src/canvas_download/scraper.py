"""Web scraper for Canvas LMS courses.

Crawls Canvas course pages using HTTP requests (with browser-obtained cookies)
and BeautifulSoup HTML parsing to discover links.  Every HTML page within the
course is rendered to PDF via a headless Playwright browser.  Actual files
(binaries, documents) are downloaded with requests.
"""

from __future__ import annotations

import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import click
import requests
from bs4 import BeautifulSoup

from canvas_download.browser import PDFRenderer
from canvas_download.dedup import deduplicate_pdfs
from canvas_download.utils import sanitize_filename

# Course sub-paths that we should NOT crawl (not useful content)
_SKIP_SEGMENTS = frozenset({
    "grades", "settings", "users",
    "conferences", "collaborations",
    "rubrics", "outcomes", "question_banks",
    "external_tools",
})

_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm",
})

_BINARY_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".txt", ".csv", ".tsv", ".json", ".xml",
    ".py", ".java", ".c", ".cpp", ".h", ".js", ".ts",
    ".r", ".rmd", ".m", ".ipynb", ".nb",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".ico",
    ".mp3", ".wav", ".ogg", ".flac",
}) | _VIDEO_EXTENSIONS

class CanvasScraper:
    """Scrapes Canvas LMS courses via HTTP requests and HTML parsing."""

    def __init__(self, session: requests.Session, hostname: str) -> None:
        self.session = session
        self.hostname = hostname
        self.base_url = f"https://{hostname}"

    # -- Course listing -----------------------------------------------------

    def list_courses(self) -> list[dict]:
        """Fetch ``/courses`` and parse for course links."""
        resp = self.session.get(f"{self.base_url}/courses", timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        courses: dict[int, dict] = {}

        for a in soup.find_all("a", href=True):
            href = str(a["href"]).rstrip("/")
            m = re.match(r"(?:https?://[^/]+)?/courses/(\d+)$", href)
            if not m:
                continue
            cid = int(m.group(1))
            name = a.get_text(strip=True)
            if not name:
                continue
            # Keep the longest name for each course ID
            if cid not in courses or len(name) > len(courses[cid]["name"]):
                courses[cid] = {"id": cid, "name": name}

        result = list(courses.values())
        result.sort(key=lambda c: c["name"].lower())
        return result

    def select_course(self, courses: list[dict]) -> dict:
        """Display courses and let the user pick one interactively."""
        if not courses:
            click.echo("No courses found.")
            raise SystemExit(1)

        click.echo(f"\nFound {len(courses)} course(s):\n")
        for i, c in enumerate(courses, 1):
            click.echo(f"  {i}. {c['name']}")

        click.echo()
        while True:
            raw = click.prompt("Select a course number").strip()
            try:
                idx = int(raw)
                if 1 <= idx <= len(courses):
                    sel = courses[idx - 1]
                    click.echo(f"  → {sel['name']}")
                    return sel
            except ValueError:
                pass
            click.echo(f"  Please enter a number between 1 and {len(courses)}.")

    # -- Course scraping ----------------------------------------------------

    def scrape_course(
        self, course_id: int, course_dir: Path,
    ) -> tuple[int, int, int]:
        """Crawl a course's web pages, download files and save pages as PDF.

        Uses BFS to walk all links within ``/courses/{course_id}/...``.
        Every HTML page is rendered to PDF via Playwright.
        Actual binary files are downloaded with requests.
        Near-duplicate PDFs (>= 95% similar text) are removed.

        Returns ``(files_downloaded, pages_saved_as_pdf, duplicates_removed)``.
        """
        prefix = f"/courses/{course_id}"

        visited: set[str] = set()
        queue: deque[str] = deque()
        download_urls: dict[str, str | None] = {}  # url -> suggested filename
        # Pages to render as PDF: url -> title
        pdf_pages: dict[str, str] = {}

        # Seed the crawl with the main course sections
        for suffix in (
            "", "/modules", "/pages", "/files",
            "/assignments", "/announcements", "/discussion_topics",
            "/quizzes",
        ):
            queue.append(f"{self.base_url}{prefix}{suffix}")

        click.echo("\n  Crawling course pages...")
        crawl_count = 0

        while queue:
            url = queue.popleft()
            norm = self._normalize_url(url)
            if norm in visited:
                continue
            visited.add(norm)

            parsed = urlparse(norm)
            path = parsed.path

            # Must be on our Canvas host
            if parsed.hostname and parsed.hostname != self.hostname:
                continue

            # Is this a download URL?  Capture it regardless of course prefix
            # (files may be shared across courses on the same Canvas host).
            if self._is_download_url(path, parsed.query):
                # Convert /files/{id} to a clean /files/{id}/download URL
                dl_url = self._to_download_url(path)
                download_urls[dl_url] = None
                continue

            # Only crawl HTML pages within this course
            if not path.startswith(prefix):
                continue

            # Skip non-content sections
            relative = path[len(prefix):].strip("/")
            first_seg = relative.split("/")[0] if relative else ""
            if first_seg in _SKIP_SEGMENTS:
                continue

            # Fetch the page
            crawl_count += 1
            if crawl_count % 10 == 0:
                click.echo(
                    f"    ...crawled {crawl_count} pages, "
                    f"found {len(download_urls)} file(s), "
                    f"{len(pdf_pages)} page(s) so far"
                )

            try:
                resp = self.session.get(norm, timeout=30)
            except Exception:
                continue

            # Skip failed responses
            if resp.status_code >= 400:
                continue

            ct = resp.headers.get("Content-Type", "")
            final_host = urlparse(resp.url).hostname

            # Redirected off-site -> probably a file on cloud storage
            if final_host != self.hostname:
                if "text/html" not in ct:
                    download_urls[norm] = None
                continue

            # Non-HTML response on our host -> it's a file
            if "text/html" not in ct:
                download_urls[norm] = None
                continue

            # Parse the HTML
            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract a page title for the PDF filename
            title = self._extract_title(soup, path)
            pdf_pages[norm] = title

            # Look for actual file download links within the page
            self._find_file_links(soup, resp.url, prefix, download_urls)

            # Also parse embedded HTML in Canvas ENV JS (Rich Content)
            self._extract_env_file_urls(resp.text, download_urls)

            # Extract and queue new links for crawling
            for link_url in self._extract_links(soup, resp.url, prefix):
                link_norm = self._normalize_url(link_url)
                if link_norm not in visited:
                    queue.append(link_norm)

            # Small delay to be polite to the server
            time.sleep(0.1)

        click.echo(
            f"    Crawl complete: visited {crawl_count} page(s), "
            f"found {len(download_urls)} file(s), "
            f"{len(pdf_pages)} page(s) to save as PDF"
        )

        # Discover files from Canvas's file listing API
        # (the files page is JS-rendered, so requests can't scrape it)
        api_files = self._discover_files_from_listing(course_id)
        for dl_url in api_files:
            if dl_url not in download_urls:
                download_urls[dl_url] = None
        if api_files:
            click.echo(
                f"    File listing: found {len(api_files)} file(s) "
                f"({len(download_urls)} total after merge)"
            )

        # Discover File-type module items via the modules API
        # (this works with cookie auth even when the files API is 403)
        mod_files = self._discover_files_from_modules(course_id)
        new_from_mods = 0
        for dl_url in mod_files:
            if dl_url not in download_urls:
                download_urls[dl_url] = None
                new_from_mods += 1
        if mod_files:
            click.echo(
                f"    Modules API: found {len(mod_files)} file(s) "
                f"({new_from_mods} new)"
            )

        # Download all discovered files
        files_downloaded = self._download_files(download_urls, course_dir)

        # Render all visited pages as PDFs
        pages_saved = self._save_pages_as_pdf(pdf_pages, course_dir)

        # Deduplicate near-identical PDFs
        pages_dir = course_dir / "pages"
        dupes_removed = 0
        if pages_dir.exists():
            dupes_removed = deduplicate_pdfs(pages_dir)

        return files_downloaded, pages_saved, dupes_removed

    # -- Internal helpers ---------------------------------------------------

    def _normalize_url(self, url: str) -> str:
        """Normalize a URL: make absolute, strip fragment, strip trailing /."""
        parsed = urlparse(url)
        if not parsed.scheme:
            url = (
                f"{self.base_url}{url}"
                if url.startswith("/")
                else f"{self.base_url}/{url}"
            )
            parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return parsed._replace(fragment="", path=path).geturl()

    @staticmethod
    def _is_download_url(path: str, query: str) -> bool:
        """Return True only for URLs that are definitely file downloads.

        Catches:
        - /files/{id}/download
        - /files/{id} (preview page — will be converted to download URL)
        - ?download_frd=1
        - Known binary file extensions
        """
        # /files/12345/download endpoint
        if re.search(r"/files/\d+/download$", path):
            return True
        # /files/12345 (preview URL — we'll append /download later)
        if re.search(r"/files/\d+$", path):
            return True
        # Force-download query parameter
        if "download_frd" in (query or ""):
            return True
        # Direct link to a file with a binary extension
        ext = Path(path).suffix.lower()
        if ext in _BINARY_EXTENSIONS:
            return True
        return False

    def _to_download_url(self, path: str) -> str:
        """Convert a Canvas file path to a clean download URL.

        Handles:
        - /files/{id}          -> /files/{id}/download
        - /files/{id}/download -> /files/{id}/download  (unchanged)
        - other paths          -> returned as-is

        Always strips query parameters and builds a clean URL.
        """
        m = re.search(r"(/files/\d+)(/download)?$", path)
        if m:
            clean_path = m.group(1) + "/download"
            # Preserve any course prefix before /files/
            idx = path.find(m.group(0))
            full_path = path[:idx] + clean_path
            return f"{self.base_url}{full_path}"
        return f"{self.base_url}{path}"

    def _discover_files_from_listing(
        self, course_id: int,
    ) -> list[str]:
        """Discover files from the Canvas file listing page.

        Tries the REST API first (fast).  If that fails (403 when using
        cookie auth), falls back to rendering the /files page with
        Playwright and scraping the JS-rendered HTML.
        """
        # --- Attempt 1: REST API (works with API tokens) ------------------
        api_url = (
            f"{self.base_url}/api/v1/courses/{course_id}/files"
            f"?per_page=100"
        )
        download_urls: list[str] = []

        click.echo("\n  Fetching file listing...")

        try:
            resp = self.session.get(api_url, timeout=30)
            if resp.status_code == 200:
                page_url: str | None = api_url
                while page_url:
                    resp = self.session.get(page_url, timeout=30)
                    resp.raise_for_status()
                    files = resp.json()
                    if not isinstance(files, list):
                        break
                    for f in files:
                        url = f.get("url")
                        if url:
                            download_urls.append(url)
                    page_url = self._next_link(resp)
                if download_urls:
                    click.echo(
                        f"    API returned {len(download_urls)} file(s)"
                    )
                    return download_urls
        except Exception:
            pass

        # --- Attempt 2: Playwright-rendered /files page -------------------
        click.echo("    API not available, using browser for file listing...")
        return self._discover_files_via_browser(course_id)

    def _discover_files_via_browser(
        self, course_id: int,
    ) -> list[str]:
        """Use Playwright to render the /files page and extract file URLs.

        Canvas renders the file listing with JavaScript, so requests/bs4
        cannot parse it.  We use a headless browser with saved cookies.
        """
        from playwright.sync_api import sync_playwright
        from canvas_download.browser import _load_cookies

        download_urls: list[str] = []
        files_url = f"{self.base_url}/courses/{course_id}/files"

        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()

            # Load saved cookies into browser context
            cookies = _load_cookies() or []
            pw_cookies = []
            for c in cookies:
                cookie: dict = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", self.hostname),
                    "path": c.get("path", "/"),
                }
                if not cookie["domain"].startswith("."):
                    cookie["url"] = (
                        f"https://{cookie['domain']}{cookie['path']}"
                    )
                    del cookie["domain"]
                    del cookie["path"]
                pw_cookies.append(cookie)
            if pw_cookies:
                context.add_cookies(pw_cookies)

            page = context.new_page()
            page.goto(files_url, wait_until="networkidle", timeout=30_000)

            # Extract all file links from the rendered page
            links = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.href)",
            )

            for href in links:
                parsed = urlparse(href)
                path = parsed.path
                if re.search(r"/files/\d+", path):
                    dl = self._to_download_url(path)
                    if dl not in download_urls:
                        download_urls.append(dl)

            # Also try to find file links in the page's data attributes
            # Canvas sometimes stores file info in data attributes
            file_rows = page.query_selector_all(
                "[data-id], .ef-item-row, tr.file"
            )
            for row in file_rows:
                # Try to get file ID from data attributes
                data_id = row.get_attribute("data-id")
                if data_id and data_id.isdigit():
                    dl = self._to_download_url(
                        f"/courses/{course_id}/files/{data_id}"
                    )
                    if dl not in download_urls:
                        download_urls.append(dl)

                # Check inner links
                inner_links = row.query_selector_all("a[href]")
                for a in inner_links:
                    href = a.get_attribute("href") or ""
                    if "/files/" in href:
                        parsed = urlparse(href)
                        dl = self._to_download_url(parsed.path)
                        if dl not in download_urls:
                            download_urls.append(dl)

            page.close()
            context.close()
            browser.close()

        except Exception as e:
            click.echo(f"    Browser file listing failed: {e}")
        finally:
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass

        if download_urls:
            click.echo(
                f"    Browser found {len(download_urls)} file(s)"
            )

        return download_urls

    @staticmethod
    def _next_link(resp: requests.Response) -> str | None:
        """Extract the 'next' page URL from the Link header."""
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                m = re.search(r"<([^>]+)>", part)
                if m:
                    return m.group(1)
        return None

    def _extract_env_file_urls(
        self,
        html: str,
        download_urls: dict[str, str | None],
    ) -> None:
        """Extract file URLs from Canvas's ENV JavaScript variable.

        Canvas stores Rich Content Editor page bodies as HTML strings
        inside the ENV JS object.  File links embedded there (e.g.
        ``/files/12345?wrap=1``) are invisible to BeautifulSoup because
        they are inside a ``<script>`` tag, not the DOM.
        """
        # Find all /files/{id} patterns in the raw HTML text
        # (covers both DOM links *and* JS-embedded HTML)
        for m in re.finditer(r'/files/(\d+)(?:/download)?(?:\?[^"\s]*)?', html):
            file_id = m.group(1)
            dl = self._to_download_url(f"/files/{file_id}")
            if dl not in download_urls:
                download_urls[dl] = None

    def _discover_files_from_modules(
        self, course_id: int,
    ) -> list[str]:
        """Find File-type items in course modules via the modules API.

        The modules API works with cookie auth (unlike the files API)
        and can discover files attached directly to modules as File items.
        """
        download_urls: list[str] = []
        api_url = (
            f"{self.base_url}/api/v1/courses/{course_id}/modules"
            f"?include[]=items&per_page=100"
        )

        page_url: str | None = api_url
        while page_url:
            try:
                resp = self.session.get(page_url, timeout=30)
                if resp.status_code != 200:
                    break
                modules = resp.json()
                if not isinstance(modules, list):
                    break
                for mod in modules:
                    for item in mod.get("items", []):
                        if item.get("type") == "File":
                            content_id = item.get("content_id")
                            if content_id:
                                dl = self._to_download_url(
                                    f"/courses/{course_id}"
                                    f"/files/{content_id}"
                                )
                                if dl not in download_urls:
                                    download_urls.append(dl)
                page_url = self._next_link(resp)
            except Exception:
                break

        return download_urls

    @staticmethod
    def _extract_title(soup: BeautifulSoup, path: str) -> str:
        """Extract a human-readable title from the page."""
        # Try <title> tag first
        title_el = soup.find("title")
        if title_el:
            title = title_el.get_text(strip=True)
            if title and title.lower() not in ("canvas", "redirect"):
                return title

        # Try first <h1>
        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            if text:
                return text

        # Fall back to the last path segment
        seg = path.rstrip("/").split("/")[-1]
        return unquote(seg) if seg else "index"

    def _find_file_links(
        self,
        soup: BeautifulSoup,
        base_url: str,
        course_prefix: str,
        download_urls: dict[str, str | None],
    ) -> None:
        """Find file download links within a page and add them.

        Catches all Canvas file patterns:
        - /courses/{id}/files/{id}/download
        - /courses/{id}/files/{id}  (preview — converted to /download)
        - /courses/{id}/files/{id}?wrap=1  (inline preview — converted)
        - /files/{id}/download
        - /files/{id}              (converted to /download)
        - ?download_frd=1
        - Direct links with binary file extensions
        """
        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            abs_url = urljoin(base_url, href)
            parsed = urlparse(abs_url)
            path = parsed.path

            # Only consider links on our Canvas host
            if parsed.hostname and parsed.hostname != self.hostname:
                continue

            # /files/12345/download (with or without course prefix)
            if re.search(r"/files/\d+/download$", path):
                dl = self._to_download_url(path)
                download_urls[dl] = None
                continue

            # /files/12345 or /files/12345?wrap=1 (preview — convert)
            if re.search(r"/files/\d+$", path):
                dl = self._to_download_url(path)
                download_urls[dl] = None
                continue

            # ?download_frd=1
            if "download_frd" in (parsed.query or ""):
                dl = self._to_download_url(path)
                download_urls[dl] = None
                continue

            # Direct links to files with binary extensions
            ext = Path(path).suffix.lower()
            if ext in _BINARY_EXTENSIONS:
                norm = self._normalize_url(abs_url)
                download_urls[norm] = None

    def _extract_links(
        self, soup: BeautifulSoup, base_url: str, course_prefix: str,
    ) -> list[str]:
        """Extract all link URLs within the course from *soup*.

        Returns course-internal links for crawling, plus any file-like
        URLs on the same Canvas host (even from other courses).
        """
        urls: list[str] = []

        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            if not href or href.startswith(("#", "mailto:", "javascript:")):
                continue
            abs_url = urljoin(base_url, href)
            parsed = urlparse(abs_url)
            host = parsed.hostname
            if host and host != self.hostname:
                continue
            path = parsed.path
            # Always queue links within the course for crawling
            if path.startswith(course_prefix):
                urls.append(abs_url)
            # Also queue file-like URLs from the same host (cross-course)
            elif self._is_download_url(path, parsed.query):
                urls.append(abs_url)

        return urls

    def _save_pages_as_pdf(
        self, pdf_pages: dict[str, str], course_dir: Path,
    ) -> int:
        """Render all discovered pages as PDF using Playwright headless."""
        if not pdf_pages:
            return 0

        pages_dir = course_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        click.echo(f"\n  Saving {len(pdf_pages)} page(s) as PDF...")

        renderer = PDFRenderer(self.hostname)
        try:
            renderer.start()
        except Exception as e:
            click.echo(f"    Failed to start PDF renderer: {e}")
            return 0

        saved = 0
        total = len(pdf_pages)
        seen_names: set[str] = set()

        for i, (url, title) in enumerate(pdf_pages.items(), 1):
            safe_name = sanitize_filename(title)
            # Ensure unique filename
            base_name = safe_name
            counter = 1
            while safe_name in seen_names:
                safe_name = f"{base_name}_{counter}"
                counter += 1
            seen_names.add(safe_name)

            dest = pages_dir / f"{safe_name}.pdf"
            if dest.exists():
                click.echo(f"    [{i}/{total}] Already exists: {safe_name}.pdf")
                saved += 1
                continue

            click.echo(f"    [{i}/{total}] {safe_name}.pdf ...", nl=False)
            if renderer.render_pdf(url, dest):
                saved += 1
                click.echo(" done")
            else:
                click.echo(" FAILED")

        renderer.close()
        click.echo(f"  Pages: {saved}/{total} saved as PDF.")
        return saved

    def _download_files(
        self,
        download_urls: dict[str, str | None],
        course_dir: Path,
    ) -> int:
        """Download all discovered file URLs."""
        if not download_urls:
            return 0

        files_dir = course_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        downloaded = 0
        skipped = 0
        total = len(download_urls)
        seen_names: set[str] = set()

        click.echo(f"\n  Downloading {total} file(s)...")

        for i, url in enumerate(download_urls, 1):
            try:
                resp = self.session.get(url, stream=True, timeout=60)
                resp.raise_for_status()
            except Exception as e:
                click.echo(f"    [{i}/{total}] FAILED: {e}")
                continue

            # If we got HTML back, it's not a real file (e.g. a preview page)
            ct = resp.headers.get("Content-Type", "")
            if "text/html" in ct:
                resp.close()
                continue

            filename = self._filename_from_response(resp, url)
            safe_name = sanitize_filename(filename)

            # Skip video files
            if Path(safe_name).suffix.lower() in _VIDEO_EXTENSIONS:
                click.echo(f"    [{i}/{total}] Skipping video: {safe_name}")
                skipped += 1
                resp.close()
                continue

            # Deduplicate by filename
            if safe_name in seen_names:
                resp.close()
                continue
            seen_names.add(safe_name)

            dest = files_dir / safe_name

            # Skip if already downloaded with matching size
            cl = resp.headers.get("Content-Length")
            if dest.exists() and cl and dest.stat().st_size == int(cl):
                click.echo(f"    [{i}/{total}] Already exists: {safe_name}")
                skipped += 1
                resp.close()
                continue

            click.echo(
                f"    [{i}/{total}] Downloading {safe_name}...", nl=False,
            )
            try:
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(64 * 1024):
                        f.write(chunk)
                downloaded += 1
                click.echo(" done")
            except Exception as e:
                click.echo(f" FAILED: {e}")

        click.echo(f"  Files: {downloaded} downloaded, {skipped} skipped.")
        return downloaded

    @staticmethod
    def _filename_from_response(resp: requests.Response, url: str) -> str:
        """Determine the filename from the response or URL."""
        # Content-Disposition header
        cd = resp.headers.get("Content-Disposition", "")
        if "filename" in cd:
            m = re.search(
                r"filename\*?=['\"]?(?:UTF-8'')?([^'\";\n]+)",
                cd,
                re.IGNORECASE,
            )
            if m:
                return unquote(m.group(1).strip())

        # Final URL path (after redirects)
        path = unquote(urlparse(resp.url).path)
        name = path.rstrip("/").split("/")[-1]
        if name and name != "download" and "." in name:
            return name

        # Original URL path
        for seg in reversed(unquote(urlparse(url).path).split("/")):
            if seg and seg != "download" and "." in seg:
                return seg

        return name or "unknown_file"
