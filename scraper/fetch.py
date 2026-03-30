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
# Two separate logs: one per date-field mode so filed-date and tran-date searches
# don't share state (different searches, different result sets).
FETCHED_LOG     = RAW_DIR.parent / "fetched_windows.json"       # filed-date mode
FETCHED_LOG_TRN = RAW_DIR.parent / "fetched_windows_tran.json"  # tran-date mode

TRAN_TYPES = ["C", "E", "O", "OA", "OD", "OR"]
# C  = Contribution,          E  = Expenditure
# O  = Other,                 OA = Other Account Receivable
# OD = Other Disbursement,    OR = Other Receipt
# (OR: Return/Refund of Contribution, Refunds & Rebates, Misc Other Receipt
#  OD: Misc Other Disbursement, Refunds & Rebates)

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

# Magic bytes for file type detection
_XLS_MAGIC  = b"\xd0\xcf\x11\xe0"   # OLE2 Compound Document (old .xls)
_XLSX_MAGIC = b"PK\x03\x04"         # ZIP archive (new .xlsx)


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


def _validate_download(path: Path) -> int:
    """
    Validate that a downloaded file is a real Excel file with data rows.

    Returns the number of data rows (excluding header), or -1 if the file is
    invalid (HTML error page, corrupted, empty, etc.).
    """
    if not path.exists() or path.stat().st_size == 0:
        return -1

    header = path.read_bytes()[:8]

    # HTML error page (F5 bot defense, session expired, etc.)
    if header[:5].lower() in (b"<!doc", b"<html", b"<head", b"<?xml"):
        return -1

    try:
        if header[:4] == _XLS_MAGIC:
            import xlrd as _xlrd
            _wb = _xlrd.open_workbook(str(path), on_demand=True)
            nrows = _wb.sheet_by_index(0).nrows
            _wb.release_resources()
            return max(nrows - 1, 0)  # subtract header row
        elif header[:4] == _XLSX_MAGIC:
            wb = load_workbook(path, read_only=True)
            count = 0
            for _ in wb.active.rows:
                count += 1
                if count > ORESTAR_ROW_CAP + 1:
                    break
            wb.close()
            return max(count - 1, 0)
        else:
            # Unknown format — treat as invalid
            return -1
    except Exception:
        return -1


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
    date_field: str = "filed",
) -> Path | None:
    """
    Fill the ORESTAR search form for one week, submit, then download
    the Excel export.

    If tran_type is "ALL", no type filter is applied (downloads all types
    in one request). Otherwise, filters to the specific transaction type.

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
        # Transaction type (skip for ALL — leave default which returns everything)
        if tran_type != "ALL":
            page.select_option('select[name="cneSearchTranType"]', tran_type)
            page.wait_for_timeout(600)  # brief wait for any dynamic field updates

        # Date range (MM/DD/YYYY) — filed date or transaction date depending on mode
        if date_field == "tran":
            page.fill('input[name="cneSearchTranStartDate"]',     start.strftime("%m/%d/%Y"))
            page.fill('input[name="cneSearchTranEndDate"]',        end.strftime("%m/%d/%Y"))
        else:
            page.fill('input[name="cneSearchTranFiledStartDate"]', start.strftime("%m/%d/%Y"))
            page.fill('input[name="cneSearchTranFiledEndDate"]',   end.strftime("%m/%d/%Y"))

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

        # ── Validate the downloaded file ──────────────────────────────────
        row_count = _validate_download(filename)
        if row_count < 0:
            log.warning(
                "Invalid download for %s %s→%s — file is not valid Excel "
                "(HTML error page, corrupted, or empty). Deleting and will retry.",
                tran_type, start, end,
            )
            filename.unlink(missing_ok=True)
            _return_to_search(page)
            return None

        if row_count == 0:
            log.info(
                "Empty result for %s %s→%s (0 data rows) — valid but no transactions",
                tran_type, start, end,
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


def _load_fetched(log_file: Path = FETCHED_LOG) -> set:
    """Load the set of already-fetched (tran_type, start, end) keys from disk."""
    if log_file.exists():
        try:
            with open(log_file) as f:
                return {tuple(x) for x in json.load(f)}
        except Exception:
            return set()
    return set()


def _save_fetched(fetched: set, log_file: Path = FETCHED_LOG) -> None:
    """Persist the fetched-windows set to disk immediately."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w") as f:
        json.dump(sorted([list(x) for x in fetched]), f, indent=2)


