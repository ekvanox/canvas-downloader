"""CLI entry point — orchestrates browser login, course selection, and scraping."""

from __future__ import annotations

import argparse
import sys

import questionary

from canvas_download.browser import clear_session, create_session
from canvas_download.scraper import CanvasScraper
from canvas_download.utils import get_course_dir, load_saved_hostname


def main() -> None:
    """Download all files and save pages as PDF from a Canvas LMS course."""

    parser = argparse.ArgumentParser(
        description="Download all files and pages from a Canvas LMS course.",
    )
    parser.add_argument(
        "--clear-session",
        action="store_true",
        help="Delete saved browser session and exit.",
    )
    parser.add_argument(
        "--hostname",
        type=str,
        default=None,
        help="Canvas hostname (e.g. canvas.education.lu.se). "
        "Overrides any saved hostname.",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=str,
        default=None,
        help="Root directory for downloads (default: ~/downloaded-courses).",
    )
    args = parser.parse_args()

    if args.clear_session:
        clear_session()
        return

    # ── Hostname ──────────────────────────────────────────────────────────
    hostname = args.hostname

    if not hostname:
        saved_hostname = load_saved_hostname()
        if saved_hostname:
            print(f"Using saved hostname: {saved_hostname}")
            hostname = saved_hostname
        else:
            hostname = questionary.text(
                "Canvas hostname (e.g. canvas.education.lu.se):",
            ).ask()
            if not hostname:
                print("No hostname provided.")
                sys.exit(1)
            hostname = hostname.strip()

    hostname = hostname.removeprefix("https://").removeprefix("http://")
    hostname = hostname.rstrip("/")

    # ── Login via browser → get cookies → requests.Session ────────────────
    print("\nStarting session...")

    try:
        session, user_name = create_session(hostname)  # noqa: F841
    except Exception as e:
        print(f"\nLogin failed: {e}")
        sys.exit(1)

    # ── Course selection ──────────────────────────────────────────────────
    scraper = CanvasScraper(session, hostname)

    print("\nFetching courses...")
    courses = scraper.list_courses()

    if not courses:
        print("No courses found.")
        sys.exit(1)

    choices = [
        questionary.Choice(title=c["name"], value=c)
        for c in courses
    ]
    selected = questionary.select(
        "Select a course:", choices=choices,
    ).ask()

    if selected is None:
        print("No course selected.")
        sys.exit(1)

    course_id = selected["id"]
    course_name = selected["name"]

    course_dir = get_course_dir(course_name, base_dir=args.output_dir)
    course_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nDownloading to: {course_dir}")

    files_count, pages_count, dupes_removed = scraper.scrape_course(
        course_id, course_dir,
    )

    # ── Summary ───────────────────────────────────────────────────────────
    print()
    print("━" * 40)
    print(f"  Course : {course_name}")
    print(f"  Files  : {files_count} downloaded")
    print(f"  Pages  : {pages_count} saved as PDF ({dupes_removed} duplicates removed)")
    print(f"  Output : {course_dir}")
    print("━" * 40)
    print("Done!")
