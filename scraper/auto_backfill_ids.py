#!/usr/bin/env python3
"""
Find filers with ORESTAR discrepancies that still need work.

An exact identity diff is authoritative and selects one missing-ID filer at a
time. Committees without a usable diff fall back to batches of ten ranked by
the count survey (or, as a last resort, dollar discrepancy). Selected IDs are
written to /tmp/auto_backfill_ids.txt.

Incomplete filers (from previous failed runs) are removed from the "done"
list so they get retried. Ordinary retries come up naturally by discrepancy
size; only a partially validated identity tree is priority-boosted so its
frozen snapshot can finish before another tree starts.

Used by the backfill workflow in auto mode.
"""

import json
import os
from pathlib import Path

FILERS_DIR = Path("data/aggregated/filers")
INDEX_FILE = Path("data/aggregated/filer_index.json")
TRACKING_FILE = Path("data/backfilled_filers.txt")
INCOMPLETE_FILE = Path("data/incomplete_backfills.txt")
OUTPUT_FILE = Path(os.environ.get("AUTO_BACKFILL_OUTPUT", "/tmp/auto_backfill_ids.txt"))
MODE_FILE = Path(os.environ.get("AUTO_BACKFILL_MODE_OUTPUT",
                                "/tmp/auto_backfill_mode.txt"))
END_DATE_FILE = Path(os.environ.get("AUTO_BACKFILL_END_DATE_OUTPUT",
                                    "/tmp/auto_backfill_end_date.txt"))
RESUME_FILE = Path(os.environ.get("AUTO_BACKFILL_RESUME_OUTPUT",
                                  "/tmp/auto_backfill_resume.txt"))
BATCH_SIZE = 10
IDENTITY_BATCH_SIZE = 1

print(f"Working directory: {Path.cwd()}")
print(f"Filers dir exists: {FILERS_DIR.exists()}")
print(f"Index file exists: {INDEX_FILE.exists()}")

# Build slug -> filer_id mapping from the index
slug_to_fid = {}
if INDEX_FILE.exists():
    with open(INDEX_FILE) as f:
        for entry in json.load(f):
            if entry.get("filer_id") and entry.get("slug"):
                slug_to_fid[entry["slug"]] = str(entry["filer_id"])
print(f"Filer IDs in index: {len(slug_to_fid)}")

already_done = set()
if TRACKING_FILE.exists():
    already_done = set(TRACKING_FILE.read_text().split())
    print(f"Already backfilled: {len(already_done)} filers")

# Incomplete filers from previous runs — remove from done so they get retried.
# Format: "fid" or "fid:count". Filers with 3+ retries are skipped for now
# (they'll be retried after all other filers are processed).
MAX_RETRIES = 3
incomplete = {}  # fid -> retry count
if INCOMPLETE_FILE.exists():
    for line in INCOMPLETE_FILE.read_text().strip().split("\n"):
        if ":" in line:
            fid_str, cnt = line.split(":", 1)
            fid_str = fid_str.strip()
            incomplete[fid_str] = int(cnt)
        elif line.strip():
            incomplete[line.strip()] = 1
    retryable = {fid for fid, cnt in incomplete.items() if cnt < MAX_RETRIES}
    deferred = {fid for fid, cnt in incomplete.items() if cnt >= MAX_RETRIES}
    already_done -= retryable
    if retryable:
        print(f"Incomplete filers to retry: {len(retryable)}")
    if deferred:
        print(f"Deferred filers (>={MAX_RETRIES} retries, skipping for now): {len(deferred)} — {sorted(deferred)}")

# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------
#
# Rank by ROWS MISSING, not by dollar discrepancy.
#
# The dollar ranking sent the backfill after committees it could not help. Of
# the 50 largest deltas measured by the coverage survey, 32 had nothing to
# fetch at all: Committee for SAIF Keeping tops the list at $665,242 and holds
# all seven of the seven transactions ORESTAR has for it. Meanwhile Oregon
# Right to Life is $614,666 adrift and short only 42 rows, while Local 48 is
# short 13,626. Rows missing and dollars adrift turn out to be close to
# independent, so ranking by one to find the other never worked.
#
# The survey asks ORESTAR for its own record count -- one search, no export --
# and records the shortfall. Fetching what it found missing is the only part of
# the discrepancy a backfill can address; the rest is balance reconciliation
# for dormant and pre-ORESTAR committees, which is a different job.
SURVEY_FILE = Path("data/coverage_survey.json")
DIFF_FILE = Path("data/coverage_diff.json")
IDENTITY_PROGRESS_FILE = Path("data/identity_remediation_windows.json")


def _evidence_day(row):
    """Comparable YYYY-MM-DD horizon for a survey or exact diff row."""
    return str(row.get("range_end") or row.get("checked") or "")[:10]


def _active_identity_roots():
    """Return filer -> frozen end date for resumable top-level windows."""
    try:
        rows = json.loads(IDENTITY_PROGRESS_FILE.read_text()) \
            if IDENTITY_PROGRESS_FILE.exists() else []
    except Exception:
        rows = []
    roots = {}
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        if (not isinstance(key, list) or len(key) != 7 or key[0] != "ALL"
                or key[3:6] != ["None", "None", "None"]):
            continue
        fid, end = str(key[-1]), str(key[2])
        if end > roots.get(fid, ""):
            roots[fid] = end
    return roots


survey_rows = []
if SURVEY_FILE.exists():
    try:
        survey_rows = json.loads(SURVEY_FILE.read_text())
    except Exception as exc:
        print(f"Could not read {SURVEY_FILE}: {exc}")
survey_days = {
    str(row.get("filer_id") or ""): _evidence_day(row)
    for row in survey_rows
    if row.get("filer_id")
}
active_roots = _active_identity_roots()

filers = []
deferred_exact = []
exact_covered = set()
diff_rows = []
if DIFF_FILE.exists():
    try:
        diff_rows = json.loads(DIFF_FILE.read_text())
    except Exception as exc:
        print(f"Could not read {DIFF_FILE}: {exc}")

if diff_rows:
    for r in diff_rows:
        fid = str(r.get("filer_id") or "")
        if not fid or r.get("complete") is None:
            continue
        missing = len(r.get("missing") or [])
        # Exact evidence suppresses count evidence when it is at least as
        # current. A known missing-ID result remains authoritative even when
        # deferred; falling back to a count fetch would recreate the mixed
        # surplus/shortfall bug this path exists to avoid.
        if missing or _evidence_day(r) >= survey_days.get(fid, ""):
            exact_covered.add(fid)
        if missing <= 0:
            continue
        # A partially completed forced tree is resumable even after the
        # ordinary retry limit. Its validated leaves must not be thrown away
        # merely because one runner made no progress.
        if incomplete.get(fid, 0) >= MAX_RETRIES and fid not in active_roots:
            deferred_exact.append((missing, fid, r.get("name", "")))
            continue
        # Exact identity evidence overrides the historical done list. A count
        # can say "done" while a withdrawn row cancels the missing row.
        filers.append((missing, fid, r.get("name", "")))

# "Deferred" means after the other exact-missing committees, not abandoned.
# Without this pass the workflow eventually emitted no IDs and reported all
# discrepancies addressed while known missing transaction IDs remained.
if not filers and deferred_exact:
    filers = deferred_exact
    print(f"Retrying {len(filers)} deferred exact-missing filer(s); "
          "no non-deferred identity work remains")

if filers:
    # Finish an active frozen tree before starting another committee. This is
    # what turns a later `filer_ids=auto` dispatch into a true resume.
    filers.sort(key=lambda row: (row[1] in active_roots, row[0], row[1]),
                 reverse=True)
    batch = filers[:IDENTITY_BATCH_SIZE]
    unit = "identity rows"
    mode = "identity"
    print(f"Exact diff covers {len(exact_covered)} committees; "
          f"{len(filers)} still have genuinely missing transaction IDs")
