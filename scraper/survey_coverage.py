#!/usr/bin/env python3
"""
survey_coverage.py — which committees are actually missing transactions?

The backfill recovers missing rows. For a large share of flagged committees
that is not the problem, and pointing the backfill at them spends the request
budget to learn nothing.

Committee for SAIF Keeping is the clearest case: it tops the discrepancy list
at $665,242, ORESTAR holds SEVEN transactions for it in total, we hold all
seven, and ORESTAR's last account summary (2008) says the committee ended with
nothing. There are no rows to fetch. Its delta comes from a transaction-derived
balance disagreeing with a summary for a committee that predates the
transaction record — a different question, with a different answer.

Local 48 Electricians is the opposite: ORESTAR reports 18,968 rows for 2023
against the 9,937 we hold, and a narrowed re-fetch has already recovered rows
that were genuinely absent.

Telling those two apart costs ONE search per committee. ORESTAR prints the
number of matching records on the results page and does not cap it, so a single
query settles "are we missing rows, and how many" without downloading anything.
This surveys that, cheaply, and ranks committees by what the backfill can
actually fix.

Deliberately does not export. A survey that downloaded would cost the same as
the backfill it is meant to target.

Usage:
    python scraper/survey_coverage.py                    # flagged committees, worst delta first
    python scraper/survey_coverage.py --filer-ids 4572 416
    python scraper/survey_coverage.py --limit 40         # stop after N committees
    python scraper/survey_coverage.py --report           # print findings, fetch nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

import fetch as F
import supabase_sync

DATA_DIR = Path(__file__).parent.parent / "data"
SURVEY_PATH = DATA_DIR / "coverage_survey.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

FIRST_YEAR = 2006


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _flagged_committees() -> list[dict]:
    """Committees with a balance discrepancy, worst first.

    Read from the database rather than data/aggregated/filers/*.json. Those
    files are only as fresh as the last local pull, and ranking from a stale
    copy produced a priority list whose top entry had no discrepancy at all —
    it had been fixed days earlier.
    """
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("select data from dashboard_cache where key = 'balance_discrepancies'")
            row = cur.fetchone()
            if not row:
                return []
            d = row[0]
            if isinstance(d, str):
                d = json.loads(d)
    finally:
        conn.close()
    rows = d.get("filers") if isinstance(d, dict) else d
    if rows is None and isinstance(d, dict):
        rows = next((v for v in d.values() if isinstance(v, list)), [])
    out = [r for r in (rows or []) if r.get("filer_id")]
    out.sort(key=lambda r: -abs(r.get("delta", 0) or 0))
    return out


def _our_counts(filer_ids: list[str]) -> dict[str, int]:
    """Rows we hold per filer — one query for the whole batch, not one each."""
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select filer_id, count(*) from transactions "
                "where filer_id = any(%s) group by filer_id",
                (list(filer_ids),),
            )
            return {str(k): int(v) for k, v in cur.fetchall()}
    finally:
        conn.close()


def _load_survey() -> dict:
    if SURVEY_PATH.exists():
        try:
            return {str(e["filer_id"]): e for e in json.loads(SURVEY_PATH.read_text())}
        except Exception:
            return {}
    return {}


def _save_survey(surveyed: dict) -> None:
    """Written after every committee, not at the end.

    A run that is cut off by F5 mid-survey must keep what it learned; the whole
    point is that progress accumulates across runs.
    """
    SURVEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(surveyed.values(), key=lambda e: -(e.get("missing") or 0))
    SURVEY_PATH.write_text(json.dumps(rows, indent=1))


# ---------------------------------------------------------------------------
# The one query
# ---------------------------------------------------------------------------

def _return_to_form(page) -> None:
    """Back to the search form, without the flat seven-second sleep.

    fetch.py's _return_to_search clicks Reset when it is already on the form,
    but a survey is always standing on a results page, so it took the slow path
    every single time: a full navigation plus PAGE_RENDER_WAIT = 7 seconds,
    whether or not the form was ready sooner.

    Polling for the form's CSRF field is both faster and more correct. The
    sleep existed because F5's JavaScript challenge needs time to run, and
    waiting for the element it produces handles that properly: if the challenge
    is still going the field is not there yet and we keep waiting, and if the
    page is ready early we proceed immediately.
    """
    if "gotoPublicTransactionSearch.do" not in page.url:
        page.goto(F.SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
    if "secure.sos.state.or.us/orestar" not in page.url:
        raise F.SessionExpiredError(f"Session expired — redirected to {page.url}")
    # state="attached", NOT the default "visible".
    #
    # OWASP_CSRFTOKEN is <input type="hidden">, so it is never visible and the
    # default state can never be satisfied. The first version of this waited on
    # the default, timed out after 30 seconds on a perfectly healthy page, and
    # reported the timeout as "F5 challenge or session expiry" — inventing a
    # rate limit that was not happening and surveying zero committees where the
    # slow path had managed 96. _load_search_form gets this right by using
    # .count(), which ignores visibility.
    try:
        page.wait_for_selector('input[name="OWASP_CSRFTOKEN"]',
                               state="attached", timeout=30_000)
    except PlaywrightTimeout:
        raise F.SessionExpiredError(
            "Search form never rendered — F5 challenge or session expiry"
        )


def orestar_count(page, filer_id: str, start: date, end: date) -> int | None:
    """How many records ORESTAR holds for this filer over this range.

    Returns None if the count could not be read, which the caller must treat as
    "unknown" — recording it as 0 would mark a committee complete on the
    strength of a parse failure.
    """
    _return_to_form(page)
    page.fill('input[name="cneSearchFilerCommitteeId"]', str(filer_id))
    page.wait_for_timeout(250)
    page.fill('input[name="cneSearchTranStartDate"]', start.strftime("%m/%d/%Y"))
    page.fill('input[name="cneSearchTranEndDate"]', end.strftime("%m/%d/%Y"))
    page.click('input[name="search"]')
    try:
        page.wait_for_url(F.RESULTS_URL_PATTERN, timeout=30_000)
    except PlaywrightTimeout:
        log.warning("Timed out reading count for filer %s", filer_id)
        return None

    if "secure.sos.state.or.us/orestar" not in page.url:
        raise F.SessionExpiredError(f"Session expired — redirected to {page.url}")

    text = page.inner_text("body")
    m = re.search(r"([\d,]+)\s+records found", text)
    if not m:
        # "No records found" is a real answer, not a failure.
        if "no records found" in text.lower():
            return 0
        return None
    return int(m.group(1).replace(",", ""))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report() -> int:
    if not SURVEY_PATH.exists():
        log.error("No %s yet — run the survey first.", SURVEY_PATH)
        return 1
    rows = json.loads(SURVEY_PATH.read_text())
    short = [r for r in rows if (r.get("missing") or 0) > 0]
    clean = [r for r in rows if (r.get("missing") or 0) <= 0]
    d_short = sum(abs(r.get("delta") or 0) for r in short)
    d_clean = sum(abs(r.get("delta") or 0) for r in clean)

    print()
    print(f"  committees surveyed          : {len(rows):,}")
    print(f"  MISSING ROWS (backfill helps): {len(short):,}   ${d_short:,.0f} of delta")
    print(f"  complete (backfill cannot)   : {len(clean):,}   ${d_clean:,.0f} of delta")
    print(f"  rows missing in total        : {sum(r['missing'] for r in short):,}")
    if short:
        print()
        print(f"    {'filer':>7} {'ORESTAR':>9} {'held':>9} {'missing':>9} {'delta':>12}  name")
        for r in short[:25]:
            print(f"    {r['filer_id']:>7} {r['orestar']:>9,} {r['held']:>9,} "
                  f"{r['missing']:>9,} {(r.get('delta') or 0):>12,.0f}  {r['name'][:34]}")
    if clean:
        print()
        print("  largest deltas with NOTHING to fetch (a different problem):")
        for r in sorted(clean, key=lambda e: -abs(e.get("delta") or 0))[:10]:
            print(f"    {r['filer_id']:>7} {r['orestar']:>9,} held {r['held']:>9,} "
                  f"delta {(r.get('delta') or 0):>12,.0f}  {r['name'][:34]}")
    print()
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--filer-ids", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many committees (0 = until blocked)")
    ap.add_argument("--max-minutes", type=int, default=70,
                    help="stop after this long, so the job's own timeout never "
                         "arrives mid-run (0 = no budget)")
    ap.add_argument("--report", action="store_true", help="print findings, fetch nothing")
    ap.add_argument("--recheck", action="store_true",
                    help="re-survey committees already recorded")
    args = ap.parse_args()

    if args.report:
        return report()

    flagged = _flagged_committees()
    by_id = {str(r["filer_id"]): r for r in flagged}
    if args.filer_ids:
        targets = [str(f) for f in args.filer_ids]
    else:
        targets = [str(r["filer_id"]) for r in flagged]
    if not targets:
        log.info("Nothing flagged — nothing to survey.")
        return 0

    surveyed = {} if args.recheck else _load_survey()
    todo = [t for t in targets if t not in surveyed]
    log.info("%d committees flagged, %d already surveyed, %d to go",
             len(targets), len(targets) - len(todo), len(todo))
    if not todo:
        log.info("Survey complete for every flagged committee.")
        return report()

    held = _our_counts(todo)
    today = date.today()
    done_this_run = 0

    # Stop on our own terms, before the job's timeout stops us.
    #
    # A GitHub job timeout is reported as a cancellation, which skipped the
    # commit step and discarded 90 minutes and 96 committees of measured
    # progress. Finishing early and deliberately means the commit and the
    # chain both run normally.
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None

    with sync_playwright() as p:
        browser, context, page = F.setup_browser(p)
        consecutive_failures = 0
        for fid in todo:
            if args.limit and done_this_run >= args.limit:
                log.info("Reached --limit %d for this run.", args.limit)
                break
            if deadline and time.monotonic() >= deadline:
                log.info("Reached the %d-minute budget after %d committees — "
                         "stopping so this run's progress is committed.",
                         args.max_minutes, done_this_run)
                break
            meta = by_id.get(fid, {})
            try:
                n = orestar_count(page, fid, date(FIRST_YEAR, 1, 1), today)
                consecutive_failures = 0
            except F.SessionExpiredError as exc:
                consecutive_failures += 1
                log.warning("Blocked at filer %s (%d/2): %s — restarting browser",
                            fid, consecutive_failures, exc)
                try:
                    browser.close()
                except Exception:
                    pass
                if consecutive_failures >= 2:
                    log.warning("Rate-limited — stopping. %d surveyed this run; "
                                "the next run continues from here.", done_this_run)
                    break
                browser, context, page = F.setup_browser(p)
                continue
            except Exception as exc:
                log.warning("Filer %s failed: %s", fid, exc)
                n = None

            if n is None:
                # Not recorded at all: an unknown count must never be filed as
                # a result, or the committee is written off on a parse failure.
                log.warning("Filer %s: no count read — leaving unsurveyed", fid)
                continue

            ours = held.get(fid, 0)
            surveyed[fid] = {
                "filer_id": fid,
                "name": meta.get("name", ""),
                "orestar": n,
                "held": ours,
                "missing": max(n - ours, 0),
                "delta": meta.get("delta"),
                "dormant": meta.get("dormant"),
                "checked": today.isoformat(),
            }
            _save_survey(surveyed)
            done_this_run += 1
            verdict = (f"MISSING {n - ours:,}" if n > ours else "complete")
            log.info("[%d] filer %s %-34s ORESTAR %7d  held %7d  %s",
                     done_this_run, fid, (meta.get("name") or "")[:34], n, ours, verdict)
            time.sleep(F.REQUEST_DELAY)
        try:
            browser.close()
        except Exception:
            pass

    log.info("Surveyed %d committees this run; %d of %d recorded overall.",
             done_this_run, len(surveyed), len(targets))
    return report()


if __name__ == "__main__":
    sys.exit(main())
