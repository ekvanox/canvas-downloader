"""Integration tests for the Canvas scraper.

These tests use the locally saved session (~/.canvas-dl/cookies.json)
to make real requests against the configured Canvas instance.

Run:  uv run pytest tests/ -v
"""

from __future__ import annotations

import pytest
import requests

from canvas_download.browser import (
    COOKIES_PATH,
    create_session,
)
from canvas_download.utils import load_saved_hostname
from canvas_download.scraper import CanvasScraper


@pytest.fixture(scope="session")
def hostname() -> str:
    """Load saved hostname, skip the entire suite if none is available."""
    h = load_saved_hostname()
    if h is None:
        pytest.skip(
            "No saved hostname found. "
            "Run 'uv run canvas-download' first to log in and save a session."
        )
    return h


@pytest.fixture(scope="session")
def session(hostname: str) -> requests.Session:
    """Create an authenticated requests.Session from saved cookies."""
    if not COOKIES_PATH.exists():
        pytest.skip(
            "No saved cookies found at ~/.canvas-dl/cookies.json. "
            "Run 'uv run canvas-download' first to log in."
        )
    sess, _ = create_session(hostname)
    return sess


@pytest.fixture(scope="session")
def scraper(session: requests.Session, hostname: str) -> CanvasScraper:
    """Create a CanvasScraper with the authenticated session."""
    return CanvasScraper(session, hostname)


# ── Authentication ────────────────────────────────────────────────────────


class TestAuthentication:
    def test_session_can_fetch_courses(
        self, session: requests.Session, hostname: str,
    ) -> None:
        """Fetching /courses should return 200 (not redirect to login)."""
        resp = session.get(
            f"https://{hostname}/courses",
            allow_redirects=False,
            timeout=15,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code} — session may be expired"
        )


# ── Courses ───────────────────────────────────────────────────────────────


class TestCourses:
    def test_list_courses(self, scraper: CanvasScraper) -> None:
        """Should find courses by parsing the /courses HTML page."""
        courses = scraper.list_courses()
        assert isinstance(courses, list)
        print(f"\n  Found {len(courses)} course(s) by scraping /courses")

        if courses:
            course = courses[0]
            assert "id" in course
            assert "name" in course
            print(f"  First course: {course['name']} (id={course['id']})")


# ── Scraper helpers ───────────────────────────────────────────────────────


class TestScraperHelpers:
    def test_is_download_url(self) -> None:
        """URL patterns that should be detected as file downloads."""
        assert CanvasScraper._is_download_url(
            "/courses/1/files/42/download", "",
        )
        assert CanvasScraper._is_download_url(
            "/courses/1/files/42", "download_frd=1",
        )
        assert CanvasScraper._is_download_url(
            "/courses/1/files/lecture.pdf", "",
        )
        assert not CanvasScraper._is_download_url(
            "/courses/1/pages/intro", "",
        )
        assert not CanvasScraper._is_download_url(
            "/courses/1/modules", "",
        )
