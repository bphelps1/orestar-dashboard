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
default. The count survey is one search per committee; this exports every
provably complete leaf and may need many disjoint searches to get below the
export cap. Reserve it for committees where the answer changes a decision.

ORESTAR caps the results UI at 100 pages — 5,000 rows — and its Excel export at
4,999 rows. Windows are therefore split until each reports at most the export
cap, then downloaded once. Every leaf is checked against its reported count,
and every group of children is reconciled against its parent. A short or
overlapping collection is treated as FAILED rather than merged: a partial
collection is worse than none, because it looks like an answer.

Usage:
    python scraper/diff_coverage.py --filer-ids 221 19050
    python scraper/diff_coverage.py --flagged --limit 20
    python scraper/diff_coverage.py --report
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import Error as PlaywrightError
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

# Local dates are only a routing hint.  Keeping seeded windows below the UI cap
# leaves room for rows that ORESTAR has and our database does not, while every
# seeded window still covers a contiguous piece of the original date range.
SEED_TARGET_ROWS = 4_000

log = logging.getLogger("diff_coverage")

Window = tuple[str, date, date, str | None, str | None, str | None]


class CollectionDeadlineExceeded(RuntimeError):
    """The current committee exhausted the run budget before it was provable."""


class PartitionMismatchError(RuntimeError):
    """Disjoint child searches did not reproduce their parent's true count."""


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise CollectionDeadlineExceeded("coverage-diff time budget reached")


def _remaining_timeout_ms(deadline: float | None, ceiling_ms: int) -> int:
    """A blocking-call timeout that cannot extend past ``deadline``."""
    if deadline is None:
        return ceiling_ms
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise CollectionDeadlineExceeded("coverage-diff time budget reached")
    return min(ceiling_ms, remaining)


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


def _wait_for_new_rows(
    page,
    seen: set[str],
    timeout_seconds: int = 20,
    deadline: float | None = None,
) -> dict[str, dict]:
    """Wait until the results table contains at least one previously unseen ID.

    A fixed sleep after clicking Next is not a page-change guarantee. Under
    load the old, non-empty page can remain visible for several seconds; reading
    it again silently loses a page while still looking like a successful parse.
    """
    _check_deadline(deadline)
    poll_deadline = time.monotonic() + timeout_seconds
    if deadline is not None:
        poll_deadline = min(poll_deadline, deadline)
    while time.monotonic() < poll_deadline:
        current = _parse_rows(page.inner_text("body"))
        if current and (not seen or set(current) - seen):
            return current
        page.wait_for_timeout(250)
    _check_deadline(deadline)
    return {}


def _window_label(window: Window) -> str:
    tran_type, start, end, amt_from, amt_to, payee_prefix = window
    bits = [f"{tran_type} {start}→{end}"]
    if amt_from is not None or amt_to is not None:
        bits.append(f"${amt_from or '0'}-{amt_to or 'max'}")
    if payee_prefix:
        bits.append(f"payee~{payee_prefix}*")
    return " ".join(bits)


def _parse_export_rows(content: bytes) -> dict[str, dict] | None:
    """Read transaction IDs from an ORESTAR Excel export in memory."""
    if content[:4] == F._XLS_MAGIC:
        engine = "xlrd"
    elif content[:4] == F._XLSX_MAGIC:
        engine = "openpyxl"
    else:
        return None
    try:
        import pandas as pd

        frame = pd.read_excel(io.BytesIO(content), engine=engine, dtype=str)
    except Exception as exc:                               # noqa: BLE001
        log.warning("Could not parse ORESTAR Excel export (%s)", exc)
        return None
    columns = {str(column).strip().lower(): column for column in frame.columns}
    id_column = columns.get("tran id") or columns.get("transaction id")
    if id_column is None:
        log.warning("ORESTAR Excel export has no transaction-ID column")
        return None
    rows: dict[str, dict] = {}
    for raw_id in frame[id_column].dropna():
        tran_id = str(raw_id).strip()
        if tran_id.endswith(".0") and tran_id[:-2].isdigit():
            tran_id = tran_id[:-2]
        if tran_id.isdigit():
            rows[tran_id] = {}
    return rows


