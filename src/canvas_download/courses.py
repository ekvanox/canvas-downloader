"""Course listing and interactive selection (legacy — unused)."""

from __future__ import annotations

from typing import Any

from canvas_download.browser import CanvasBrowser


def list_courses(browser: CanvasBrowser) -> list[dict[str, Any]]:
    """Fetch all active courses for the authenticated user."""
    courses = browser.api_get_paginated(
        "courses", params={"enrollment_state": "active"},
    )

    # Sort alphabetically by name for consistent display
    courses.sort(key=lambda c: c.get("name", "").lower())
    return courses


def select_course(courses: list[dict[str, Any]]) -> dict[str, Any]:
    """Display courses and let the user pick one interactively.

    Returns the selected course dict.
    """
    if not courses:
        print("No active courses found for this account.")
        raise SystemExit(1)

    print()
    print(f"Found {len(courses)} active course(s):")
    print()

    for i, course in enumerate(courses, start=1):
        name = course.get("name", "Unnamed Course")
        code = course.get("course_code", "")
        label = f"  {i}. {name}"
        if code:
            label += f"  ({code})"
        print(label)

    print()

    while True:
        raw = input("Select a course number: ").strip()
        try:
            idx = int(raw)
            if 1 <= idx <= len(courses):
                selected = courses[idx - 1]
                print(f"  → {selected.get('name', 'Unnamed Course')}")
                return selected
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(courses)}.")
