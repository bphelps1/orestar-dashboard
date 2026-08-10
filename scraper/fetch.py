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
from datetime import date, datetime, timedelta
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

# {(type, start, end, amt_from, amt_to, payee_prefix): true_record_count}
# Filled from each results page and flushed to disk by _flush_record_counts.
RECORD_COUNTS: dict = {}

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
    amt_from: str | None = None,
    amt_to: str | None = None,
    payee_prefix: str | None = None,
) -> Path | None:
    """
    Fill the ORESTAR search form for one week, submit, then download
    the Excel export.

    If tran_type is "ALL", no type filter is applied (downloads all types
    in one request). Otherwise, filters to the specific transaction type.

    Returns the path to the saved .xlsx file, or None on failure.
    """
    # The narrowing dimensions belong in the filename, or two different
    # sub-windows of the same day would collide on disk and the second would be
    # skipped as "already downloaded".
    _suffix = ""
    if amt_from is not None or amt_to is not None:
        _suffix += f"_amt{amt_from or ''}-{amt_to or ''}"
    if payee_prefix:
        _suffix += f"_p{payee_prefix}"
    filename = raw_dir / f"{tran_type}_{start.isoformat()}_{end.isoformat()}{_suffix}.xlsx"
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

        # Narrowing filters, used only when a window has already been split as
        # far as date and type allow.
        if amt_from is not None:
            page.fill('input[name="cneSearchTranAmountFrom"]', str(amt_from))
        if amt_to is not None:
            page.fill('input[name="cneSearchTranAmountTo"]', str(amt_to))
        if payee_prefix:
            page.fill('input[name="cneSearchContributorTxt"]', payee_prefix)
            page.select_option('select[name="cneSearchContributorTxtSearchType"]', "S")

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

        # ORESTAR prints the true number of matching records here, and that
        # number is NOT capped — the page says "58366 records found ... A
        # maximum 5000 records are displayed". It is the only source of truth
        # for how many rows a window should yield, so capture it while we are
        # standing on the page. RECORD_COUNTS is what the completeness
        # verifier compares the downloaded rows against.
        try:
            _txt = page.inner_text("body")
            _m = re.search(r"([\d,]+)\s+records found", _txt)
            if _m:
                RECORD_COUNTS[(tran_type, str(start), str(end),
                               str(amt_from), str(amt_to), str(payee_prefix))] = \
                    int(_m.group(1).replace(",", ""))
        except Exception:
            pass                      # a missing count must never fail the fetch

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

# Amount bands used when a window cannot be split by date any further. Chosen
# to match where Oregon contributions actually cluster — small recurring payroll
# deductions dominate, so the low end needs fine bands and the top can be one
# open bucket.
AMOUNT_BANDS = [
    (None, "9.99"), ("10", "14.99"), ("15", "15.99"), ("16", "19.99"),
    ("20", "24.99"), ("25", "49.99"), ("50", "99.99"), ("100", "249.99"),
    ("250", "999.99"), ("1000", "4999.99"), ("5000", None),
]

# Contributor-name prefixes, the last resort. A payroll batch is thousands of
# rows sharing ONE filer and ONE amount — 4,969 rows of exactly $17.50 on
# 2023-03-02, all from a single committee — so neither amount nor filer can
# partition it. The contributors are all different (4,957 of them), which makes
# their names the only dimension that splits such a batch.
PAYEE_PREFIXES = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + [str(d) for d in range(10)]
    # Names really do start with punctuation — 92 rows in this dataset begin
    # with one of these. A-Z0-9 alone would drop them from any narrowed window,
    # which would be a fresh silent gap of exactly the kind this code exists to
    # close.
    #
    # This list is drawn from characters actually observed in the data, so a
    # first character nobody has seen yet would still be dropped. The real
    # guarantee would be reconciling each sub-query's rows against the parent
    # window's true record count, which ORESTAR prints on the results page and
    # does NOT cap. That check is not built yet; until it is, this tier is
    # thorough rather than provably complete.
    + ["(", '"', "[", "'", ".", "-", "@", "?"]
)


