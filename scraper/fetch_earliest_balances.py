#!/usr/bin/env python3
"""
fetch_earliest_balances.py — Scrape ORESTAR Account Summary data for all years.

Uses Playwright (headed browser) to navigate ORESTAR Account Summary pages
backwards via the "Prev" button, collecting ALL years' account summary data
and the earliest-year "Beginning Balance (Previous Year)" for each filer.

This is needed because:
  - ORESTAR's default Account Summary page shows the current year only
  - Year navigation uses POST forms with OWASP CSRF tokens
  - Plain HTTP requests can't navigate years; a real browser session is required
  - Back-calculating from the current year's beginning balance introduces errors
  - Per-year ORESTAR data enables tracing where discrepancies originate

Output:
  data/earliest_balances.json — { "filer_id": { "earliest_year": int, "beginning_balance": float, "ts": float }, ... }
  data/orestar_yearly_summaries.json — { "filer_id": { "years": { "2026": {...}, "2025": {...} }, "ts": float }, ... }

Usage:
    python scraper/fetch_earliest_balances.py [--filer-ids 19763 12345] [--max-filers 100]
"""

from __future__ import annotations

import argparse
import json
import logging
import re

import orestar_parse
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BASE_URL = "https://secure.sos.state.or.us/orestar"
DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_PATH = DATA_DIR / "earliest_balances.json"
YEARLY_PATH = DATA_DIR / "orestar_yearly_summaries.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Wait time after initial page load (F5 bot defense)
PAGE_RENDER_WAIT = 5
# Wait between "Prev" clicks
PREV_CLICK_WAIT = 1.5
# Polite delay between filers
FILER_DELAY = 1.0

_YEAR_RE = re.compile(r"Account Summary Information for the year\s+(\d{4})")


def _parse_year(html: str) -> int | None:
    m = _YEAR_RE.search(html)
    return int(m.group(1)) if m else None


def _parse_beginning_balance(html: str) -> float:
    """The anchor the whole rolling cash-on-hand calculation starts from.

    Worth its own function purely to mark how much rides on it: this figure
    seeds every subsequent year's balance for the filer, so a sign error here
    is not a display bug, it is a wrong number all the way down.
    """
    return orestar_parse.parse_dollar(html, "Beginning Balance (Previous Year)", 0.0)


def _parse_dollar(html: str, label: str) -> float:
    """Extract a dollar amount following a label in the ORESTAR HTML.

    Labels are passed as literals — the shared parser escapes them — so
    "Beginning Balance (Previous Year)" is written plainly here.
    """
    return orestar_parse.parse_dollar(html, label, 0.0)


def _parse_yearly_summary(html: str) -> dict:
    """Parse all Account Summary fields from an ORESTAR year page."""
    return {
        "beginning_balance": _parse_dollar(html, "Beginning Balance (Previous Year)"),
        "contributions": _parse_dollar(html, "Total Contributions"),
        "expenditures": _parse_dollar(html, "Total Expenditures"),
        "other_receipts": _parse_dollar(html, "Other Receipts"),
        "other_disbursements": _parse_dollar(html, "Other Disbursements"),
        "balance_adjustments": _parse_dollar(html, "Balance Adjustments"),
        "ending_cash_balance": _parse_dollar(html, "Ending Cash Balance"),
        "loans_received": _parse_dollar(html, "Loans Received (Non-Exempt)"),
        "loans_received_exempt": _parse_dollar(html, "Loans Received (Exempt)"),
        "loan_payments": _parse_dollar(html, "Loan Payments (Non-Exempt)"),
        "loan_payments_exempt": _parse_dollar(html, "Loan Payments (Exempt)"),
        "inkind_contributions": _parse_dollar(html, "In-Kind Contributions"),
        "inkind_expenditures": _parse_dollar(html, "In-Kind Expenditures"),
        "accounts_receivable": _parse_dollar(html, "Accounts Receivable"),
        "accounts_payable": _parse_dollar(html, "Accounts Payable"),
        "total_outstanding_loans": _parse_dollar(html, "Total Outstanding Loans"),
        "outstanding_personal_expenditures": _parse_dollar(html, "Outstanding Personal Expenditures"),
    }


