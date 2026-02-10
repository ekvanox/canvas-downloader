"""Utility functions: filename sanitization and path helpers."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
_MAX_FILENAME_LEN = 200

STATE_DIR = Path.home() / ".canvas-dl"
_CONFIG_PATH = STATE_DIR / "config.toml"


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


def load_config() -> dict:
    """Load the config file from ``~/.canvas-dl/config.toml``.

    Returns an empty dict if the file doesn't exist or is invalid.
    """
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return tomllib.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(config: dict) -> None:
    """Write *config* to ``~/.canvas-dl/config.toml``."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in config.items():
        if isinstance(value, str):
            lines.append(f'{key} = "{value}"')
        elif isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, int | float):
            lines.append(f"{key} = {value}")
    _CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_saved_hostname() -> str | None:
    """Load the previously saved Canvas hostname from config."""
    return load_config().get("hostname") or None


def save_hostname(hostname: str) -> None:
    """Persist the Canvas hostname to config."""
    config = load_config()
    config["hostname"] = hostname
    save_config(config)


def get_download_dir(base_dir: str | Path | None = None) -> Path:
    """Return the root download directory.

    Priority:
    1. *base_dir* argument (from ``--output-dir``)
    2. ``output_dir`` in ``~/.canvas-dl/config.toml``
    3. ``~/downloaded-courses/``
    """
    if base_dir is not None:
        return Path(base_dir).expanduser().resolve()
    cfg_dir = load_config().get("output_dir")
    if cfg_dir:
        return Path(cfg_dir).expanduser().resolve()
    return Path.home() / "downloaded-courses"


def get_course_dir(
    course_name: str, base_dir: str | Path | None = None,
) -> Path:
    """Return the download directory for a specific course."""
    return get_download_dir(base_dir) / sanitize_filename(course_name)
