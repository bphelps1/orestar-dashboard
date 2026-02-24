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
import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from openpyxl import load_workbook
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

# Tracks which windows have been successfully fetched across runs (committed to git)
FETCHED_LOG = RAW_DIR.parent / "fetched_windows.json"

TRAN_TYPES = ["C", "E"]   # C = Contribution, E = Expenditure

# Time (seconds) to wait after page.goto() for F5 JS challenge + app to render
PAGE_RENDER_WAIT = 7

# Polite pause between downloads
REQUEST_DELAY = 1.5

# ORESTAR truncates Excel exports at this many data rows.  Any downloaded file
# with exactly this many rows is treated as capped and the window is split into
# two halves so both halves can be fetched independently.
ORESTAR_ROW_CAP = 4999

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

class SessionExpiredError(Exception):
    """Raised when the ORESTAR session has expired and the browser was redirected away."""
    pass


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

    # Session expiry redirects to sos.oregon.gov — detect and raise
    if "secure.sos.state.or.us/orestar" not in page.url:
        raise SessionExpiredError(
            f"Session expired — redirected to {page.url}"
        )

    if page.locator('input[name="OWASP_CSRFTOKEN"]').count() == 0:
        raise RuntimeError(
            "ORESTAR search form did not load. "
            "F5 may have blocked the browser, or the site is down."
        )
    log.info("Search form ready.")


def _return_to_search(page) -> None:
    """
    Reset the form for the next search.
    Clicks the Reset button if already on the search form (fast),
    otherwise does a full page reload (slow, only needed after errors).
    """
    if "gotoPublicTransactionSearch.do" in page.url:
        # Already on search form — just click Reset to clear fields
        reset = page.locator('input[type="button"][value="Reset"]')
        if reset.count() > 0:
            reset.first.click()
            page.wait_for_timeout(300)
            return
    # Not on search form (e.g. after a results page or error) — full reload
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

    except SessionExpiredError:
        raise  # propagate up to _fetch_range for browser restart
    except Exception as exc:
        # If the page redirected off ORESTAR during a form interaction, the session
        # is broken — raise SessionExpiredError so _fetch_range can restart the browser.
        # (goto(SEARCH_URL) can succeed with an expired session, so we can't rely solely
        # on the _load_search_form URL check — we must catch it here too.)
        if "secure.sos.state.or.us/orestar" not in page.url:
            raise SessionExpiredError(
                f"Session expired during interaction — redirected to {page.url}"
            ) from exc
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


def _load_fetched() -> set:
    """Load the set of already-fetched (tran_type, start, end) keys from disk."""
    if FETCHED_LOG.exists():
        try:
            with open(FETCHED_LOG) as f:
                return {tuple(x) for x in json.load(f)}
        except Exception:
            return set()
    return set()


def _save_fetched(fetched: set) -> None:
    """Persist the fetched-windows set to disk immediately."""
    FETCHED_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(FETCHED_LOG, "w") as f:
        json.dump(sorted([list(x) for x in fetched]), f, indent=2)


def _fetch_range(start: date, end: date) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    windows = list(week_windows(start, end))
    tasks   = [(tt, ws, we) for tt in TRAN_TYPES for ws, we in windows]
    total   = len(tasks)
    log.info("Fetching %d windows × %d types = %d downloads", len(windows), len(TRAN_TYPES), total)

    # Windows recorded here are skipped on every run — they have already been
    # fetched, processed, and committed to git in a previous run.  This is the
    # key mechanism that lets us make progress across runs despite F5 rate-limiting
    # each runner IP after ~25 requests.
    fetched = _load_fetched()
    skipped = sum(1 for tt, ws, we in tasks if (tt, str(ws), str(we)) in fetched)
    if skipped:
        log.info("Skipping %d already-fetched windows (recorded in %s)", skipped, FETCHED_LOG.name)

    with sync_playwright() as p:
        browser, context, page = setup_browser(p)
        i = 0
        consecutive_restarts = 0
        while i < total:
            tran_type, w_start, w_end = tasks[i]
            key = (tran_type, str(w_start), str(w_end))

            # Skip windows already processed in a previous run
            if key in fetched:
                i += 1
                continue

            log.info("[%d/%d] %s  %s → %s", i + 1, total, tran_type, w_start, w_end)
            try:
                result = download_week(page, context, w_start, w_end, tran_type, RAW_DIR)
                consecutive_restarts = 0

                # ── Check for ORESTAR row-cap truncation ─────────────────────
                span_days = (w_end - w_start).days
                cap_hit = False
                if result is not None and result.exists() and span_days > 0:
                    try:
                        wb = load_workbook(result, read_only=True)
                        file_rows = wb.active.max_row  # includes header row
                        wb.close()
                        if file_rows is not None and file_rows - 1 >= ORESTAR_ROW_CAP:
                            cap_hit = True
                    except Exception:
                        pass

                if cap_hit:
                    half = span_days // 2
                    mid  = w_start + timedelta(days=half)
                    log.warning(
                        "ORESTAR cap hit for %s %s→%s — splitting at %s",
                        tran_type, w_start, w_end, mid,
                    )
                    sub1 = (tran_type, w_start, mid)
                    sub2 = (tran_type, mid + timedelta(days=1), w_end)
                    ins = i + 1
                    for sub in (sub1, sub2):
                        sub_key = (sub[0], str(sub[1]), str(sub[2]))
                        if sub_key not in fetched:
                            tasks.insert(ins, sub)
                            total += 1
                            ins += 1
                    # Mark original window done (its 4999 rows are valid; sub-windows
                    # will fetch the remainder and process.py deduplicates by tran_id)
                    fetched.add(key)
                    _save_fetched(fetched)
                    i += 1
                    continue

                i += 1
                # Record as fetched (file saved OR empty results — both are "done")
                fetched.add(key)
                _save_fetched(fetched)
            except SessionExpiredError as exc:
                consecutive_restarts += 1
                log.warning(
                    "Blocked (attempt %d/3) at [%d/%d] %s %s→%s: %s — restarting browser",
                    consecutive_restarts, i + 1, total, tran_type, w_start, w_end, exc,
                )
                try:
                    browser.close()
                except Exception:
                    pass
                if consecutive_restarts >= 3:
                    # Persistent IP-level block — stop now so process.py and the
                    # git commit still run.  Next run will skip already-fetched windows
                    # and get a fresh runner IP, so the next batch will succeed.
                    log.warning(
                        "Rate-limited after 3 attempts at [%d/%d] — stopping fetch to "
                        "preserve partial results. Re-run the workflow to continue.",
                        i + 1, total,
                    )
                    break
                browser, context, page = setup_browser(p)
                # Don't increment i — retry the same window with the fresh session
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