def _narrow_further(tran_type, day, amt_from, amt_to, payee_prefix):
    """Next set of sub-queries for a single-day window that still hits the cap.

    Returns [] when every dimension is spent, which is the honest answer — the
    caller then records the window as incomplete rather than pretending.

    Tiers, in order:
      1. amount bands      — splits a mixed day into ranges
      2. contributor prefix — splits a single-amount batch, the only thing that can

    Filer is not a tier. Every capped cluster measured on this data has exactly
    one filer (4,969 rows of $17.50 on 2023-03-02, all one committee), so a
    filer split would return the same oversized set.
    """
    if amt_from is None and amt_to is None:
        return [(tran_type, day, day, af, at, None) for af, at in AMOUNT_BANDS]
    if not payee_prefix:
        return [(tran_type, day, day, amt_from, amt_to, pre) for pre in PAYEE_PREFIXES]
    return []


def _task_key(task):
    """Log key for a task.

    Un-narrowed windows keep the original 3-part key so the existing fetch log
    (8,000+ entries) stays valid; narrowed sub-windows get a longer key.
    """
    tran_type, ws, we, af, at, pp = task
    if af is None and at is None and pp is None:
        return (tran_type, str(ws), str(we))
    return (tran_type, str(ws), str(we), str(af), str(at), str(pp))


def _narrowing_label(af, at, pp) -> str:
    bits = []
    if af is not None or at is not None:
        bits.append(f"${af or '0'}-{at or 'max'}")
    if pp:
        bits.append(f"payee~{pp}*")
    return ("  [" + ", ".join(bits) + "]") if bits else ""


def _flush_record_counts() -> None:
    """Persist captured record counts next to the fetch log.

    Kept as a plain list of rows rather than a nested structure so
    verify_completeness.py can read it without knowing how windows were split.
    """
    if not RECORD_COUNTS:
        return
    path = RAW_DIR.parent / "record_counts.json"
    try:
        existing = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        existing = []
    seen = {tuple(e["key"]) for e in existing}
    for key, n in RECORD_COUNTS.items():
        if key not in seen:
            existing.append({"key": list(key), "reported": n})
    path.write_text(json.dumps(existing, indent=1))
    log.info("Recorded ORESTAR's own counts for %d windows -> %s",
             len(RECORD_COUNTS), path.name)


