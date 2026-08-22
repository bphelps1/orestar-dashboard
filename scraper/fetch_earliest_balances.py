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
import sys

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

# Seconds to let F5's JavaScript challenge resolve into the real page before
# reloading. The challenge is quick; the old fixed 5-second sleep was both too
# long when it had already cleared and too short when it had not.
CHALLENGE_WAIT = 20
# How many times to re-request a page that never gets past the challenge.
PAGE_LOAD_ATTEMPTS = 3

# Wait between "Prev" clicks
PREV_CLICK_WAIT = 1.5
# Retries for a single Prev click. A flaky navigation must not be able to
# pass itself off as the beginning of a committee's filing history.
PREV_CLICK_RETRIES = 3

# Fraction of a batch that may fail before the run is treated as blocked
# rather than merely unlucky.
BATCH_FAILURE_ABORT = 0.5

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
        # Both rows are labelled just "In-Kind"; only the enclosing section
        # tells them apart. Asking for labels the page never prints returned
        # zero for all 45,938 yearly records.
        "inkind_contributions": orestar_parse.parse_dollar_between(
            html, "Cash Contributions", "Total Contributions", "In-Kind", 0.0),
        "inkind_expenditures": orestar_parse.parse_dollar_between(
            html, "Cash Expenditures", "Total Expenditures", "In-Kind", 0.0),
        "accounts_receivable": _parse_dollar(html, "Accounts Receivable"),
        "accounts_payable": _parse_dollar(html, "Accounts Payable"),
        "total_outstanding_loans": _parse_dollar(html, "Total Outstanding Loans"),
        "outstanding_personal_expenditures": _parse_dollar(html, "Outstanding Personal Expenditures"),
    }


def _load_summary_page(page, url: str, filer_id: str) -> str | None:
    """Load an Account Summary page, sitting through F5's bot challenge.

    ORESTAR fronts every request with an F5/TSPD JavaScript challenge: it
    answers 200 with a ~7 KB script that computes a token, sets a cookie and
    re-requests the real page. The cookies carry Max-Age=30, so the challenge
    recurs constantly rather than once per session.

    The old code did `page.goto(url)` — which waits for the LOAD event — and
    called anything else a failure. That is the wrong success condition: the
    challenge document replaces itself mid-flight, so `load` may never fire on
    the document being waited for, and a 30-second timeout was reported as
    "Failed to load page" even when the browser went on to render the real
    thing. On 27 July that turned into 200 of 200 filers "failing" while a
    plain browser could open the very same URLs.

    So: return as soon as the DOM is parsed, then wait for the thing that
    actually matters — the year heading — reloading a few times to give the
    challenge room. A failure now means the data genuinely never arrived.
    """
    for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            # Not fatal by itself — the challenge often lands the real page
            # anyway, so fall through and check the content before judging.
            log.debug("Filer %s: goto attempt %d raised: %s", filer_id, attempt, e)

        deadline = time.time() + CHALLENGE_WAIT
        while time.time() < deadline:
            try:
                html = page.content()
            except Exception:
                html = ""
            if _YEAR_RE.search(html):
                return html
            time.sleep(0.5)

        log.debug("Filer %s: challenge not cleared on attempt %d/%d",
                  filer_id, attempt, PAGE_LOAD_ATTEMPTS)

    log.warning("Filer %s: page never resolved past the bot challenge after %d attempts",
                filer_id, PAGE_LOAD_ATTEMPTS)
    return None


