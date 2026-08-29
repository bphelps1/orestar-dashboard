#!/usr/bin/env python3
"""
diff_coverage.py — which ROWS do we hold that ORESTAR does not, and vice versa?

survey_coverage.py compares COUNTS. That is cheap — one search per committee —
and it is the right first pass, but it cannot answer the question it appears to
answer, because the two sides are not counting the same thing.

ORESTAR's search returns superseded originals. This pipeline drops them: when
an amendment exists, the original it replaced is removed, which is correct and
is what `_drop_superseded` is for. So a committee that holds EVERY row it
should still reports fewer than ORESTAR, permanently. Oregon Firearms
Federation PAC holds 3,905 against ORESTAR's 3,906 and is not missing anything
at all — a backfill downloaded the full 3,906 and the merge correctly dropped
one superseded row straight back out.

That single fact broke a whole afternoon of analysis. "21 committees short 429
rows" was mostly this. Worse, the completeness test built on those counts has
now been wrong in both directions: `held >= orestar` wrongly certified
committees holding SURPLUS rows, and `held == orestar` wrongly rejects
committees where supersession simply worked. There is no correct count-based
test, because a count cannot distinguish "a row we should not have" from "a row
we correctly removed".

Comparing the IDENTITIES settles it. ORESTAR's results page prints a Tran ID
per row; diffing that set against ours yields two exact answers instead of one
ambiguous number:

    surplus  — ids we hold that ORESTAR does not return.
               Withdrawn or superseded filings. Nothing removes these today,
               so our store drifts upward invisibly. Plumbers & Steamfitters
               PAC held 16, worth $32,284.04, and surveyed as "missing: 0".

    missing  — ids ORESTAR returns that we do not hold.
               Genuinely absent; the backfill can recover these.

The cost is real and is the reason this is a separate tool rather than the
default. The count survey is one search per committee; this pages the entire
result set, fifty rows at a time. Reserve it for committees where the answer
changes a decision.

ORESTAR caps the results UI at 100 pages — 5,000 rows — the same class of limit
as the 4,999-row export cap. Past that the Next button simply stops, silently,
so a naive pager returns 5,000 of 11,766 and the diff invents thousands of
phantom "surplus" rows. Windows are therefore split until each reports under
the cap, and a window whose collected total falls short of its own reported
count is treated as FAILED rather than merged: a partial collection is worse
than none, because it looks like an answer.

Usage:
    python scraper/diff_coverage.py --filer-ids 221 19050
    python scraper/diff_coverage.py --flagged --limit 20
    python scraper/diff_coverage.py --report
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright

import fetch as F
import survey_coverage as SC
import supabase_sync

DATA_DIR = Path(__file__).parent.parent / "data"
DIFF_PATH = DATA_DIR / "coverage_diff.json"

# Two re-checks of committees whose withdrawn rows are moving a balance for
# every one committee measured for the first time. Any finite ratio prevents
# starvation; this one says re-checking is twice as urgent as new coverage
# without ever letting new coverage stop.
RECHECK_PER_NEW = 2

# A blocked results page is a runner-wide condition, not fifty independent
# committee failures. Two consecutive unusable committees are enough to stop
# the slice, commit what was learned, and leave a real cooldown before the next
# scheduled attempt.
MAX_CONSECUTIVE_FAILURES = 2

# ORESTAR shows 50 rows per page and stops offering "Next" after 100 of them.
# Not documented anywhere; measured by paging filer 221 and getting exactly
# 5,000 of 5,266 rows with the button quietly disabled.
PAGE_ROWS = 50
UI_ROW_CAP = 5_000

# The failed runs made roughly 600 paginated requests through two very large
# committees before F5 stopped rendering counts for about eighteen minutes.
# Pace both searches and Next clicks below that observed burst rate. Throughput
# is secondary here: a fast partial answer is deliberately not an answer.
ORESTAR_REQUEST_DELAY = max(3.0, F.REQUEST_DELAY)

log = logging.getLogger("diff_coverage")


# ---------------------------------------------------------------------------
# Local side
# ---------------------------------------------------------------------------

def _held_ids(filer_id: str, start: date, end: date) -> tuple[set[str], set[str]]:
    """(ids we hold, ids we deliberately dropped as superseded).

    The second set is what makes this tool worth its cost. ORESTAR's search
    still returns an original after an amendment replaces it; we drop that
    original on purpose. A plain identity diff would therefore report it as
    MISSING — the same false signal the count comparison gives, just with an id
    attached to it.

    An original we correctly dropped is recognisable: some amendment we DO hold
    names it in original_id. Anything ORESTAR returns that we lack and that no
    amendment of ours points at is genuinely absent.
    """
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """select tran_id from transactions
                   where filer_id = %s and tran_date >= %s and tran_date <= %s""",
                (filer_id, start, end),
            )
            held = {str(r[0]) for r in cur.fetchall()}
            cur.execute(
                """select distinct original_id from transactions
                   where filer_id = %s and tran_date >= %s and tran_date <= %s
                     and original_id is not null and original_id <> tran_id""",
                (filer_id, start, end),
            )
            superseded = {str(r[0]) for r in cur.fetchall()}
    finally:
        conn.close()
    return held, superseded


# ---------------------------------------------------------------------------
# ORESTAR side
# ---------------------------------------------------------------------------

def _parse_rows(text: str) -> dict[str, dict]:
    """Tran IDs off a results page.

    The table is tab separated:
        Tran ID | Tran Date | Status | Filer | Contributor/Payee | Sub Type | Amount

    Keyed on the leading integer and a trailing "$" amount so headers, footers
    and the site's navigation chrome cannot be mistaken for data.
    """
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = [c.strip() for c in line.split("\t")]
        if len(parts) < 7:
            continue
        tid, amount = parts[0], parts[-1]
        if not tid.isdigit():
            continue
        # ORESTAR renders negatives in accounting parentheses: "($68.50)", not
        # "-$68.50". Requiring a leading "$" therefore drops every negative row
        # silently — one Cash Balance Adjustment was enough to make filer 3865
        # collect 3,905 of 3,906 and be refused as unusable. A parser that
        # skips rows it does not recognise must be able to say so; this one
        # could not, and only the collected-vs-reported guard caught it.
        neg = amount.startswith("(") and amount.endswith(")")
        if neg:
            amount = amount[1:-1]
        if not amount.startswith("$"):
            continue
        try:
            amt = float(amount[1:].replace(",", ""))
        except ValueError:
            continue
        if neg:
            amt = -amt
        out[tid] = {"date": parts[1], "status": parts[2],
                    "payee": parts[4], "sub_type": parts[5], "amount": amt}
    return out


def _wait_for_new_rows(page, seen: set[str], timeout_seconds: int = 20) -> dict[str, dict]:
    """Wait until the results table contains at least one previously unseen ID.

    A fixed sleep after clicking Next is not a page-change guarantee. Under
    load the old, non-empty page can remain visible for several seconds; reading
    it again silently loses a page while still looking like a successful parse.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = _parse_rows(page.inner_text("body"))
        if current and (not seen or set(current) - seen):
            return current
        page.wait_for_timeout(250)
    return {}


