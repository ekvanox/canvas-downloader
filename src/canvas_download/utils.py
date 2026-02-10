"""Utility functions: filename sanitization and path helpers."""

from __future__ import annotations

import re
from pathlib import Path

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
_MAX_FILENAME_LEN = 200


def sanitize_filename(name: str) -> str:
    """Replace illegal filesystem characters and truncate to a safe length."""
    cleaned = _ILLEGAL_CHARS.sub("_", name).strip()
    if len(cleaned) > _MAX_FILENAME_LEN:
        # Preserve extension when truncating
        stem, _, ext = cleaned.rpartition(".")
        if stem and ext:
            max_stem = _MAX_FILENAME_LEN - len(ext) - 1
            cleaned = f"{stem[:max_stem]}.{ext}"
        else:
            cleaned = cleaned[:_MAX_FILENAME_LEN]
    return cleaned


def get_download_dir() -> Path:
    """Return the root download directory: ~/downloaded-courses/."""
    return Path.home() / "downloaded-courses"


def get_course_dir(course_name: str) -> Path:
    """Return the download directory for a specific course."""
    return get_download_dir() / sanitize_filename(course_name)