def _export_rows(
    page,
    context,
    filer_id: str,
    label: str,
    deadline: float | None = None,
) -> dict[str, dict] | None:
    """Download the current result set once instead of paging it 50 rows at a time."""
    try:
        csrf = page.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href*="OWASP_CSRFTOKEN"]')];
            if (!links.length) return null;
            const m = links[0].href.match(/OWASP_CSRFTOKEN=([^&"'\\s]+)/);
            return m ? m[1] : null;
        }""")
    except Exception as exc:                               # noqa: BLE001
        log.warning("Filer %s %s: could not read export token (%s)",
                    filer_id, label, exc)
        return None
    if not csrf:
        log.warning("Filer %s %s: no export token on results page", filer_id, label)
        return None

    results_url = page.url
    session_match = re.search(r";(JSESSIONID_ORESTAR=[^?&\s]+)", results_url)
    session_path = f";{session_match.group(1)}" if session_match else ""
    export_url = f"{F.EXPORT_URL}{session_path}?OWASP_CSRFTOKEN={csrf}"
    content = b""
    try:
        cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies()}
        response = F.requests.get(
            export_url,
            cookies=cookies,
            headers={"User-Agent": F.USER_AGENT, "Referer": results_url},
            timeout=max(0.001, _remaining_timeout_ms(deadline, 120_000) / 1000),
        )
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            content = response.content
    except Exception as exc:                               # noqa: BLE001
        log.warning("Filer %s %s: direct export failed (%s)", filer_id, label, exc)

    rows = _parse_export_rows(content)
    if rows is None:
        # ORESTAR occasionally rejects the cookie replay while allowing the same
        # export in the browser session.  Use Playwright's temporary download as
        # the fallback; nothing is written into the repository.
        try:
            with page.expect_download(
                timeout=_remaining_timeout_ms(deadline, 60_000)
            ) as download_info:
                try:
                    page.goto(
                        export_url,
                        wait_until="commit",
                        timeout=_remaining_timeout_ms(deadline, 60_000),
                    )
                except PlaywrightError as exc:
                    # A Content-Disposition response begins the download and
                    # aborts the document navigation. Playwright reports that
                    # successful hand-off as "Download is starting" even
                    # though expect_download captured the file.
                    if "Download is starting" not in str(exc):
                        raise
            content = Path(download_info.value.path()).read_bytes()
        except Exception as exc:                           # noqa: BLE001
            log.warning("Filer %s %s: browser export failed (%s)",
                        filer_id, label, exc)
            return None
        rows = _parse_export_rows(content)
    return rows


def _read_current_results_count(
    page,
    deadline: float | None,
    timeout_seconds: int = 20,
) -> int | None:
    """Read the already-rendering results count without issuing a new search."""
    _check_deadline(deadline)
    poll_deadline = time.monotonic() + timeout_seconds
    if deadline is not None:
        poll_deadline = min(poll_deadline, deadline)
    while time.monotonic() < poll_deadline:
        text = page.inner_text(
            "body", timeout=_remaining_timeout_ms(deadline, 30_000)
        )
        match = re.search(r"([\d,]+)\s+records found", text)
        if match:
            return int(match.group(1).replace(",", ""))
        if "no records found" in text.lower():
            return 0
        page.wait_for_timeout(250)
    _check_deadline(deadline)
    return None


def _goto_tran_id_sort(
    page,
    filer_id: str,
    label: str,
    direction: str,
    expected_count: int,
    deadline: float | None,
) -> bool:
    """Sort the current exact result set by its unique transaction ID."""
    selector = (
        f'a[href*="by=RSN"][href*="srtOrder={direction}"]'
    )
    try:
        _check_deadline(deadline)
        links = page.locator(selector)
        if links.count() == 0:
            log.warning(
                "Filer %s %s: no Tran ID %s sort link on results page",
                filer_id, label, direction,
            )
            return False
        href = links.first.get_attribute("href")
        if not href:
            log.warning(
                "Filer %s %s: empty Tran ID %s sort link",
                filer_id, label, direction,
            )
            return False
        # Sorting is another ORESTAR results request.  Pace it like a search;
        # the point of this fast path is to avoid the prefix burst, not replace
        # it with a smaller unpaced burst.
        page.wait_for_timeout(int(ORESTAR_REQUEST_DELAY * 1000))
        _check_deadline(deadline)
        page.goto(
            urljoin(page.url, href),
            wait_until="domcontentloaded",
            timeout=_remaining_timeout_ms(deadline, 60_000),
        )
        _check_deadline(deadline)
    except CollectionDeadlineExceeded:
        raise
    except Exception as exc:                               # noqa: BLE001
        log.warning(
            "Filer %s %s: Tran ID %s sort failed (%s)",
            filer_id, label, direction, exc,
        )
        return False

    if "secure.sos.state.or.us/orestar" not in page.url:
        log.warning(
            "Filer %s %s: Tran ID %s sort left ORESTAR (%s)",
            filer_id, label, direction, page.url,
        )
        return False
    rendered_count = _read_current_results_count(page, deadline)
    if rendered_count != expected_count:
        log.warning(
            "Filer %s %s: Tran ID %s sort rendered %s rows, expected %d",
            filer_id,
            label,
            direction,
            "unknown" if rendered_count is None else rendered_count,
            expected_count,
        )
        return False
    log.info(
        "Filer %s %s: Tran ID %s sort preserved the %d-row parent",
        filer_id, label, direction, expected_count,
    )
    return True


def _export_tran_id_extremes(
    page,
    context,
    filer_id: str,
    label: str,
    expected_count: int,
    row_cap: int,
    deadline: float | None,
) -> dict[str, dict] | None:
    """Verified subset from the low and high ends of a capped parent.

    Tran ID is unique.  Therefore, when ``expected_count <= 2 * row_cap``,
    the first ``row_cap`` IDs in ascending order plus the first ``row_cap`` in
    descending order must cover the whole result set.  Each export is still
    validated independently, and the caller still accepts only an exact union.
    """
    if expected_count > 2 * row_cap:
        return None

    merged: dict[str, dict] = {}
    for direction in ("asc", "desc"):
        if not _goto_tran_id_sort(
            page, filer_id, label, direction, expected_count, deadline
        ):
            return merged or None
        rows = _export_rows(page, context, filer_id, label, deadline)
        if rows is None or len(rows) != row_cap:
            log.warning(
                "Filer %s %s: Tran ID %s export contained %s IDs, expected %d",
                filer_id,
                label,
                direction,
                "unknown" if rows is None else len(rows),
                row_cap,
            )
            return merged or None
        merged.update(rows)
    return merged


def _collect_window(
    page,
    filer_id: str,
    start: date,
    end: date,
    tran_type: str = "ALL",
    amt_from: str | None = None,
    amt_to: str | None = None,
    payee_prefix: str | None = None,
    deadline: float | None = None,
    context=None,
) -> dict | None:
    """Every row ORESTAR returns for one window, or None if it cannot be trusted.

    None means "do not use this window", and the caller must not treat it as an
    empty result. Half a result set looks exactly like a committee that has
    fewer rows than it does, and that is how a diff manufactures surplus.
    """
    window: Window = (tran_type, start, end, amt_from, amt_to, payee_prefix)
    label = _window_label(window)

    # Recursive windows issue searches too; pacing only Next clicks leaves a
    # burst at every split boundary.
    _check_deadline(deadline)
    page.wait_for_timeout(int(ORESTAR_REQUEST_DELAY * 1000))
    _check_deadline(deadline)
    try:
        reported = SC.orestar_count(
            page, filer_id, start, end, tran_type, amt_from, amt_to, payee_prefix,
            deadline=deadline,
        )
    except SC.SearchDeadlineExceeded as exc:
        raise CollectionDeadlineExceeded(str(exc)) from exc
    _check_deadline(deadline)
    if reported is None:
        log.warning("Filer %s %s: no record count read", filer_id, label)
        return None
    if reported == 0:
        return {"reported": 0, "rows": {}}
    # An export holds at most 4,999 data rows; the UI itself holds 5,000.  Split
    # at the lower limit so a completed leaf can be fetched in one request.
    row_cap = min(UI_ROW_CAP, F.ORESTAR_ROW_CAP)
    if reported > row_cap:
        return {"reported": reported, "rows": None}      # caller must split

    if context is not None:
        _check_deadline(deadline)
        rows = _export_rows(page, context, filer_id, label, deadline)
        _check_deadline(deadline)
        if rows is None or len(rows) != reported:
            log.warning("Filer %s %s: export contained %s of %d IDs — window UNUSABLE",
                        filer_id, label, "unknown" if rows is None else len(rows),
                        reported)
            return None
        return {"reported": reported, "rows": rows}

    first_page = _wait_for_new_rows(page, set(), deadline=deadline)
    if not first_page:
        log.warning("Filer %s %s: first result page never rendered", filer_id, label)
        return None

    rows: dict[str, dict] = dict(first_page)
    max_pages = (reported + PAGE_ROWS - 1) // PAGE_ROWS
    pages_read = 1
    while len(rows) < reported and pages_read < max_pages:
        _check_deadline(deadline)
        nxt = [b for b in page.query_selector_all('input[value="Next"]') if b.is_enabled()]
        if not nxt:
            break
        try:
            nxt[0].click()
            # This is both polite pacing and the minimum wait before polling the
            # content. The poll below, rather than this delay, proves progress.
            page.wait_for_timeout(int(ORESTAR_REQUEST_DELAY * 1000))
            _check_deadline(deadline)
        except Exception as e:                            # noqa: BLE001
            if isinstance(e, CollectionDeadlineExceeded):
                raise
            log.warning("Filer %s %s: paging stopped (%s)", filer_id, label, e)
            return None
        current = _wait_for_new_rows(page, set(rows), deadline=deadline)
        if not current:
            log.warning("Filer %s %s: Next produced no new rows after page %d",
                        filer_id, label, pages_read)
            return None
        rows.update(current)
        pages_read += 1

    if len(rows) != reported:
        log.warning("Filer %s %s: collected %d of %d — window UNUSABLE",
                    filer_id, label, len(rows), reported)
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


def _date_seed_windows(
    filer_id: str,
    start: date,
    end: date,
    target_rows: int = SEED_TARGET_ROWS,
) -> list[tuple[date, date]]:
    """Build complete, contiguous date windows from the local row distribution.

    The local rows decide only where to put boundaries; they never decide which
    dates to search.  The returned windows cover ``start`` through ``end`` with
    no gaps, so ORESTAR-only dates remain in scope.  Every parent and the final
    root are reconciled against ORESTAR's own count before the result is usable.
    """
    conn = supabase_sync._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """select tran_date, count(*) from transactions
                   where filer_id = %s and tran_date between %s and %s
                   group by tran_date order by tran_date""",
                (filer_id, start, end),
            )
            date_counts = cur.fetchall()
    finally:
        conn.close()

    normalized: list[tuple[date, int]] = []
    for raw_day, raw_count in date_counts:
        day = raw_day.date() if isinstance(raw_day, datetime) else raw_day
        if isinstance(day, str):
            day = date.fromisoformat(day[:10])
        normalized.append((day, int(raw_count)))
    return _build_date_seed_windows(start, end, normalized, target_rows)


def _build_date_seed_windows(
    start: date,
    end: date,
    date_counts: list[tuple[date, int]],
    target_rows: int = SEED_TARGET_ROWS,
) -> list[tuple[date, date]]:
    """Pure partition builder used by `_date_seed_windows` and its tests."""
    if sum(count for _, count in date_counts) <= target_rows:
        return []

    windows: list[tuple[date, date]] = []
    window_start = start
    running = 0
    one_day = timedelta(days=1)
    for day, count in date_counts:
        if count >= target_rows:
            if window_start < day:
                windows.append((window_start, day - one_day))
            windows.append((day, day))
            window_start = day + one_day
            running = 0
            continue
        if running and running + count > target_rows:
            windows.append((window_start, day - one_day))
            window_start = day
            running = count
        else:
            running += count
    if window_start <= end:
        windows.append((window_start, end))
    return windows if len(windows) > 1 else []


def _date_windows_cover(
    start: date, end: date, windows: list[tuple[date, date]]
) -> bool:
    expected = start
    for window_start, window_end in windows:
        if window_start != expected or window_end < window_start:
            return False
        expected = window_end + timedelta(days=1)
    return expected == end + timedelta(days=1)


def _is_prefix_partition(parent: Window, children: list[Window]) -> bool:
    """Whether ``children`` only add contributor-prefix filters to ``parent``."""
    return bool(children) and parent[5] is None and all(
        child[:5] == parent[:5] and child[5] is not None for child in children
    )


def _order_children_by_local_cost(
    filer_id: str, children: list[Window]
) -> list[Window]:
    """Run cheap disjoint siblings first and leave the request-heavy one last.

    Local counts are only a scheduling hint.  They never remove a child or
    participate in reconciliation, and an unavailable count preserves the
    narrowing ladder's original order.
    """
    costs: list[tuple[int, int, Window]] = []
    for index, child in enumerate(children):
        cost = F._held_rows(filer_id, *child)
        if cost is None:
            return children
        costs.append((int(cost), index, child))
    ordered = [child for _cost, _index, child in sorted(costs)]
    if ordered != children:
        log.info(
            "Filer %s: ordered %d child windows cheapest-first; "
            "largest local window (%d rows) runs last",
            filer_id, len(children), max(cost for cost, _index, _child in costs),
        )
    return ordered


def _collect_tree(
    page,
    context,
    filer_id: str,
    window: Window,
    deadline: float | None,
    depth: int,
    seed_windows: list[tuple[date, date]] | None,
) -> dict | None:
    """Collect one window and prove every descendant reconciles to its parent."""
    _check_deadline(deadline)
    tran_type, start, end, amt_from, amt_to, payee_prefix = window
    result = _collect_window(
        page, filer_id, start, end, tran_type, amt_from, amt_to, payee_prefix,
        deadline, context,
    )
    if result is None or result["rows"] is not None:
        return result

    children: list[Window]
    if depth == 0:
        if seed_windows is None:
            try:
                _check_deadline(deadline)
                seed_windows = _date_seed_windows(filer_id, start, end)
                _check_deadline(deadline)
            except CollectionDeadlineExceeded:
                raise
            except Exception as exc:                       # noqa: BLE001
                log.warning("Filer %s: local date seeding failed (%s); using ladder",
                            filer_id, exc)
                seed_windows = []
        if seed_windows:
            if not _date_windows_cover(start, end, seed_windows):
                raise PartitionMismatchError(
                    f"date seeds do not cover {start}→{end}"
                )
            children = [("ALL", a, b, None, None, None) for a, b in seed_windows]
        else:
            children = F._narrow_filer(*window)
    else:
        children = F._narrow_filer(*window)

    if not children:
        raise PartitionMismatchError(
            f"{_window_label(window)} has {result['reported']} rows after every "
            "narrowing dimension is spent"
        )

    prefix_partition = _is_prefix_partition(window, children)
    if context is not None and not prefix_partition:
        children = _order_children_by_local_cost(filer_id, children)

    log.info("Filer %s %s: %d rows, over the %d cap — narrowing into %d",
             filer_id, _window_label(window), result["reported"], UI_ROW_CAP,
             len(children))

    parent_reported = result["reported"]
    parent_sample: dict[str, dict] = {}
    row_cap = min(UI_ROW_CAP, F.ORESTAR_ROW_CAP)
    if context is not None and prefix_partition:
        # Prefixes are the only available split for a single-day/single-amount
        # batch, but dozens of count+export pairs can trip F5 near the end of
        # the alphabet.  Before walking them, sort the exact parent by its
        # unique Tran ID in both directions.  Two capped exports cover any
        # parent up to twice the export cap; otherwise each is still genuine
        # overlap evidence for the prefix fallback.
        #
        # This is a proof, not an estimate: every member of the union came from
        # this exact parent or one of its stricter filters, and a subset of an
        # N-item set with N unique members is the full set.  Anything short,
        # oversized, duplicated across processed children, or otherwise
        # inconsistent is still refused.
        _check_deadline(deadline)
        parent_sample = (
            _export_tran_id_extremes(
                page,
                context,
                filer_id,
                _window_label(window),
                parent_reported,
                row_cap,
                deadline,
            )
            or {}
        )
        _check_deadline(deadline)
        if parent_sample:
            if len(parent_sample) > parent_reported:
                raise PartitionMismatchError(
                    f"{_window_label(window)} opposite Tran ID exports have "
                    f"{len(parent_sample)} unique IDs, parent reports "
                    f"{parent_reported}"
                )
            log.info(
                "Filer %s %s: retained %d IDs from opposite Tran ID exports",
                filer_id, _window_label(window), len(parent_sample),
            )
            if len(parent_sample) == parent_reported:
                log.info(
                    "Filer %s %s: reconciled %d IDs from opposite Tran ID "
                    "exports; prefix leaves not needed",
                    filer_id, _window_label(window), parent_reported,
                )
                return {"reported": parent_reported, "rows": parent_sample}

        if not parent_sample:
            sample = _export_rows(
                page, context, filer_id, _window_label(window), deadline
            )
            _check_deadline(deadline)
            if sample is not None and len(sample) == row_cap:
                parent_sample = sample
                log.info(
                    "Filer %s %s: retained %d capped parent IDs as overlap evidence",
                    filer_id, _window_label(window), len(parent_sample),
                )
            else:
                log.warning(
                    "Filer %s %s: capped parent export contained %s IDs, expected "
                    "%d; ignoring the sample and requiring every child",
                    filer_id,
                    _window_label(window),
                    "unknown" if sample is None else len(sample),
                    row_cap,
                )

        if parent_sample and len(parent_sample) < row_cap:
            log.warning(
                "Filer %s %s: overlap evidence has only %d IDs, expected at "
                "least %d; ignoring it and requiring every child",
                filer_id, _window_label(window), len(parent_sample), row_cap,
            )
            parent_sample = {}

    child_rows: dict[str, dict] = {}
    child_reported = 0
    for child_index, child in enumerate(children, start=1):
        _check_deadline(deadline)
        part = _collect_tree(
            page, context, filer_id, child, deadline, depth + 1, seed_windows=[]
        )
        if part is None:
            return None
        child_reported += part["reported"]
        child_rows.update(part["rows"])
        if len(child_rows) != child_reported:
            raise PartitionMismatchError(
                f"{_window_label(window)} processed children report "
                f"{child_reported} rows / {len(child_rows)} unique IDs"
            )

        if parent_sample:
            merged = dict(parent_sample)
            merged.update(child_rows)
            if len(merged) > parent_reported:
                raise PartitionMismatchError(
                    f"{_window_label(window)} overlap evidence has "
                    f"{len(merged)} unique IDs, parent reports {parent_reported}"
                )
            if len(merged) == parent_reported:
                log.info(
                    "Filer %s %s: reconciled %d IDs after %d/%d prefix leaves",
                    filer_id, _window_label(window), parent_reported,
                    child_index, len(children),
                )
                return {"reported": parent_reported, "rows": merged}

    merged = dict(parent_sample)
    merged.update(child_rows)
    if len(merged) != parent_reported or (
        not parent_sample and child_reported != parent_reported
    ):
        raise PartitionMismatchError(
            f"{_window_label(window)} children report {child_reported} rows / "
            f"{len(child_rows)} unique child IDs / {len(merged)} with overlap "
            f"evidence, parent reports {parent_reported}"
        )
    return {"reported": parent_reported, "rows": merged}


def orestar_ids(
    page,
    filer_id: str,
    start: date,
    end: date,
    depth: int = 0,
    deadline: float | None = None,
    seed_windows: list[tuple[date, date]] | None = None,
    context=None,
    raise_partition_error: bool = False,
) -> dict | None:
    """Every ORESTAR Tran ID, or None unless the complete tree reconciles."""
    root: Window = ("ALL", start, end, None, None, None)
    try:
        result = _collect_tree(
            page, context, filer_id, root, deadline, depth, seed_windows=seed_windows
        )
    except PartitionMismatchError as exc:
        log.error("Filer %s: partition UNUSABLE (%s)", filer_id, exc)
        if raise_partition_error:
            raise
        return None
    return None if result is None else result["rows"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _costs(filer_ids: list[str]) -> dict[str, int]:
    """Rows we hold per committee — a good proxy for what measuring one costs.

    Every window over the cap costs extra searches and exports across the
    narrowing tree, so cost is superlinear in size. Our own row count is close
    enough to rank by and free to obtain.
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


def _target_name(entries: dict, target: dict) -> str:
    """Keep a known committee name when a targeted run supplies only its ID."""
    fid = str(target["filer_id"])
    return target.get("name") or (entries.get(fid) or {}).get("name", "")


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
    entry["name"] = _target_name(entries, target)
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

def _remediation_verification_failures(
    entries: dict,
    requested_ids,
    successful_ids: set[str],
) -> list[str]:
    """Explain why a targeted missing-ID remediation cannot be certified."""
    failures = []
    for fid in map(str, requested_ids):
        row = entries.get(fid) or {}
        missing = row.get("missing") or []
        if fid not in successful_ids:
            failures.append(f"{fid}: no fresh usable diff")
        elif missing:
            failures.append(f"{fid}: {len(missing)} missing IDs remain")
    return failures


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
    ap.add_argument(
        "--require-no-missing",
        action="store_true",
        help=("fail unless every explicitly targeted filer is freshly diffed "
              "and has no genuinely missing transaction IDs"),
    )
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%H:%M:%S")
    if args.report:
        return report()
    if args.require_no_missing and not args.filer_ids:
        ap.error("--require-no-missing requires explicit --filer-ids")
    if args.require_no_missing and not args.recheck:
        ap.error("--require-no-missing requires --recheck for fresh evidence")
    if args.require_no_missing and args.start_year != 2006:
        ap.error("--require-no-missing requires --start-year=2006")
    if args.limit < 0:
        ap.error("--limit must be zero or positive")
    if args.require_no_missing and args.limit:
        ap.error("--require-no-missing cannot be combined with --limit")

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
    budget_exhausted = False
    successful_ids: set[str] = set()

    with sync_playwright() as p:
        browser, _ctx, page = F.setup_browser_retrying(p)
        try:
            for t in targets:
                if deadline and time.monotonic() > deadline:
                    log.info("Time budget reached — stopping with %d diffed.", done)
                    break
                fid = str(t["filer_id"])
                log.info("=== Diffing filer %s %s ===", fid, t.get("name", ""))
                attempted += 1
                failure_reason = "unusable_window"
                deterministic_refusal = False
                try:
                    theirs = orestar_ids(
                        page, fid, start, end, deadline=deadline, context=_ctx,
                        raise_partition_error=True,
                    )
                except CollectionDeadlineExceeded:
                    log.warning("Filer %s: time budget reached before a complete, "
                                "reconciled result", fid)
                    failure_reason = "time_budget"
                    budget_exhausted = True
                    theirs = None
                except PartitionMismatchError as exc:
                    log.warning("Filer %s: deterministic partition refusal (%s)",
                                fid, exc)
                    failure_reason = "partition_mismatch"
                    deterministic_refusal = True
                    theirs = None
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
                    if budget_exhausted:
                        log.info("Time budget reached inside filer %s — stopping cleanly.",
                                 fid)
                        break
                    if deterministic_refusal:
                        # A proved gap/overlap is local to this partition tree,
                        # not evidence that the runner's browser or IP is blocked.
                        consecutive_failures = 0
                        continue
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
                    browser, _ctx, page = F.setup_browser_retrying(p)
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
                    "name": _target_name(entries, t),
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
                successful_ids.add(fid)
        finally:
            browser.close()

    log.info("Diffed %d committees this run.", done)
    log.info("Attempted %d committees; usable %d; unusable %d; blocked %s",
             attempted, done, unusable, "yes" if blocked else "no")
    # Stable machine-readable line for the workflow's chain guard.
    print(f"RUN_RESULT attempted={attempted} usable={done} "
          f"unusable={unusable} blocked={1 if blocked else 0}")
    if args.require_no_missing:
        failures = _remediation_verification_failures(
            entries, args.filer_ids, successful_ids,
        )
        if failures:
            # A fresh usable diff settles the fate of that fetch tree even
            # when the overall multi-filer gate fails. Clean filers are done;
            # filers still missing IDs must be fetched from scratch next time.
            # Preserve progress only for targets whose verification itself was
            # unusable, so a later run can retry the check without re-fetching.
            if successful_ids:
                F.clear_identity_progress(sorted(successful_ids))
            for failure in failures:
                log.error("Identity remediation verification failed — %s", failure)
            print(f"REMEDIATION_VERIFY passed=0 failed={len(failures)}")
            return 1
        print(f"REMEDIATION_VERIFY passed={len(successful_ids)} failed=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
