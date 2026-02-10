"""Browser-based login for Canvas LMS.

Opens a real browser window (via Playwright) for the user to log in through
their institution's authentication flow (SSO, 2FA, etc.), then extracts
cookies and returns an authenticated requests.Session for scraping.

Also provides a Playwright-based PDF renderer for saving pages.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests
from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright

STATE_DIR = Path.home() / ".canvas-dl"
COOKIES_PATH = STATE_DIR / "cookies.json"


def clear_session() -> None:
    """Delete saved session data (cookies and hostname)."""
    from canvas_download.utils import load_config, save_config

    removed = False
    if COOKIES_PATH.exists():
        COOKIES_PATH.unlink()
        removed = True

    config = load_config()
    if "hostname" in config:
        del config["hostname"]
        save_config(config)
        removed = True

    if removed:
        print(f"Saved session cleared ({STATE_DIR}).")
    else:
        print("No saved session found.")


def _save_session(cookies: list, hostname: str) -> None:
    """Persist cookies and hostname for next run."""
    from canvas_download.utils import save_hostname

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    save_hostname(hostname)


def _load_cookies() -> list[dict] | None:
    """Load saved cookies from disk."""
    if not COOKIES_PATH.exists():
        return None
    try:
        return json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _build_session(cookies: list, hostname: str) -> requests.Session:
    """Create a requests.Session pre-loaded with Canvas cookies."""
    session = requests.Session()
    for c in cookies:
        session.cookies.set(
            c["name"],
            c["value"],
            domain=c.get("domain", hostname),
            path=c.get("path", "/"),
        )
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })
    return session


def _verify_session(session: requests.Session, hostname: str) -> str | None:
    """Check if the session is valid by fetching /courses.

    Returns the user's display name, or None if not logged in.
    """
    try:
        resp = session.get(
            f"https://{hostname}/courses",
            timeout=15,
            allow_redirects=False,
        )
        if resp.status_code != 200:
            return None
        # Canvas embeds the current user in ENV JS object
        m = re.search(r'"display_name"\s*:\s*"([^"]+)"', resp.text[:10000])
        return m.group(1) if m else "Canvas User"
    except Exception:
        return None


def create_session(hostname: str) -> tuple[requests.Session, str]:
    """Create an authenticated requests.Session for Canvas.

    Tries saved cookies first.  If they're expired, opens a browser window
    for the user to log in interactively.

    Returns ``(session, user_name)``.
    """
    # Try saved cookies
    saved = _load_cookies()
    if saved:
        session = _build_session(saved, hostname)
        name = _verify_session(session, hostname)
        if name:
            print(f"  Logged in as: {name}")
            return session, name
        print("  Saved session expired.")

    # Need interactive login
    print("  Opening browser for login...")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(f"https://{hostname}")
        page.wait_for_load_state("domcontentloaded")

        print()
        print("  A browser window has opened.")
        print("  Please log in to Canvas, then return here.")
        print("  Waiting for login... (timeout: 5 minutes)")

        for _ in range(300):
            cookies = context.cookies()
            session = _build_session(cookies, hostname)
            name = _verify_session(session, hostname)
            if name:
                print(f"\n  Logged in as: {name}")
                _save_session(cookies, hostname)
                print(f"  Session saved to {STATE_DIR}")
                return session, name
            time.sleep(1)

        raise RuntimeError("Login timed out after 5 minutes.")
    finally:
        try:
            pw.stop()
        except Exception:
            pass


# -- PDF renderer -----------------------------------------------------------


class PDFRenderer:
    """Renders Canvas pages as PDF using a headless Playwright browser.

    Uses the same saved cookies so the browser is authenticated.
    """

    def __init__(self, hostname: str) -> None:
        self.hostname = hostname
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    def start(self) -> None:
        """Launch a headless Chromium with saved cookies."""
        cookies = _load_cookies() or []
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context()

        # Add cookies to the browser context
        pw_cookies = []
        for c in cookies:
            cookie: dict = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", self.hostname),
                "path": c.get("path", "/"),
            }
            # Playwright requires either url or domain+path
            if not cookie["domain"].startswith("."):
                cookie["url"] = f"https://{cookie['domain']}{cookie['path']}"
                del cookie["domain"]
                del cookie["path"]
            pw_cookies.append(cookie)

        if pw_cookies:
            self._context.add_cookies(pw_cookies)

    def render_pdf(self, url: str, dest: Path) -> bool:
        """Navigate to *url* and save the page as a PDF.

        Returns True on success, False on failure.
        """
        if self._context is None:
            return False

        page = None
        try:
            page = self._context.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            dest.parent.mkdir(parents=True, exist_ok=True)
            page.pdf(path=str(dest), format="A4", print_background=True)
            page.close()
            return True
        except Exception:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            return False

    def close(self) -> None:
        """Shut down the browser."""
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None
        self._browser = None
        self._context = None
