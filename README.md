# Canvas Download

CLI tool to download all files and wiki pages from a Canvas LMS course by scraping the web interface.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run playwright install chromium
```

## Usage

```sh
uv run canvas-download
```

On first run you'll be prompted for:

1. **Canvas hostname** — e.g. `canvas.education.lu.se`
2. **Browser login** — a Chromium window will open so you can log in through your institution's normal flow (username/password, SSO, 2FA, etc.)

After login the browser closes. Cookies are saved to `~/.canvas-dl/` so subsequent runs skip the login step.

The tool then scrapes the course's HTML pages (modules, wiki pages, etc.) using `requests` + BeautifulSoup, following links within the course to discover all downloadable content.

### Clear session

```sh
uv run canvas-download --clear-session
```

Or manually:

```sh
rm -rf ~/.canvas-dl/
```

## What it downloads

| Content      | Location                                      |
| ------------ | --------------------------------------------- |
| Course files | `~/downloaded-courses/<course>/files/`         |
| Wiki pages   | `~/downloaded-courses/<course>/pages/*.html`   |

Video files (`.mp4`, `.mov`, etc.) are **excluded** automatically.

Re-running the tool will **skip** files that have already been downloaded (matched by size).

## How it works

1. **Login** — Playwright opens a real Chromium window for authentication (supports SSO, 2FA, etc.)
2. **Cookies** — After login, cookies are extracted and used with a plain `requests.Session`
3. **Crawl** — BFS crawl of the course's HTML pages starting from modules, pages, files, and assignments sections
4. **Download** — Files discovered through link-following are downloaded; wiki pages are saved as clean HTML

No Canvas API endpoints are used — all content is discovered by parsing the regular web pages.

## Project structure

```
src/canvas_download/
├── __init__.py         # Package marker
├── __main__.py         # python -m support
├── cli.py              # Click CLI entry point & orchestration
├── browser.py          # Playwright login, cookie extraction, session creation
├── scraper.py          # BFS course crawler (requests + BeautifulSoup)
└── utils.py            # Filename sanitization, path helpers
```