def _scrape_filer_earliest(page, filer_id: str) -> tuple[dict | None, dict]:
    """Navigate to a filer's Account Summary and click Prev until earliest year.

    Returns (earliest_balance_info, yearly_summaries) where yearly_summaries
    is {year_str: {beginning_balance, contributions, expenditures, ...}}.
    """
    yearly = {}  # year_str → summary dict
    url = f"{BASE_URL}/publicAccountSummary.do?filerId={filer_id}"

    html = _load_summary_page(page, url, filer_id)
    if html is None:
        return None, yearly

    year = _parse_year(html)
    if not year:
        log.warning("No year found on page for filer %s", filer_id)
        return None, yearly

    log.info("Filer %s: starting at year %d", filer_id, year)

    # Collect the current year's data
    yearly[str(year)] = _parse_yearly_summary(html)

    # Page back with "Prev" to the FIRST account statement ORESTAR holds. Its
    # "Beginning Balance (Previous Year)" is the anchor the whole rolling
    # cash-on-hand calculation starts from.
    #
    # Reaching that page is the entire point, so the two ways of stopping are
    # kept apart. Arriving at the first statement — no Prev button left, or the
    # year stops going down — is a RESULT. Running out of clicks, or a click
    # that fails, is a FAILURE that happens to leave the browser on some middle
    # year whose beginning balance is a mid-history figure, not an opening one.
    #
    # Both used to `break` into the same code path, so a timeout on one click
    # recorded whatever year it stopped on as "earliest". 155 filers carry a
    # balance collected that way, the worst of them 19 years short: filer 142
    # stopped at 2024 and banked $220,614.68 as an opening balance for a
    # committee that has been filing since 2006.
    prev_year = year
    max_clicks = 30                    # 2006–2026 is 21 years, so this is slack
    reached_earliest = False

    for click_num in range(max_clicks):
        prev_btn = page.locator('input[type="submit"][value="Prev"]').first
        if prev_btn.count() == 0:
            log.info("Filer %s: no Prev button at year %d — first statement reached",
                     filer_id, year)
            reached_earliest = True
            break

        # Retry the click: a single flaky navigation should not be allowed to
        # masquerade as the start of a committee's history.
        # Each Prev is a fresh request, so F5 can challenge again — its cookies
        # last 30 seconds. Waiting on "networkidle" is the same wrong success
        # condition as before: wait for the year heading to come back instead.
        html, new_year = None, None
        for attempt in range(PREV_CLICK_RETRIES):
            try:
                prev_btn.click()
            except Exception as e:
                log.warning("Filer %s: Prev click failed at year %d (attempt %d/%d): %s",
                            filer_id, year, attempt + 1, PREV_CLICK_RETRIES, e)
                time.sleep(PREV_CLICK_WAIT * (attempt + 1))
                continue
            deadline = time.time() + CHALLENGE_WAIT
            while time.time() < deadline:
                try:
                    html = page.content()
                except Exception:
                    html = ""
                new_year = _parse_year(html)
                if new_year:
                    break
                time.sleep(0.5)
            if new_year:
                break
            log.warning("Filer %s: no year heading after Prev at %d (attempt %d/%d)",
                        filer_id, year, attempt + 1, PREV_CLICK_RETRIES)

        if not new_year:
            log.error("Filer %s: lost the year after Prev click %d — opening balance "
                      "NOT trustworthy", filer_id, click_num + 1)
            break

        if new_year >= prev_year:
            # ORESTAR's own floor: Prev no longer moves us back.
            log.info("Filer %s: year stopped decreasing at %d — first statement reached",
                     filer_id, new_year)
            reached_earliest = True
            break

        yearly[str(new_year)] = _parse_yearly_summary(html)
        prev_year = new_year
        year = new_year
    else:
        log.error("Filer %s: still paging after %d clicks (year %d) — opening balance "
                  "NOT trustworthy", filer_id, max_clicks, year)

    html = page.content()
    final_year = _parse_year(html) or year
    beg_bal = _parse_beginning_balance(html)

    log.info("Filer %s: earliest year = %d, beginning balance = $%.2f, years = %d, complete = %s",
             filer_id, final_year, beg_bal, len(yearly), reached_earliest)

    earliest = {
        "earliest_year": final_year,
        "beginning_balance": beg_bal,
        # Consumers must check this before using beginning_balance as an anchor.
        "reached_earliest": reached_earliest,
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
        "--max-minutes", type=int, default=0,
        help="stop after this long so the JOB's timeout never arrives mid-run "
             "(0 = no budget). A cancelled step is not a successful one, and the "
             "retrigger is gated on success, so a timeout silently ends the chain.",
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
            # An entry that never reached the first statement holds a
            # mid-history balance, not an opening one. Retry those regardless
            # of age — freshness is not what is wrong with them.
            #
            # Scoped to entries carrying a non-zero balance, and to ones known
            # to have failed. A cached $0 anchor contributes nothing whether it
            # is trusted or not, so re-scraping all 7,366 to confirm zeros
            # would be thousands of page loads to change no number; those pick
            # the flag up on their normal refresh instead.
            or cache[fid].get("reached_earliest") is False
            or (not cache[fid].get("reached_earliest")
                and cache[fid].get("beginning_balance"))
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
        # Stop on our own terms, before the job's timeout stops us.
        #
        # This sweep has no natural end inside one run — it walks thousands of
        # filers — so it ran until the job's 180-minute limit cancelled it. A
        # cancelled step is not a successful one, and "Retrigger for next batch"
        # is gated on success(), so the chain ended with 5,454 filers still to
        # do and nothing scheduled to resume. One partial batch, then silence.
        #
        # Finishing early and deliberately keeps the step successful, which is
        # what lets the chain continue. The gate stays as it is on purpose:
        # success() is also how cancelling a runaway chain stops it.
        _deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
        for fid in ids_to_fetch:
            if _deadline and time.time() >= _deadline:
                log.info("Reached the %d-minute budget after %d filers — stopping so "
                         "this run counts as successful and the chain continues.",
                         args.max_minutes, done)
                break
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

    # A batch where nearly everything failed is not a batch that ran.
    #
    # On 27 July one scraped 200 filers, failed all 200 on page-load timeouts,
    # logged them as warnings, exited 0 and retriggered itself — holding the
    # concurrency lane and evicting a scheduled job, to correct no balances at
    # all. ORESTAR was up; it was refusing us, most likely after the daily job
    # had just pulled 7,245 pages through it.
    #
    # Failing loudly here stops the chain instead of feeding it into a wall,
    # and the failure rate is the signal: a handful of bad filers is normal,
    # almost none succeeding is a blocked scraper.
    if done and failed / done >= BATCH_FAILURE_ABORT:
        remaining_path.write_text("0")          # do not retrigger into a wall
        log.error("%d of %d filers failed (%.0f%%). ORESTAR is refusing this "
                  "scraper — stopping rather than retriggering.",
                  failed, done, 100 * failed / done)
        sys.exit(1)


if __name__ == "__main__":
    main()