if not filers and survey_rows:
    surveyed_complete = 0
    for r in survey_rows:
        fid = str(r.get("filer_id") or "")
        missing = int(r.get("missing") or 0)
        # A usable identity diff is authoritative in both directions. Never
        # let a stale count survey re-queue a filer whose exact missing set is
        # empty, or replace a deferred exact remediation with count evidence.
        if (not fid or fid in already_done or fid in exact_covered
                or incomplete.get(fid, 0) >= MAX_RETRIES):
            continue
        if missing <= 0:
            # Measured and complete. Not "not yet done" — there is nothing to
            # fetch, so queueing it would burn a run to confirm that again.
            surveyed_complete += 1
            continue
        filers.append((missing, fid, r.get("name", "")))
    print(f"Survey covers {len(survey_rows)} committees: "
          f"{len(filers)} short of ORESTAR, {surveyed_complete} already complete")
    filers.sort(reverse=True)
    batch = filers[:BATCH_SIZE]
    unit = "rows"
    mode = "count"
elif not filers:
    # No row-count evidence yet.  A dollar difference may select a useful
    # committee, but only when it came from a paired ORESTAR/app capture.  Live
    # transactions against an older summary are deliberately not eligible:
    # that fallback is what repeatedly sent the backfill after healthy filers.
    print("No coverage survey found — considering paired balance snapshots only. "
          "Run the Coverage Survey workflow to target by rows actually missing.")
    for f in FILERS_DIR.glob("*.json"):
        with open(f) as fh:
            d = json.load(fh)
        slug = d.get("slug", f.stem)
        scope_ids = sorted({str(fid) for fid in (d.get("filer_ids") or []) if str(fid)})
        # An aggregate dollar gap cannot say which physical committee is
        # missing a row. Count/identity evidence is per-filer and may still
        # queue these scopes; the heuristic dollar fallback must not guess.
        if len(scope_ids) != 1:
            continue
        fid = scope_ids[0]
        if (not fid or fid in already_done or fid in exact_covered
                or incomplete.get(fid, 0) >= MAX_RETRIES):
            continue
        comparison = d.get("orestar_comparison") or {}
        if (comparison.get("status") != "paired"
                or not comparison.get("actionable")
                or d.get("closed")):
            continue
        disc = abs(comparison.get("delta_at_capture") or 0)
        if disc > 0.01:
            filers.append((disc, fid, d.get("name", "")))
    filers.sort(reverse=True)
    batch = filers[:BATCH_SIZE]
    unit = "dollars"
    mode = "count"

if batch:
    print(f"Found {len(filers)} filers to backfill, selecting top {len(batch)} by {unit}:")
    for score, fid, name in batch:
        retry = " (RETRY)" if fid in incomplete else ""
        shown = (f"{score:,} exact IDs missing" if unit == "identity rows"
                 else f"{score:,} rows missing" if unit == "rows"
                 else f"${score:,.2f}")
        print(f"  {fid}: {shown} — {name}{retry}")
    OUTPUT_FILE.write_text(" ".join(fid for _, fid, _ in batch))
    MODE_FILE.write_text(mode + "\n")
    if mode == "identity" and batch[0][1] in active_roots:
        END_DATE_FILE.write_text(active_roots[batch[0][1]] + "\n")
        RESUME_FILE.write_text("true\n")
        print(f"Resuming frozen identity tree through {active_roots[batch[0][1]]}")
    else:
        END_DATE_FILE.unlink(missing_ok=True)
        RESUME_FILE.write_text("false\n")
else:
    OUTPUT_FILE.unlink(missing_ok=True)
    END_DATE_FILE.unlink(missing_ok=True)
    RESUME_FILE.write_text("false\n")
    MODE_FILE.write_text(("identity" if diff_rows else "count") + "\n")
    print("No filers with unresolved discrepancies.")
