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

            # Must be on our Canvas host and within this course
            if parsed.hostname and parsed.hostname != self.hostname:
                continue
            if not path.startswith(prefix):
                continue

            # Skip non-content sections
            relative = path[len(prefix):].strip("/")
            first_seg = relative.split("/")[0] if relative else ""
            if first_seg in _SKIP_SEGMENTS:
                continue

            # Is this a direct download URL?  Queue it for file download.
            if self._is_download_url(path, parsed.query):
                download_urls[norm] = None
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

        This is intentionally strict -- only explicit download endpoints
        and known binary file extensions are treated as downloads.
        """
        # /files/12345/download endpoint
        if re.search(r"/files/\d+/download$", path):
            return True
        # Force-download query parameter
        if "download_frd" in (query or ""):
            return True
        # Direct link to a file with a binary extension
        ext = Path(path).suffix.lower()
        if ext in _BINARY_EXTENSIONS:
            return True
        return False

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
        """Find file download links within a page and add them."""
        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            abs_url = urljoin(base_url, href)
            parsed = urlparse(abs_url)
            path = parsed.path

            # /files/12345/download
            if re.search(r"/files/\d+/download$", path):
                norm = self._normalize_url(abs_url)
                download_urls[norm] = None
                continue

            # ?download_frd=1
            if "download_frd" in (parsed.query or ""):
                norm = self._normalize_url(abs_url)
                download_urls[norm] = None
                continue

            # Direct links to files with binary extensions
            ext = Path(path).suffix.lower()
            if ext in _BINARY_EXTENSIONS:
                norm = self._normalize_url(abs_url)
                download_urls[norm] = None

    def _extract_links(
        self, soup: BeautifulSoup, base_url: str, course_prefix: str,
    ) -> list[str]:
        """Extract all link URLs within the course from *soup*."""
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
            if parsed.path.startswith(course_prefix):
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