def _record_truncated(tran_type: str, day: date, rows: int) -> None:
    """Note a window that came back at the cap and cannot be split any further.

    Written to disk rather than only logged, so an incomplete window is
    discoverable after the run ends instead of living in a CI log nobody
    re-reads. The completeness audit reads this file.
    """
    path = RAW_DIR.parent / "truncated_windows.json"
    try:
        existing = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        existing = []
    entry = {"tran_type": tran_type, "date": str(day), "rows": rows,
             "noticed": datetime.now().isoformat(timespec="seconds")}
    if not any(e.get("tran_type") == tran_type and e.get("date") == str(day) for e in existing):
        existing.append(entry)
        path.write_text(json.dumps(existing, indent=1))


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
    # One request PER TRANSACTION TYPE, not one request for all of them.
    #
    # An all-types request bundles every category into a single export, and a
    # busy week overruns ORESTAR's 4,999-row cap: 2016-10-02..08 holds 6,355
    # rows. Date-splitting is supposed to rescue that, and it partly does — an
    # all-types fetch of that week left us with 2,224 expenditure rows, while
    # requesting type E alone returned 2,517, matching ORESTAR's own reported
    # count exactly. An ID-level diff put all 293 absentees in the "genuinely
    # missing" column: not one was a superseded original.
    #
    # Six smaller requests are far less likely to reach the cap at all, and the
    # per-type result is the one that provably matches ORESTAR. This is also
    # what the original tran-date backfill did, which is why the fetch log's
    # older entries are per-type.
    tasks   = [(t, ws, we, None, None, None) for ws, we in windows for t in TRAN_TYPES]
    total   = len(tasks)
    log.info(
        "Fetching %d windows (%d weeks x %d types, date_field=%s)",
        total, len(windows), len(TRAN_TYPES), date_field,
    )

    # Windows recorded here are skipped on every run — they have already been
    # fetched, processed, and committed to git in a previous run.  This is the
    # key mechanism that lets us make progress across runs despite F5 rate-limiting
    # each runner IP after ~25 requests.
    log_file = FETCHED_LOG_TRN if date_field == "tran" else FETCHED_LOG
    fetched = _load_fetched(log_file)
    skipped = sum(1 for t in tasks if _task_key(t) in fetched)
    if skipped:
        log.info("Skipping %d already-fetched windows (recorded in %s)", skipped, log_file.name)

    with sync_playwright() as p:
        browser, context, page = setup_browser(p)
        i = 0
        consecutive_restarts = 0
        while i < total:
            tran_type, w_start, w_end, amt_from, amt_to, payee_prefix = tasks[i]
            key = _task_key(tasks[i])

            # Skip windows already processed in a previous run
            if key in fetched:
                i += 1
                continue

            log.info("[%d/%d] %s  %s → %s%s", i + 1, total, tran_type, w_start, w_end,
                     _narrowing_label(amt_from, amt_to, payee_prefix))
            try:
                result = download_week(page, context, w_start, w_end, tran_type, RAW_DIR,
                                       date_field, amt_from, amt_to, payee_prefix)
                consecutive_restarts = 0

                # ── Check for ORESTAR row-cap truncation ─────────────────────
                #
                # The check used to be skipped for single-day windows, on the
                # apparent assumption that a day could not overrun the cap. It
                # can: 15 filed-dates in this dataset already exceed 4,999 rows,
                # topping out at 7,965 on 2018-10-02, and those are the counts
                # we HOLD — the true ones are higher. Splitting bottoms out at
                # one day, so those windows truncated with nothing logged.
                #
                # A single day cannot be split further by date, so detection is
                # all we can offer here — but a named, recorded gap beats a
                # silent one, and it gives the audit something to find.
                span_days = (w_end - w_start).days
                cap_hit = False
                if result is not None and result.exists():
                    row_count = _validate_download(result)
                    if row_count >= ORESTAR_ROW_CAP:
                        if span_days > 0:
                            cap_hit = True
                        else:
                            # Date is exhausted; narrow on another dimension.
                            # Order: amount, then contributor name. Filer is
                            # deliberately NOT a tier — every capped cluster
                            # measured here belongs to a single committee, so
                            # splitting on filer would put every row in one
                            # bucket and change nothing.
                            subs = _narrow_further(tran_type, w_start,
                                                   amt_from, amt_to, payee_prefix)
                            if subs:
                                ins = i + 1
                                for sub in subs:
                                    if _task_key(sub) not in fetched:
                                        tasks.insert(ins, sub); total += 1; ins += 1
                                log.warning(
                                    "Cap hit at %s %s%s — narrowing into %d sub-queries",
                                    tran_type, w_start,
                                    _narrowing_label(amt_from, amt_to, payee_prefix), len(subs),
                                )
                            else:
                                log.error(
                                    "TRUNCATED: %s %s%s returned %d rows (cap %d) and no "
                                    "dimension is left to split on — INCOMPLETE",
                                    tran_type, w_start,
                                    _narrowing_label(amt_from, amt_to, payee_prefix),
                                    row_count, ORESTAR_ROW_CAP,
                                )
                                _record_truncated(tran_type, w_start, row_count)

                if cap_hit:
                    half = span_days // 2
                    mid  = w_start + timedelta(days=half)
                    log.warning(
                        "ORESTAR cap hit for %s %s→%s — splitting at %s",
                        tran_type, w_start, w_end, mid,
                    )
                    # Six fields, like every other task. When tasks grew to
                    # carry the narrowing dimensions, this branch kept building
                    # 3-tuples, so the first cap-triggered date split crashed
                    # the fetcher on the next iteration:
                    #   ValueError: not enough values to unpack (expected 6, got 3)
                    # It broke the daily refresh outright, and the cascade test
                    # missed it because that test only exercised a single-day
                    # window, where the date branch never runs.
                    #
                    # The narrowing dimensions carry through the split: a window
                    # already narrowed by amount stays narrowed in both halves.
                    sub1 = (tran_type, w_start, mid, amt_from, amt_to, payee_prefix)
                    sub2 = (tran_type, mid + timedelta(days=1), w_end,
                            amt_from, amt_to, payee_prefix)
                    ins = i + 1
                    for sub in (sub1, sub2):
                        if _task_key(sub) not in fetched:
                            tasks.insert(ins, sub)
                            total += 1
                            ins += 1
                    # Only mark the original window as fetched if both
                    # sub-windows are already done.  This prevents data loss
                    # when the scraper stops (rate-limit) before finishing
                    # the second sub-window — the original stays "unfetched"
                    # so the next run will re-split and pick up the remainder.
                    sub1_key = _task_key(sub1)
                    sub2_key = _task_key(sub2)
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

    _flush_record_counts()
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
        if rows >= 0 and rows < ORESTAR_ROW_CAP:
            log.debug("Already downloaded: %s (%d rows)", filename.name, rows)
            return filename
        if rows >= ORESTAR_ROW_CAP:
            # Previously hit the cap — skip download, go straight to split
            span_days = (end - start).days
            if span_days > 1:
                mid = start + timedelta(days=span_days // 2)
                log.info("Resuming split for filer %s %s→%s at %s (cap file exists)",
                         filer_id, start, end, mid)
                # Note: recursive calls may return None on successful splits
                # (cap file → split → all leaves exist). Only treat as failure
                # if no files exist for the sub-window after the call.
                download_filer_window(page, context, filer_id, start, mid, raw_dir)
                half1_files = list(raw_dir.glob(f"filer{filer_id}_{start.isoformat()}_*.xlsx"))
                if not half1_files:
                    raise SessionExpiredError(
                        f"Split window failed for filer {filer_id} {start}→{mid} — incomplete download"
                    )
                time.sleep(REQUEST_DELAY)
                download_filer_window(page, context, filer_id, mid + timedelta(days=1), end, raw_dir)
                half2_files = list(raw_dir.glob(f"filer{filer_id}_{mid + timedelta(days=1):%Y-%m-%d}_*.xlsx"))
                if not half2_files:
                    raise SessionExpiredError(
                        f"Split window failed for filer {filer_id} {mid+timedelta(days=1)}→{end} — incomplete download"
                    )
                return filename  # return the cap file path to signal success

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
            # Keep the cap file on disk so resume logic can skip re-downloading
            _return_to_search(page)
            download_filer_window(page, context, filer_id, start, mid, raw_dir)
            half1_files = list(raw_dir.glob(f"filer{filer_id}_{start.isoformat()}_*.xlsx"))
            if not half1_files:
                log.warning("First half failed for filer %s %s→%s — skipping second half",
                            filer_id, start, mid)
                raise SessionExpiredError(
                    f"Split window failed for filer {filer_id} {start}→{mid} — incomplete download"
                )
            time.sleep(REQUEST_DELAY)
            download_filer_window(page, context, filer_id, mid + timedelta(days=1), end, raw_dir)
            half2_files = list(raw_dir.glob(f"filer{filer_id}_{mid + timedelta(days=1):%Y-%m-%d}_*.xlsx"))
            if not half2_files:
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
    """Fetch all transactions for specific filers, one year at a time.

    Instead of downloading the full 2006-2026 range and recursively splitting,
    breaks each filer into year-by-year requests. Each year is small enough to
    usually succeed in one request. If a year fails, the next run retries just
    that year — previously downloaded years are skipped (files exist on disk).

    Writes data/incomplete_backfills.txt with filer IDs that had incomplete years.
    """
    current_year = date.today().year
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    incomplete_filers: list[str] = []

    log.info("Backfilling %d filers year-by-year from %d to %d", len(filer_ids), start_year, current_year)

    with sync_playwright() as p:
        browser, context, page = setup_browser(p)
        consecutive_failures = 0
        for fid in filer_ids:
            log.info("=== Backfilling filer %s ===", fid)
            filer_had_error = False
            for year in range(start_year, current_year + 1):
                yr_start = date(year, 1, 1)
                yr_end = date(year, 12, 31) if year < current_year else date.today()
                try:
                    result = download_filer_window(page, context, fid, yr_start, yr_end, RAW_DIR)
                    if result is not None:
                        consecutive_failures = 0
                except SessionExpiredError:
                    filer_had_error = True
                    log.warning("Session expired during filer %s year %d — restarting browser", fid, year)
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser, context, page = setup_browser(p)
                    # Retry this year once with fresh session
                    try:
                        result = download_filer_window(page, context, fid, yr_start, yr_end, RAW_DIR)
                        if result is not None:
                            consecutive_failures = 0
                            continue
                    except Exception as exc:
                        log.error("Failed filer %s year %d after restart: %s", fid, year, exc)
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        log.warning("Rate-limited — stopping filer %s at year %d", fid, year)
                        break
                except Exception as exc:
                    filer_had_error = True
                    log.error("Failed filer %s year %d: %s", fid, year, exc)
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        log.warning("Rate-limited — stopping filer %s at year %d", fid, year)
                        break
            else:
                # All years completed without breaking — filer is done
                if not filer_had_error:
                    log.info("Filer %s: all years downloaded successfully", fid)
                continue

            # Broke out of year loop — filer is incomplete
            filer_had_error = True

            if filer_had_error:
                incomplete_filers.append(fid)

            if consecutive_failures >= 2:
                log.warning(
                    "Rate-limited: %d consecutive failures — stopping early. "
                    "Remaining filers will be retried on the next run.",
                    consecutive_failures,
                )
                break
        browser.close()

    # Write incomplete filers with retry counts so auto-backfill can defer
    # filers that keep failing (e.g. huge filers that always get rate-limited).
    # Format: "filer_id:count" per line.
    incomplete_path = RAW_DIR.parent / "incomplete_backfills.txt"
    if incomplete_filers:
        log.info("Incomplete filers (will retry next run): %s", " ".join(incomplete_filers))
        existing: dict[str, int] = {}
        if incomplete_path.exists():
            for line in incomplete_path.read_text().strip().split("\n"):
                if ":" in line:
                    fid_str, cnt = line.split(":", 1)
                    existing[fid_str.strip()] = int(cnt)
                elif line.strip():
                    existing[line.strip()] = 1
        for fid in incomplete_filers:
            existing[fid] = existing.get(fid, 0) + 1
            log.info("  Filer %s: retry count now %d", fid, existing[fid])
        incomplete_path.write_text(
            "\n".join(f"{fid}:{cnt}" for fid, cnt in sorted(existing.items())) + "\n"
        )
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
    # MUST mirror how _fetch_range enumerates work, or the backfill chain
    # cannot terminate. This counted ("ALL", start, end) keys while the fetcher
    # records one key per TYPE, so the keys it looked for were never written
    # and "remaining" stayed permanently above zero — an endless retrigger.
    windows = list(week_windows(start, end))
    tasks = [(t, str(ws), str(we)) for ws, we in windows for t in TRAN_TYPES]
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