def _fetch_range(start: date, end: date, date_field: str = "filed") -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    windows = list(week_windows(start, end))
    tasks   = [("ALL", ws, we) for ws, we in windows]
    total   = len(tasks)
    log.info(
        "Fetching %d windows (all types, date_field=%s)",
        total, date_field,
    )

    # Windows recorded here are skipped on every run — they have already been
    # fetched, processed, and committed to git in a previous run.  This is the
    # key mechanism that lets us make progress across runs despite F5 rate-limiting
    # each runner IP after ~25 requests.
    log_file = FETCHED_LOG_TRN if date_field == "tran" else FETCHED_LOG
    fetched = _load_fetched(log_file)
    skipped = sum(1 for tt, ws, we in tasks if (tt, str(ws), str(we)) in fetched)
    if skipped:
        log.info("Skipping %d already-fetched windows (recorded in %s)", skipped, log_file.name)

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
                result = download_week(page, context, w_start, w_end, tran_type, RAW_DIR, date_field)
                consecutive_restarts = 0

                # ── Check for ORESTAR row-cap truncation ─────────────────────
                span_days = (w_end - w_start).days
                cap_hit = False
                if result is not None and result.exists() and span_days > 0:
                    row_count = _validate_download(result)
                    if row_count >= ORESTAR_ROW_CAP:
                        cap_hit = True

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
                    # Only mark the original window as fetched if both
                    # sub-windows are already done.  This prevents data loss
                    # when the scraper stops (rate-limit) before finishing
                    # the second sub-window — the original stays "unfetched"
                    # so the next run will re-split and pick up the remainder.
                    sub1_key = (sub1[0], str(sub1[1]), str(sub1[2]))
                    sub2_key = (sub2[0], str(sub2[1]), str(sub2[2]))
                    if sub1_key in fetched and sub2_key in fetched:
                        fetched.add(key)
                        _save_fetched(fetched, log_file)
                    i += 1
                    continue

                i += 1
                if result is not None:
                    # Only mark as fetched when a file was actually saved.
                    # download_week() returns None on failure (timeout, CSRF
                    # error, etc.) — leave those windows unfetched so the next
                    # run retries them.
                    fetched.add(key)
                    _save_fetched(fetched, log_file)
                else:
                    log.warning(
                        "Download returned None for %s %s→%s — will retry next run",
                        tran_type, w_start, w_end,
                    )
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


