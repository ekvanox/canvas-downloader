"""CLI entry point — orchestrates browser login, course selection, and scraping."""

from __future__ import annotations

import sys

import click

from canvas_download.browser import clear_session, create_session, load_saved_hostname
from canvas_download.scraper import CanvasScraper
from canvas_download.utils import get_course_dir


@click.command()
@click.option(
    "--clear-session",
    "do_clear",
    is_flag=True,
    default=False,
    help="Delete saved browser session and exit.",
)
def main(do_clear: bool) -> None:
    """Download all files and save pages as PDF from a Canvas LMS course."""

    if do_clear:
        clear_session()
        return

    # ── Hostname ──────────────────────────────────────────────────────────
    saved_hostname = load_saved_hostname()

    if saved_hostname:
        click.echo(f"Using saved hostname: {saved_hostname}")
        hostname = saved_hostname
    else:
        hostname = click.prompt(
            "Canvas hostname (e.g. canvas.education.lu.se)",
        ).strip()
        hostname = hostname.removeprefix("https://").removeprefix("http://")
        hostname = hostname.rstrip("/")

    # ── Login via browser → get cookies → requests.Session ────────────────
    click.echo()
    click.echo("Starting session...")

    try:
        session, user_name = create_session(hostname)  # noqa: F841
    except Exception as e:
        click.echo(f"\nLogin failed: {e}")
        sys.exit(1)

    # ── Scrape ────────────────────────────────────────────────────────────
    scraper = CanvasScraper(session, hostname)

    click.echo()
    click.echo("Fetching courses...")
    courses = scraper.list_courses()
    selected = scraper.select_course(courses)

    course_id = selected["id"]
    course_name = selected["name"]

    course_dir = get_course_dir(course_name)
    course_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"\nDownloading to: {course_dir}")

    files_count, pages_count = scraper.scrape_course(course_id, course_dir)

    # ── Summary ───────────────────────────────────────────────────────────
    click.echo()
    click.echo("━" * 40)
    click.echo(f"  Course : {course_name}")
    click.echo(f"  Files  : {files_count} downloaded")
    click.echo(f"  Pages  : {pages_count} saved as PDF")
    click.echo(f"  Output : {course_dir}")
    click.echo("━" * 40)
    click.echo("Done!")