def _scrape_filer_earliest(page, filer_id: str) -> tuple[dict | None, dict]:
    """Navigate to a filer's Account Summary and click Prev until earliest year.

    Returns (earliest_balance_info, yearly_summaries) where yearly_summaries
    is {year_str: {beginning_balance, contributions, expenditures, ...}}.
    """
    yearly = {}  # year_str → summary dict
    url = f"{BASE_URL}/publicAccountSummary.do?filerId={filer_id}"

    try:
        page.goto(url, timeout=30_000)
        time.sleep(PAGE_RENDER_WAIT)
    except Exception as e:
        log.warning("Failed to load page for filer %s: %s", filer_id, e)
        return None, yearly

    html = page.content()
    year = _parse_year(html)
    if not year:
        log.warning("No year found on page for filer %s", filer_id)
        return None, yearly

    log.info("Filer %s: starting at year %d", filer_id, year)

    # Collect the current year's data
    yearly[str(year)] = _parse_yearly_summary(html)

    # Click "Prev" until we reach the earliest year (when Prev stops changing the year)
    prev_year = year
    max_clicks = 30  # safety limit (covers 2006-2026 range + margin)

    for click_num in range(max_clicks):
        # Find Prev button
        prev_btn = page.locator('input[type="submit"][value="Prev"]').first
        if prev_btn.count() == 0:
            log.info("Filer %s: no Prev button at year %d — this is the earliest", filer_id, year)
            break

        try:
            prev_btn.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
            time.sleep(PREV_CLICK_WAIT)
        except Exception as e:
            log.warning("Filer %s: Prev click failed at year %d: %s", filer_id, year, e)
            break

        html = page.content()
        new_year = _parse_year(html)

        if not new_year:
            log.warning("Filer %s: lost year after Prev click %d", filer_id, click_num + 1)
            break

        if new_year >= prev_year:
            # Year didn't decrease — we've hit the floor
            log.info("Filer %s: year stopped decreasing at %d (was %d) — earliest reached",
                     filer_id, new_year, prev_year)
            break

        log.debug("Filer %s: navigated to year %d", filer_id, new_year)
        # Collect this year's data
        yearly[str(new_year)] = _parse_yearly_summary(html)
        prev_year = new_year
        year = new_year

    # Now we're on the earliest year page — extract beginning balance
    html = page.content()
    final_year = _parse_year(html) or year
    beg_bal = _parse_beginning_balance(html)

    log.info("Filer %s: earliest year = %d, beginning balance = $%.2f, years collected = %d",
             filer_id, final_year, beg_bal, len(yearly))

    earliest = {
        "earliest_year": final_year,
        "beginning_balance": beg_bal,
        "ts": datetime.now().timestamp(),
    }
    return earliest, yearly


def get_all_filer_ids() -> list[str]:
    """Extract unique filer IDs from the ORESTAR cash balances cache."""
    cache_path = DATA_DIR / "orestar_cash_balances.json"
    if not cache_path.exists():
        log.error("No ORESTAR cache found at %s — run aggregation first", cache_path)
        return []
    with open(cache_path) as f:
        cache = json.load(f)
    return sorted(cache.keys())