def download_filer_window(
    page,
    context,
    filer_id: str,
    start: date,
    end: date,
    raw_dir: Path,
) -> Path | None:
    """
    Download ALL transactions for a specific filer in a date range.

    No transaction type filter — pulls everything. If results hit
    the 4,999 row cap, splits the window in half and retries both halves.
    """
    filename = raw_dir / f"filer{filer_id}_{start.isoformat()}_{end.isoformat()}.xlsx"
    if filename.exists():
        rows = _validate_download(filename)
        if rows > 0 and rows < ORESTAR_ROW_CAP:
            log.debug("Already downloaded: %s (%d rows)", filename.name, rows)
            return filename

    try:
        _return_to_search(page)

        # Fill filer committee ID (no transaction type = all types)
        page.fill('input[name="cneSearchFilerCommitteeId"]', str(filer_id))
        page.wait_for_timeout(300)

        # Transaction date range
        page.fill('input[name="cneSearchTranStartDate"]', start.strftime("%m/%d/%Y"))
        page.fill('input[name="cneSearchTranEndDate"]', end.strftime("%m/%d/%Y"))

        # Submit
        page.click('input[name="search"]')
        try:
            page.wait_for_url(RESULTS_URL_PATTERN, timeout=30_000)
        except PlaywrightTimeout:
            log.warning("Timed out waiting for results: filer %s %s→%s", filer_id, start, end)
            _return_to_search(page)
            return None

        # Extract CSRF + session for Excel export
        results_url = page.url
        csrf = page.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href*="OWASP_CSRFTOKEN"]')];
            if (!links.length) return null;
            const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"'\\s]+)/);
            return m ? m[1] : null;
        }""")
        if not csrf:
            log.warning("No CSRF token for filer %s %s→%s", filer_id, start, end)
            _return_to_search(page)
            return None

        session_match = re.search(r";(JSESSIONID_ORESTAR=[^?&\s]+)", results_url)
        session_path = f";{session_match.group(1)}" if session_match else ""

        # Download Excel
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
            log.debug("requests returned HTML; falling back to browser download...")
            with page.expect_download(timeout=60_000) as dl_info:
                page.goto(f"{EXPORT_URL}?OWASP_CSRFTOKEN={csrf}",
                          wait_until="commit", timeout=60_000)
            dl_info.value.save_as(filename)
        else:
            filename.write_bytes(resp.content)

        # Validate
        row_count = _validate_download(filename)
        if row_count < 0:
            log.warning("Invalid download for filer %s %s→%s — deleting", filer_id, start, end)
            filename.unlink(missing_ok=True)
            _return_to_search(page)
            return None

        log.info("Filer %s %s→%s: %d rows (%d bytes)",
                 filer_id, start, end, row_count, filename.stat().st_size)

        # Row-cap check — split window and recurse
        if row_count >= ORESTAR_ROW_CAP:
            span_days = (end - start).days
            if span_days <= 1:
                log.warning("Row cap hit on a single day for filer %s on %s — cannot split further",
                            filer_id, start)
                _return_to_search(page)
                return filename

            mid = start + timedelta(days=span_days // 2)
            log.warning("Row cap hit for filer %s %s→%s — splitting at %s",
                         filer_id, start, end, mid)
            filename.unlink(missing_ok=True)
            _return_to_search(page)
            result1 = download_filer_window(page, context, filer_id, start, mid, raw_dir)
            if result1 is None:
                log.warning("First half failed for filer %s %s→%s — skipping second half (will retry next run)",
                            filer_id, start, mid)
                raise SessionExpiredError(
                    f"Split window failed for filer {filer_id} {start}→{mid} — incomplete download"
                )
            time.sleep(REQUEST_DELAY)
            result2 = download_filer_window(page, context, filer_id, mid + timedelta(days=1), end, raw_dir)
            if result2 is None:
                log.warning("Second half failed for filer %s %s→%s — incomplete download",
                            filer_id, mid + timedelta(days=1), end)
                raise SessionExpiredError(
                    f"Split window failed for filer {filer_id} {mid+timedelta(days=1)}→{end} — incomplete download"
                )
            return None

        _return_to_search(page)
        time.sleep(REQUEST_DELAY)
        return filename

    except SessionExpiredError:
        raise
    except Exception as exc:
        if "secure.sos.state.or.us/orestar" not in page.url:
            raise SessionExpiredError(
                f"Session expired during filer fetch — redirected to {page.url}"
            ) from exc
        log.warning("Failed filer %s %s→%s: %s", filer_id, start, end, exc)
        try:
            _return_to_search(page)
        except Exception:
            pass
        return None


def backfill_filers(filer_ids: list[str], start_year: int = 2006) -> None:
    """Fetch all transactions for specific filers across all years.

    Writes data/incomplete_backfills.txt with filer IDs that had errors
    (partial downloads due to rate-limiting, split failures, etc.).
    The auto-backfill system prioritizes these on the next run.
    """
    end_date = date.today()
    start_date = date(start_year, 1, 1)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    incomplete_filers: list[str] = []

    log.info("Backfilling %d filers from %s to %s", len(filer_ids), start_date, end_date)

    with sync_playwright() as p:
        browser, context, page = setup_browser(p)
        consecutive_failures = 0
        for fid in filer_ids:
            log.info("=== Backfilling filer %s ===", fid)
            # Count files before to detect if anything was downloaded
            files_before = set(RAW_DIR.glob(f"filer{fid}_*"))
            had_error = False
            try:
                download_filer_window(page, context, fid, start_date, end_date, RAW_DIR)
            except SessionExpiredError:
                had_error = True
                log.warning("Session expired during filer %s — restarting browser", fid)
                try:
                    browser.close()
                except Exception:
                    pass
                browser, context, page = setup_browser(p)
                # Retry once
                try:
                    download_filer_window(page, context, fid, start_date, end_date, RAW_DIR)
                    had_error = False  # retry succeeded
                except Exception as exc:
                    log.error("Failed filer %s after restart: %s", fid, exc)
            except Exception as exc:
                had_error = True
                log.error("Failed filer %s: %s", fid, exc)

            files_after = set(RAW_DIR.glob(f"filer{fid}_*"))
            if files_after - files_before:
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            if had_error:
                incomplete_filers.append(fid)

            if consecutive_failures >= 2:
                log.warning(
                    "Rate-limited: %d consecutive failures — stopping early. "
                    "Remaining filers will be retried on the next run.",
                    consecutive_failures,
                )
                break
        browser.close()

    # Write incomplete filers so auto-backfill can prioritize them
    incomplete_path = RAW_DIR.parent / "incomplete_backfills.txt"
    if incomplete_filers:
        log.info("Incomplete filers (will retry next run): %s", " ".join(incomplete_filers))
        # Append to existing file (don't overwrite — accumulates across runs)
        existing = set()
        if incomplete_path.exists():
            existing = set(incomplete_path.read_text().split())
        existing.update(incomplete_filers)
        incomplete_path.write_text("\n".join(sorted(existing)) + "\n")
    log.info("Filer backfill complete. Raw files in: %s", RAW_DIR)

    log.info("Filer backfill complete. Raw files in: %s", RAW_DIR)


def run_incremental(days: int = 14) -> None:
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Incremental mode: %s → %s", start, today)
    _fetch_range(start, today, date_field="filed")


def run_backfill(start_year: int = 2017, end_year: int | None = None,
                 date_field: str = "filed") -> None:
    start = date(start_year, 1, 1)
    end   = date(end_year, 12, 31) if end_year else date.today()
    end   = min(end, date.today())
    log.info("Backfill mode: %s → %s (date_field=%s)", start, end, date_field)
    _fetch_range(start, end, date_field=date_field)


def run_test(days: int = 7) -> None:
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Test mode: %s → %s", start, today)
    _fetch_range(start, today, date_field="filed")


def count_remaining(start_year: int = 2017, end_year: int | None = None,
                    date_field: str = "filed") -> int:
    """
    Count standard 7-day windows not yet fetched.
    Prints the count to stdout and returns it.
    Used by the backfill workflow to decide whether to re-trigger itself.
    """
    start = date(start_year, 1, 1)
    end   = date(end_year, 12, 31) if end_year else date.today()
    end   = min(end, date.today())
    windows = list(week_windows(start, end))
    tasks = [("ALL", str(ws), str(we)) for ws, we in windows]
    log_file = FETCHED_LOG_TRN if date_field == "tran" else FETCHED_LOG
    fetched = _load_fetched(log_file)
    remaining = sum(1 for key in tasks if key not in fetched)
    print(remaining)
    return remaining


def check_split_gaps(date_field: str = "filed") -> int:
    """
    Detect windows that were split due to the ORESTAR row cap but whose
    second (or first) sub-window was never fetched.  Removes incomplete
    parent windows from the fetched log so they are retried on the next run.

    Returns the number of gaps found and repaired.
    """
    log_file = FETCHED_LOG_TRN if date_field == "tran" else FETCHED_LOG
    fetched = _load_fetched(log_file)
    if not fetched:
        log.info("No fetched windows to check.")
        return 0

    to_remove = set()
    for key in fetched:
        tt, ws_str, we_str = key
        ws = date.fromisoformat(ws_str)
        we = date.fromisoformat(we_str)
        span = (we - ws).days
        if span < 2:
            continue
        half = span // 2
        mid = ws + timedelta(days=half)
        first_key = (tt, str(ws), str(mid))
        second_key = (tt, str(mid + timedelta(days=1)), str(we))
        has_first = first_key in fetched
        has_second = second_key in fetched
        # If one half exists but not the other, the parent was prematurely
        # marked done.  Remove it so the next run re-splits and fetches
        # the missing half.
        if (has_first and not has_second) or (has_second and not has_first):
            to_remove.add(key)
            missing = second_key if has_first else first_key
            log.warning(
                "Split gap: %s %s→%s is missing sub-window %s→%s — "
                "removing parent so it will be re-fetched",
                tt, ws_str, we_str, missing[1], missing[2],
            )

    if to_remove:
        cleaned = fetched - to_remove
        _save_fetched(cleaned, log_file)
        log.info(
            "Removed %d incomplete split windows from %s",
            len(to_remove), log_file.name,
        )
    else:
        log.info("No split gaps found in %s", log_file.name)

    print(len(to_remove))
    return len(to_remove)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download ORESTAR campaign finance Excel exports."
    )
    parser.add_argument(
        "--mode",
        choices=["incremental", "backfill", "test", "count-remaining", "check-gaps"],
        default="incremental",
    )
    parser.add_argument("--days",        type=int,  default=14,   dest="days")
    parser.add_argument("--start-year",  type=int,  default=2017, dest="start_year")
    parser.add_argument("--end-year",    type=int,  default=None, dest="end_year")
    parser.add_argument(
        "--date-field",
        choices=["filed", "tran"],
        default="filed",
        dest="date_field",
        help="Search by filed date (default) or transaction date (use 'tran' for pre-2017 backfill)",
    )
    parser.add_argument(
        "--filer-ids",
        nargs="+",
        help="Fetch all transactions for specific filer committee IDs (bypasses --mode)",
    )

    args = parser.parse_args()

    if args.filer_ids:
        backfill_filers(args.filer_ids, start_year=args.start_year)
    elif args.mode == "incremental":
        run_incremental(days=args.days)
    elif args.mode == "backfill":
        run_backfill(start_year=args.start_year, end_year=args.end_year,
                     date_field=args.date_field)
    elif args.mode == "test":
        run_test(days=args.days)
    elif args.mode == "count-remaining":
        count_remaining(start_year=args.start_year, end_year=args.end_year,
                        date_field=args.date_field)
    elif args.mode == "check-gaps":
        check_split_gaps(date_field=args.date_field)


if __name__ == "__main__":
    main()
