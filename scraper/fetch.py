"""
fetch.py — Download campaign finance data from Oregon ORESTAR system.

ORESTAR uses F5 bot defense that requires JavaScript execution, so we use
Playwright with a real (headed) browser. In GitHub Actions, xvfb-run provides
a virtual display so headed mode works without a physical screen.

Usage:
    python fetch.py --mode=incremental          # last 14 days (default)
    python fetch.py --mode=backfill             # 2017-01-01 to today
    python fetch.py --mode=backfill --start-year=2020
    python fetch.py --mode=test --days=7        # single week, verify connectivity

Local:    python3 fetch.py --mode=test
CI:       xvfb-run --auto-servernum python fetch.py --mode=incremental
"""

import argparse
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL   = "https://secure.sos.state.or.us/orestar"
SEARCH_URL = f"{BASE_URL}/gotoPublicTransactionSearch.do"
EXPORT_URL = f"{BASE_URL}/XcelCNESearch"

RESULTS_URL_PATTERN = "**/gotoPublicTransactionSearchResults**"

# Raw Excel files land here temporarily (never committed to git)
RAW_DIR = Path(__file__).parent.parent / "data" / "_raw"

TRAN_TYPES = ["C", "E"]   # C = Contribution, E = Expenditure

# Time (seconds) to wait after page.goto() for F5 JS challenge + app to render
PAGE_RENDER_WAIT = 7

# Polite pause between downloads
REQUEST_DELAY = 1.5

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def setup_browser(playwright):
    """
    Launch a headed Chromium browser and load the ORESTAR search form.
    headed=True is required — F5 bot defense blocks headless browsers.
    In GitHub Actions, use:  xvfb-run --auto-servernum python fetch.py ...
    """
    log.info("Launching browser (headed mode)...")
    browser = playwright.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--start-maximized"],
    )
    context = browser.new_context(
        user_agent=USER_AGENT,
        accept_downloads=True,
        no_viewport=True,
    )
    page = context.new_page()
    _load_search_form(page)
    return browser, context, page


def _load_search_form(page) -> None:
    """Navigate to ORESTAR search form and wait for JS to render it."""
    log.info("Loading ORESTAR search form...")
    page.goto(SEARCH_URL, timeout=60_000)
    time.sleep(PAGE_RENDER_WAIT)

    if page.locator('input[name="OWASP_CSRFTOKEN"]').count() == 0:
        raise RuntimeError(
            "ORESTAR search form did not load. "
            "F5 may have blocked the browser, or the site is down."
        )
    log.info("Search form ready.")


def _return_to_search(page) -> None:
    """Go back to the search form for the next iteration."""
    if "gotoPublicTransactionSearch.do" not in page.url:
        _load_search_form(page)


# ---------------------------------------------------------------------------
# Core download logic
# ---------------------------------------------------------------------------