def _collect_window(page, filer_id: str, start: date, end: date) -> dict | None:
    """Every row ORESTAR returns for one window, or None if it cannot be trusted.

    None means "do not use this window", and the caller must not treat it as an
    empty result. Half a result set looks exactly like a committee that has
    fewer rows than it does, and that is how a diff manufactures surplus.
    """
    # Recursive windows issue searches too; pacing only Next clicks leaves a
    # burst at every split boundary.
    page.wait_for_timeout(int(ORESTAR_REQUEST_DELAY * 1000))
    reported = SC.orestar_count(page, filer_id, start, end)
    if reported is None:
        log.warning("Filer %s %s→%s: no record count read", filer_id, start, end)
        return None
    if reported == 0:
        return {"reported": 0, "rows": {}}
    if reported > UI_ROW_CAP:
        return {"reported": reported, "rows": None}      # caller must split

    first_page = _wait_for_new_rows(page, set())
    if not first_page:
        log.warning("Filer %s %s→%s: first result page never rendered",
                    filer_id, start, end)
        return None

    rows: dict[str, dict] = dict(first_page)
    max_pages = (reported + PAGE_ROWS - 1) // PAGE_ROWS
    pages_read = 1
    while len(rows) < reported and pages_read < max_pages:
        nxt = [b for b in page.query_selector_all('input[value="Next"]') if b.is_enabled()]
        if not nxt:
            break
        try:
            nxt[0].click()
            # This is both polite pacing and the minimum wait before polling the
            # content. The poll below, rather than this delay, proves progress.
            page.wait_for_timeout(int(ORESTAR_REQUEST_DELAY * 1000))
        except Exception as e:                            # noqa: BLE001
            log.warning("Filer %s %s→%s: paging stopped (%s)", filer_id, start, end, e)
            return None
        current = _wait_for_new_rows(page, set(rows))
        if not current:
            log.warning("Filer %s %s→%s: Next produced no new rows after page %d",
                        filer_id, start, end, pages_read)
            return None
        rows.update(current)
        pages_read += 1

    if len(rows) != reported:
        log.warning("Filer %s %s→%s: collected %d of %d — window UNUSABLE",
                    filer_id, start, end, len(rows), reported)
        return None
    return {"reported": reported, "rows": rows}


