"""
fetch.py — Download campaign finance data from Oregon ORESTAR system.

ORESTAR offers a session-based "Export to Excel" on its public search pages.
No API key required; works in headless environments (GitHub Actions).

Usage:
    python fetch.py --mode=incremental          # last 14 days (default)
    python fetch.py --mode=backfill             # 2017-01-01 to today
    python fetch.py --mode=backfill --start-year=2020
    python fetch.py --mode=test --days=7        # single week, verify connectivity
"""

import argparse
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://secure.sos.state.or.us/orestar"
SEARCH_URL = f"{BASE_URL}/gotoPublicTransactionSearch.do"
EXPORT_URL = f"{BASE_URL}/XcelCNESearch.do"

# Raw Excel files land here temporarily (never committed to git)
RAW_DIR = Path(__file__).parent.parent / "data" / "_raw"

# Transaction types to pull
TRAN_TYPES = ["C", "E"]   # C = Contribution, E = Expenditure
SUBTYPES   = ["CA"]       # CA = Cash

# Seconds to sleep between requests — be a polite scraper
REQUEST_DELAY = 2.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    """Create a session, hit the search page to seed cookies, return session."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (compatible; ORESTAR-Dashboard-Bot/1.0; "
            "+https://github.com/orestar-dashboard)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })

    log.info("Establishing session with ORESTAR…")
    resp = session.get(SEARCH_URL, timeout=30)
    resp.raise_for_status()
    log.info("Session established (cookies: %s)", list(session.cookies.keys()))
    return session


def get_csrf_token(session: requests.Session) -> str | None:
    """
    Fetch the search form and extract any hidden CSRF / struts token.
    ORESTAR uses Struts 1.x; the token field is named 'org.apache.struts.taglib.html.TOKEN'.
    Returns the token value, or None if not found (request still works without it on public pages).
    """
    resp = session.get(SEARCH_URL, timeout=30)
    resp.raise_for_status()

    # Simple scan for the hidden input — avoids a BeautifulSoup dependency
    for line in resp.text.splitlines():
        if "org.apache.struts.taglib.html.TOKEN" in line:
            # e.g. <input type="hidden" name="..." value="abc123">
            parts = line.split('value="')
            if len(parts) > 1:
                token = parts[1].split('"')[0]
                log.debug("CSRF token: %s", token)
                return token
    log.debug("No CSRF token found in form (may not be required)")
    return None


# ---------------------------------------------------------------------------
# Core download logic
# ---------------------------------------------------------------------------

def download_week(
    session: requests.Session,
    start: date,
    end: date,
    tran_type: str,
    raw_dir: Path,
    retries: int = 3,
) -> Path | None:
    """
    Download one week of data for a given transaction type.
    Returns the path to the saved Excel file, or None on failure.

    ORESTAR search form fields (reverse-engineered from browser traffic):
      tranFiledDate       — start date  (MM/DD/YYYY)
      tranFiledDateTo     — end date    (MM/DD/YYYY)
      tranTypeCode        — C or E
      tranSubTypeCd       — CA (cash)
      currentPageNo       — 1
      pageSize            — 200 (irrelevant; export ignores it)
      submitAction        — search
    """
    filename = raw_dir / f"{tran_type}_{start.isoformat()}_{end.isoformat()}.xlsx"
    if filename.exists():
        log.debug("Already downloaded: %s", filename.name)
        return filename

    form_data = {
        "tranFiledDate":   start.strftime("%m/%d/%Y"),
        "tranFiledDateTo": end.strftime("%m/%d/%Y"),
        "tranTypeCode":    tran_type,
        "tranSubTypeCd":   "CA",
        "currentPageNo":   "1",
        "pageSize":        "200",
        "submitAction":    "search",
    }

    for attempt in range(1, retries + 1):
        try:
            # Step 1: POST the search to establish result set in session
            log.debug(
                "Search POST %s %s→%s (attempt %d)",
                tran_type, start, end, attempt,
            )
            resp = session.post(
                f"{BASE_URL}/publicTransactionSearch.do",
                data=form_data,
                timeout=60,
            )
            resp.raise_for_status()

            # Step 2: GET the Excel export
            log.debug("Fetching Excel export…")
            export_resp = session.get(
                EXPORT_URL,
                params={"tranTypeCode": tran_type},
                timeout=120,
            )
            export_resp.raise_for_status()

            content_type = export_resp.headers.get("Content-Type", "")
            if "spreadsheet" not in content_type and "excel" not in content_type and "octet" not in content_type:
                # Might have gotten an HTML error page
                log.warning(
                    "Unexpected Content-Type '%s' for %s %s→%s; "
                    "saving anyway for inspection.",
                    content_type, tran_type, start, end,
                )

            raw_dir.mkdir(parents=True, exist_ok=True)
            filename.write_bytes(export_resp.content)
            log.info(
                "Saved %s  (%s → %s, %d bytes)",
                filename.name, start, end, len(export_resp.content),
            )
            return filename

        except requests.RequestException as exc:
            log.warning("Attempt %d failed for %s %s→%s: %s", attempt, tran_type, start, end, exc)
            if attempt < retries:
                time.sleep(REQUEST_DELAY * attempt * 3)

    log.error("All %d attempts failed for %s %s→%s", retries, tran_type, start, end)
    return None


# ---------------------------------------------------------------------------
# Mode drivers
# ---------------------------------------------------------------------------

def week_windows(start: date, end: date):
    """Yield (week_start, week_end) tuples covering [start, end] in 7-day chunks."""
    current = start
    while current <= end:
        week_end = min(current + timedelta(days=6), end)
        yield current, week_end
        current = week_end + timedelta(days=1)


def run_incremental(days: int = 14) -> None:
    """Pull the last `days` days (default 14 for overlap/amendment coverage)."""
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Incremental mode: %s → %s", start, today)
    _fetch_range(start, today)


def run_backfill(start_year: int = 2017) -> None:
    """Pull everything from Jan 1 of start_year through today."""
    start = date(start_year, 1, 1)
    today = date.today()
    log.info("Backfill mode: %s → %s", start, today)
    _fetch_range(start, today)


def run_test(days: int = 7) -> None:
    """Pull a single short window — used to verify ORESTAR connectivity."""
    today = date.today()
    start = today - timedelta(days=days)
    log.info("Test mode: %s → %s", start, today)
    _fetch_range(start, today)


def _fetch_range(start: date, end: date) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = build_session()

    total_windows = sum(1 for _ in week_windows(start, end))
    log.info(
        "Fetching %d weekly windows × %d transaction types = ~%d requests",
        total_windows, len(TRAN_TYPES), total_windows * len(TRAN_TYPES),
    )

    for tran_type in TRAN_TYPES:
        # Refresh session token between transaction types
        get_csrf_token(session)

        for w_start, w_end in week_windows(start, end):
            download_week(session, w_start, w_end, tran_type, RAW_DIR)
            time.sleep(REQUEST_DELAY)

    log.info("Fetch complete. Raw files in: %s", RAW_DIR)


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
        help="incremental=last 14 days, backfill=2017-present, test=short verify",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Days to look back in incremental/test mode (default: 14)",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=2017,
        dest="start_year",
        help="Start year for backfill mode (default: 2017)",
    )

    args = parser.parse_args()

    if args.mode == "incremental":
        run_incremental(days=args.days)
    elif args.mode == "backfill":
        run_backfill(start_year=args.start_year)
    elif args.mode == "test":
        run_test(days=args.days)


if __name__ == "__main__":
    main()