def main():
    parser = argparse.ArgumentParser(
        description="Scrape earliest-year beginning balances from ORESTAR Account Summary pages."
    )
    parser.add_argument(
        "--filer-ids", nargs="+",
        help="Specific filer IDs to scrape (default: all from ORESTAR cache)",
    )
    parser.add_argument(
        "--max-filers", type=int, default=0,
        help="Max number of filers to process (0 = all)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-scrape even if already cached",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=30,
        help="Re-scrape entries older than this many days (default: 30)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Determine which filer IDs to process
    filer_ids = args.filer_ids or get_all_filer_ids()
    if not filer_ids:
        log.error("No filer IDs to process")
        return

    # Load existing cache
    cache: dict = {}
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            cache = json.load(f)
        log.info("Loaded %d cached earliest balances", len(cache))

    # Load yearly summaries cache to check for missing yearly data
    yearly_cache: dict = {}
    if YEARLY_PATH.exists():
        with open(YEARLY_PATH) as f:
            yearly_cache = json.load(f)
        log.info("Loaded %d cached yearly summary entries", len(yearly_cache))

    # Filter to only IDs that need fetching:
    # - Not in earliest_balances cache at all, OR
    # - Stale (older than max_age_days), OR
    # - Missing from yearly summaries (backfill yearly data for previously scraped filers)
    now_ts = datetime.now().timestamp()
    cutoff = now_ts - args.max_age_days * 86_400

    if args.force:
        ids_to_fetch = filer_ids
    else:
        ids_to_fetch = [
            fid for fid in filer_ids
            if fid not in cache
            or cache[fid].get("ts", 0) < cutoff
            or fid not in yearly_cache
            or not yearly_cache[fid].get("years")
        ]

    # Total remaining (before --max-filers cap) for retrigger logic
    all_remaining = len(ids_to_fetch)

    if args.max_filers > 0:
        ids_to_fetch = ids_to_fetch[:args.max_filers]

    if not ids_to_fetch:
        log.info("All %d filer IDs are already cached and fresh — nothing to do", len(filer_ids))
        remaining_path = DATA_DIR / "earliest_balances_remaining.txt"
        remaining_path.write_text("0")
        return

    log.info("Will scrape earliest balances for %d filers (%d already cached, %d need yearly backfill)",
             len(ids_to_fetch), len(cache),
             sum(1 for fid in ids_to_fetch if fid in cache and fid not in yearly_cache))

    # Launch Playwright
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        log.info("Launching browser (headed mode — required for ORESTAR)...")
        browser = pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            accept_downloads=False,
        )
        page = context.new_page()

        done = 0
        failed = 0
        for fid in ids_to_fetch:
            result, yearly_data = _scrape_filer_earliest(page, fid)
            done += 1

            if result:
                cache[fid] = result
                # Save yearly summaries
                if yearly_data:
                    if fid not in yearly_cache:
                        yearly_cache[fid] = {"years": {}, "ts": 0}
                    yearly_cache[fid]["years"].update(yearly_data)
                    yearly_cache[fid]["ts"] = datetime.now().timestamp()

                # Save caches after each successful scrape (crash safety)
                if done % 10 == 0:
                    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with open(CACHE_PATH, "w") as f:
                        json.dump(cache, f, separators=(",", ":"))
                    with open(YEARLY_PATH, "w") as f:
                        json.dump(yearly_cache, f, separators=(",", ":"))
                    # Write remaining count so workflow can retrigger even on cancel
                    remaining_now = all_remaining - done
                    remaining_path = DATA_DIR / "earliest_balances_remaining.txt"
                    remaining_path.write_text(str(remaining_now))
                    log.info("Progress: %d / %d done (%d failed), cache saved",
                             done, len(ids_to_fetch), failed)
            else:
                failed += 1

            if done < len(ids_to_fetch):
                time.sleep(FILER_DELAY)

        browser.close()

    # Final cache save
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    with open(YEARLY_PATH, "w") as f:
        json.dump(yearly_cache, f, separators=(",", ":"))

    log.info("Done. Scraped %d filers (%d failed). Cache has %d total entries at %s",
             done, failed, len(cache), CACHE_PATH)

    # Report how many remain uncached (for workflow retrigger logic)
    still_remaining = all_remaining - done
    if args.max_filers > 0 and still_remaining > 0:
        log.info("REMAINING: %d filers still need scraping", still_remaining)

    # Write remaining count to a file for the workflow to read
    remaining_path = DATA_DIR / "earliest_balances_remaining.txt"
    remaining_path.write_text(str(still_remaining))


if __name__ == "__main__":
    main()