def _split(start: date, end: date) -> list[tuple[date, date]]:
    """Halve a window. Returns [] when it can no longer be divided."""
    if start >= end:
        return []
    # Integer day arithmetic deliberately allows mid == start. That is the
    # correct split for a two-day window: [day one], [day two].
    mid = start + timedelta(days=(end - start).days // 2)
    return [(start, mid), (mid + timedelta(days=1), end)]


def orestar_ids(page, filer_id: str, start: date, end: date,
                depth: int = 0) -> dict | None:
    """Every Tran ID ORESTAR holds for this filer, splitting past the UI cap."""
    win = _collect_window(page, filer_id, start, end)
    if win is None:
        return None
    if win["rows"] is not None:
        return win["rows"]

    halves = _split(start, end)
    if not halves:
        # A single day over the cap. Nothing here can divide it further, and
        # returning what fits would be a lie, so refuse.
        log.error("Filer %s %s: %d rows in one indivisible window — cannot diff",
                  filer_id, start, win["reported"])
        return None
    log.info("Filer %s %s→%s: %d rows, over the %d cap — splitting",
             filer_id, start, end, win["reported"], UI_ROW_CAP)
    merged: dict[str, dict] = {}
    for a, b in halves:
        part = orestar_ids(page, filer_id, a, b, depth + 1)
        if part is None:
            return None                                   # one bad half poisons the whole
        merged.update(part)
    return merged


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _costs(filer_ids: list[str]) -> dict[str, int]:
    """Rows we hold per committee — a good proxy for what measuring one costs.

    ORESTAR is paged fifty rows at a time and every window over the cap costs
    an extra search plus a full re-page of both halves, so cost is superlinear
    in size. Our own row count is close enough to rank by and free to obtain.
    """
    if not filer_ids:
        return {}
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""select filer_id, count(*) from transactions
                           where filer_id = any(%s) group by 1""", (list(filer_ids),))
            return {str(r[0]): int(r[1]) for r in cur.fetchall()}
    finally:
        conn.close()


def _prioritise(targets: list[dict], entries: dict) -> list[dict]:
    """Order committees so the scarce request budget lands where it matters.

    A diff going stale is not uniformly harmful, and that decides the order.

    Where withdrawn rows are currently being SUBTRACTED from a balance, an
    out-of-date answer actively moves a number: if a filer re-files a
    transaction ORESTAR starts counting it again, and until we re-check we keep
    excluding it. Those want re-checking soonest.

    A committee never measured costs only the information we do not yet have.
    One measured clean has nothing being subtracted, so a stale answer there
    changes no figure at all, and it goes last.

    But a strict priority order STARVES. Re-checking a committee only moves it
    to the back of its own group rather than out of it, so the withdrawn group
    never empties — and once it grows past what a day of runs can process,
    committees never measured are never reached, permanently. The survey puts
    roughly 126 of 691 committees in that group, so that is the steady state,
    not an edge case.

    So the two urgent groups are INTERLEAVED at a fixed ratio instead: two
    re-checks for every one first measurement. Both make progress regardless of
    how large either grows, and the ratio decides how fast — not whether.

    Deliberately NOT an expiry rule. An earlier draft ignored the withdrawn
    list once it was older than the summary being compared, which would have
    re-included Plumbers & Steamfitters PAC's sixteen correctly withdrawn rows
    — re-flagging it for $32,284.04 — because of the calendar, with no evidence
    anything had changed. Age decides what to RE-MEASURE. Only a measurement
    changes an answer.
    """
    recheck, failed, fresh, lazy = [], [], [], []
    today = date.today().isoformat()
    for t in targets:
        e = entries.get(str(t["filer_id"]))
        if not e:
            fresh.append(t)
            continue
        if e.get("complete") is None:
            attempted = e.get("last_attempt") or e.get("checked") or ""
            # A chained successor is not a cooldown. Retrying the same refusal
            # again minutes later recreates the cascade under a new run ID.
            # Manual targeted runs without --recheck can still force a retry;
            # the rolling sweep waits until the next day.
            if attempted >= today:
                continue
            failed.append((attempted, t))
            continue
        # A chain is several slices of one sweep, not permission to remeasure
        # the same expensive committee in every slice. checked is the last
        # usable measurement; last_attempt also suppresses a same-day retry
        # after a failed recheck whose prior evidence was preserved.
        if (e.get("last_attempt") or e.get("checked") or "") >= today:
            continue
        if e.get("surplus"):
            recheck.append((e.get("checked") or "", t))
        else:
            lazy.append((e.get("checked") or "", t))
    # Order by COST inside each tier, cheapest first.
    #
    # The interleave above balances committee COUNTS, and that is not the same
    # as balancing work. Friends of Tina Kotek is 29,268 rows and needs fifteen
    # recursive splits; the median flagged committee is eighty rows and needs
    # one search. Two re-checks of the giants is not "two committees" of
    # budget, it is the entire budget — and because they carry withdrawn rows
    # they sort first every single day.
    #
    # Measured, not assumed: a 57-minute run spent every minute inside filers
    # 4792 and 19050, measured three committees, tripped the F5 block and
    # stopped the chain. At three per run the remaining 620 need roughly 200
    # days. The count-based interleave fixed starvation on the count axis and
    # left it untouched on the cost axis.
    #
    # Cheapest-first inverts that: 647 of 668 flagged committees fit in one
    # window with no splitting at all.
    _cost = _costs([str(t["filer_id"]) for t in targets])
    def _c(t):
        return _cost.get(str(t["filer_id"]), 0)
    recheck.sort(key=lambda x: (x[0], _c(x[1])))   # oldest evidence, then cheapest
    failed.sort(key=lambda x: (x[0], _c(x[1])))
    fresh.sort(key=_c)                             # pure coverage: cheapest first
    lazy.sort(key=lambda x: (x[0], _c(x[1])))

    # Recovery attempts and first measurements alternate. One deterministic
    # bad filer cannot starve new coverage, while transient F5 casualties are
    # still retried on the next eligible daily run after a real cooldown.
    work: list[dict] = []
    fi = ni = 0
    fq = [t for _, t in failed]
    while fi < len(fq) or ni < len(fresh):
        if fi < len(fq):
            work.append(fq[fi]); fi += 1
        if ni < len(fresh):
            work.append(fresh[ni]); ni += 1

    out: list[dict] = []
    ri = wi = 0
    rq = [t for _, t in recheck]
    while ri < len(rq) or wi < len(work):
        for _ in range(RECHECK_PER_NEW):      # two re-checks...
            if ri < len(rq):
                out.append(rq[ri]); ri += 1
        if wi < len(work):                    # ...then one recovery/new item
            out.append(work[wi]); wi += 1
    out = out + [t for _, t in lazy]

    # At most one over-cap committee per slice.
    #
    # A committee past UI_ROW_CAP cannot be answered by a single search: every
    # window is split and re-paged, so one of them can cost more requests than
    # a hundred ordinary committees and is the most likely thing to trip F5.
    # Letting one through per run keeps the giants genuinely re-checked —
    # they are where withdrawn rows actually live — without letting them own
    # the budget. The rest move to the back rather than being dropped.
    big = [t for t in out if _c(t) > UI_ROW_CAP]
    small = [t for t in out if _c(t) <= UI_ROW_CAP]
    return (big[:1] + small + big[1:]) if big else out


def _load() -> dict:
    if not DIFF_PATH.exists():
        return {}
    try:
        return {e["filer_id"]: e for e in json.loads(DIFF_PATH.read_text())}
    except Exception:                                     # noqa: BLE001
        return {}


def _save(entries: dict) -> None:
    DIFF_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(entries.values(), key=lambda e: -(len(e.get("surplus") or [])))
    DIFF_PATH.write_text(json.dumps(rows, indent=1))


def _record_failure(entries: dict, target: dict, reason: str) -> dict:
    """Record an unusable attempt without destroying earlier usable evidence."""
    fid = str(target["filer_id"])
    prior = entries.get(fid) or {}
    entry = dict(prior)
    if prior.get("complete") is None:
        # Legacy failures wrote checked even though nothing was checked. Keep
        # attempt timing separate so downstream freshness cannot mistake this
        # refusal for a measurement.
        entry.pop("checked", None)
        entry.update({"filer_id": fid, "complete": None})
    else:
        entry.setdefault("filer_id", fid)
    entry["name"] = target.get("name") or entry.get("name", "")
    entry["last_attempt"] = date.today().isoformat()
    entry["last_failure"] = reason
    entry["failure_count"] = int(entry.get("failure_count") or 0) + 1
    entries[fid] = entry
    return entry


def report() -> int:
    entries = _load()
    if not entries:
        log.error("No %s yet — run the diff first.", DIFF_PATH)
        return 1
    rows = list(entries.values())
    ok = [r for r in rows if r.get("complete") is not None]
    sur = [r for r in ok if r.get("surplus")]
    mis = [r for r in ok if r.get("missing")]
    sup = [r for r in ok if r.get("superseded")]
    clean = [r for r in ok if not r.get("surplus") and not r.get("missing")]
    print()
    print(f"  committees diffed          : {len(ok):,}")
    print(f"  exact match                : {len(clean):,}")
    print(f"  hold rows ORESTAR does not : {len(sur):,}   "
          f"{sum(len(r['surplus']) for r in sur):,} rows")
    print(f"  missing rows ORESTAR has   : {len(mis):,}   "
          f"{sum(len(r['missing']) for r in mis):,} rows")
    print(f"  superseded (correctly gone): {len(sup):,}   "
          f"{sum(len(r['superseded']) for r in sup):,} rows")
    # Progress against the set that actually matters.
    #
    # The rolling re-check has no natural finish — by design, since a committee
    # measured last week can change tomorrow. But "diff every filer with a
    # remaining discrepancy" DOES have one, and without this the only way to
    # know whether it had been reached was to count JSON entries by hand.
    try:
        flagged = {str(f["filer_id"]) for f in SC._flagged_committees()}
    except Exception:                                     # noqa: BLE001
        flagged = set()
    if flagged:
        usable_ids = {f for f, e in entries.items() if e.get("complete") is not None}
        seen = flagged & usable_ids
        todo = flagged - usable_ids
        stale = sorted((e.get("checked") or "") for f, e in entries.items()
                       if f in seen and e.get("checked"))
        print(f"\n  flagged committees         : {len(flagged):,}")
        print(f"    measured at least once   : {len(seen):,}  ({len(seen)/len(flagged)*100:.0f}%)")
        print(f"    never measured           : {len(todo):,}")
        if stale:
            print(f"    oldest measurement       : {stale[0]}")

    failed = [r for r in rows if r.get("complete") is None]
    if failed:
        print(f"  could not be diffed        : {len(failed):,}  "
              f"(windows incomplete — NOT counted as clean)")
    if sur:
        print("\n  largest surplus:")
        for r in sorted(sur, key=lambda r: -len(r["surplus"]))[:15]:
            print(f"    {r['filer_id']:<8}{r.get('name','')[:36]:<36}"
                  f"+{len(r['surplus']):<5}(ORESTAR {r['orestar']:,} held {r['held']:,})")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--filer-ids", nargs="*", default=None)
    ap.add_argument("--flagged", action="store_true",
                    help="diff every currently-flagged committee")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start-year", type=int, default=2006)
    ap.add_argument("--max-minutes", type=int, default=70,
                    help="stop on our own terms, before the job timeout does it for us")
    ap.add_argument("--recheck", action="store_true",
                    help="re-diff committees already recorded")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%H:%M:%S")
    if args.report:
        return report()

    if args.filer_ids:
        targets = [{"filer_id": f, "name": ""} for f in args.filer_ids]
    elif args.flagged:
        targets = SC._flagged_committees()
    else:
        log.error("Give --filer-ids or --flagged.")
        return 2

    entries = _load()
    if not args.recheck:
        # A refusal is not a measurement. Retry first-attempt failures rather
        # than treating the mere presence of a JSON object as completed work.
        targets = [t for t in targets
                   if (entries.get(str(t["filer_id"])) or {}).get("complete") is None]
    elif not args.filer_ids:
        # The rolling flagged sweep needs staleness ordering and same-day
        # suppression. An explicit --filer-ids --recheck is a deliberate force
        # request (and is useful for a small post-deploy canary), so preserve
        # exactly the caller's list instead of silently filtering it.
        targets = _prioritise(targets, entries)
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        log.info("Nothing to diff.")
        return 0

    start, end = date(args.start_year, 1, 1), date.today()
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    done = 0
    attempted = 0
    unusable = 0
    consecutive_failures = 0
    blocked = False

    with sync_playwright() as p:
        browser, _ctx, page = F.setup_browser(p)
        try:
            for t in targets:
                if deadline and time.monotonic() > deadline:
                    log.info("Time budget reached — stopping with %d diffed.", done)
                    break
                fid = str(t["filer_id"])
                log.info("=== Diffing filer %s %s ===", fid, t.get("name", ""))
                attempted += 1
                failure_reason = "unusable_window"
                try:
                    theirs = orestar_ids(page, fid, start, end)
                except F.SessionExpiredError as exc:
                    # survey_coverage already treats this as a recoverable
                    # runner/session condition. Keep the same semantics here
                    # instead of failing the job before RUN_RESULT is emitted.
                    log.warning("Filer %s: session expired (%s)", fid, exc)
                    failure_reason = "session_expired"
                    theirs = None
                if theirs is None:
                    # Preserve a previous usable result on a failed recheck. Its
                    # checked date remains the date of the evidence, while this
                    # attempt is recorded separately. First attempts remain an
                    # explicit unknown and override unsafe count-only evidence.
                    _record_failure(entries, t, failure_reason)
                    _save(entries)
                    unusable += 1
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        blocked = True
                        log.warning("%d committees in a row unusable — stopping this "
                                    "runner before the F5 cooldown becomes a null cascade.",
                                    consecutive_failures)
                        break
                    # A broken browser/session should not poison the next
                    # committee. If the block is IP-wide the next refusal will
                    # trip the runner breaker; if it is session-local this
                    # gives the collector one clean chance to recover.
                    log.warning("Restarting the browser after an unusable result.")
                    try:
                        browser.close()
                    except Exception:                    # noqa: BLE001
                        pass
                    browser, _ctx, page = F.setup_browser(p)
                    continue
                consecutive_failures = 0
                ours, superseded_by_us = _held_ids(fid, start, end)
                surplus = sorted(ours - set(theirs))
                absent = set(theirs) - ours
                # Split what ORESTAR has and we do not into the two cases that
                # look identical to a count and mean opposite things.
                superseded = sorted(absent & superseded_by_us)
                missing = sorted(absent - superseded_by_us)
                entries[fid] = {
                    "filer_id": fid,
                    "name": t.get("name", ""),
                    "orestar": len(theirs),
                    "held": len(ours),
                    # True only when the identities agree exactly. Unlike a
                    # count, this cannot be satisfied by a surplus cancelling a
                    # shortfall — the failure that made the count survey report
                    # Plumbers & Steamfitters PAC as "missing: 0" while it held
                    # sixteen rows ORESTAR had withdrawn.
                    "complete": not surplus and not missing,
                    "surplus": surplus,
                    "missing": missing,
                    # Rows ORESTAR still returns that we dropped on purpose.
                    # Recorded so the count is explainable rather than merely
                    # excused: held + superseded should equal ORESTAR's total.
                    "superseded": superseded,
                    "checked": date.today().isoformat(),
                }
                log.info("Filer %s: ORESTAR %d, held %d, surplus %d, missing %d, "
                         "superseded %d", fid, len(theirs), len(ours),
                         len(surplus), len(missing), len(superseded))
                _save(entries)
                done += 1
        finally:
            browser.close()

    log.info("Diffed %d committees this run.", done)
    log.info("Attempted %d committees; usable %d; unusable %d; blocked %s",
             attempted, done, unusable, "yes" if blocked else "no")
    # Stable machine-readable line for the workflow's chain guard.
    print(f"RUN_RESULT attempted={attempted} usable={done} "
          f"unusable={unusable} blocked={1 if blocked else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