def download_week(
    page,
    context,
    start: date,
    end: date,
    tran_type: str,
    raw_dir: Path,
) -> Path | None:
    """
    Fill the ORESTAR search form for one week + transaction type,
    submit, then download the Excel export.

    Returns the path to the saved .xlsx file, or None on failure.
    """
    filename = raw_dir / f"{tran_type}_{start.isoformat()}_{end.isoformat()}.xlsx"
    if filename.exists():
        log.debug("Already downloaded: %s", filename.name)
        return filename

    try:
        # Ensure we're on the search form
        _return_to_search(page)

        # ── Fill the form ────────────────────────────────────────────────────
        # Transaction type
        page.select_option('select[name="cneSearchTranType"]', tran_type)
        page.wait_for_timeout(600)  # brief wait for any dynamic field updates

        # Filed date range (MM/DD/YYYY)
        page.fill(
            'input[name="cneSearchTranFiledStartDate"]',
            start.strftime("%m/%d/%Y"),
        )
        page.fill(
            'input[name="cneSearchTranFiledEndDate"]',
            end.strftime("%m/%d/%Y"),
        )

        # ── Submit search ─────────────────────────────────────────────────────
        page.click('input[name="search"]')
        try:
            page.wait_for_url(RESULTS_URL_PATTERN, timeout=30_000)
        except PlaywrightTimeout:
            log.warning("Timed out waiting for results: %s %s→%s", tran_type, start, end)
            _return_to_search(page)
            return None

        # ── Extract session info from results page ────────────────────────────
        results_url = page.url

        # CSRF token from the Export link on the results page
        csrf = page.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href*="OWASP_CSRFTOKEN"]')];
            if (!links.length) return null;
            const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"'\\s]+)/);
            return m ? m[1] : null;
        }""")

        if not csrf:
            log.warning("No CSRF token on results page for %s %s→%s", tran_type, start, end)
            _return_to_search(page)
            return None

        # JSESSIONID is embedded in the URL path (not a cookie) — must be included
        session_match = re.search(r";(JSESSIONID_ORESTAR=[^?&\s]+)", results_url)
        session_path  = f";{session_match.group(1)}" if session_match else ""

        # ── Download via requests (faster than browser download) ──────────────
        raw_dir.mkdir(parents=True, exist_ok=True)
        cookies = {c["name"]: c["value"] for c in context.cookies()}

        export_url = f"{EXPORT_URL}{session_path}?OWASP_CSRFTOKEN={csrf}"
        resp = requests.get(
            export_url,
            cookies=cookies,
            headers={"User-Agent": USER_AGENT, "Referer": results_url},
            timeout=120,
        )

        ct = resp.headers.get("Content-Type", "")
        if "html" in ct.lower():
            # Fallback: navigate to the export URL directly in the browser
            log.debug("requests returned HTML; falling back to browser download...")
            with page.expect_download(timeout=60_000) as dl_info:
                page.goto(
                    f"{EXPORT_URL}?OWASP_CSRFTOKEN={csrf}",
                    wait_until="commit",
                    timeout=60_000,
                )
            dl = dl_info.value
            dl.save_as(filename)
            log.info(
                "Saved (browser) %s  (%s → %s, %d bytes)",
                filename.name, start, end, filename.stat().st_size,
            )
        else:
            filename.write_bytes(resp.content)
            log.info(
                "Saved (requests) %s  (%s → %s, %d bytes)",
                filename.name, start, end, len(resp.content),
            )

        # Return to search form for next iteration
        _return_to_search(page)
        time.sleep(REQUEST_DELAY)
        return filename

    except Exception as exc:
        log.warning("Failed %s %s→%s: %s", tran_type, start, end, exc)
        try:
            _return_to_search(page)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Mode drivers
# ---------------------------------------------------------------------------

def week_windows(start: date, end: date):
    """Yield (week_start, week_end) 7-day chunks covering [start, end]."""
    cur = start
    while cur <= end:
        yield cur, min(cur + timedelta(days=6), end)
        cur += timedelta(days=7)


def _fetch_range(start: date, end: date) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    windows = list(week_windows(start, end))
    total   = len(windows) * len(TRAN_TYPES)
    log.info("Fetching %d windows × %d types = %d downloads", len(windows), len(TRAN_TYPES), total)

    with sync_playwright() as p:
        browser, context, page = setup_browser(p)
        count = 0
        for tran_type in TRAN_TYPES:
            for w_start, w_end in windows:
                count += 1
                log.info("[%d/%d] %s  %s → %s", count, total, tran_type, w_start, w_end)
                download_week(page, context, w_start, w_end, tran_type, RAW_DIR)
        browser.close()

    log.info("Fetch complete. Raw files in: %s", RAW_DIR)


def run_incremental(days: int = 14) -> None:
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Incremental mode: %s → %s", start, today)
    _fetch_range(start, today)


def run_backfill(start_year: int = 2017) -> None:
    start = date(start_year, 1, 1)
    today = date.today()
    log.info("Backfill mode: %s → %s", start, today)
    _fetch_range(start, today)


def run_test(days: int = 7) -> None:
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Test mode: %s → %s", start, today)
    _fetch_range(start, today)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download ORESTAR campaign finance Excel exports."
    )
    parser.add_argument(
        "--mode",
        choices=["incremental", "backfill", "test"],
        default="incremental",
    )
    parser.add_argument("--days",       type=int, default=14,   dest="days")
    parser.add_argument("--start-year", type=int, default=2017, dest="start_year")

    args = parser.parse_args()
    if args.mode == "incremental":
        run_incremental(days=args.days)
    elif args.mode == "backfill":
        run_backfill(start_year=args.start_year)
    elif args.mode == "test":
        run_test(days=args.days)


if __name__ == "__main__":
    main()
