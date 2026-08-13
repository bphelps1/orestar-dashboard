#!/usr/bin/env python3
"""
Find filers with ORESTAR discrepancies that haven't been backfilled yet.
Writes the top 10 filer IDs (by discrepancy size) to /tmp/auto_backfill_ids.txt.

Incomplete filers (from previous failed runs) are removed from the "done"
list so they get retried, but are NOT priority-boosted — they come up
naturally by discrepancy size. This prevents the same large filers from
blocking every batch.

Used by the backfill workflow in auto mode.
"""

import json
from pathlib import Path

FILERS_DIR = Path("data/aggregated/filers")
INDEX_FILE = Path("data/aggregated/filer_index.json")
TRACKING_FILE = Path("data/backfilled_filers.txt")
INCOMPLETE_FILE = Path("data/incomplete_backfills.txt")
OUTPUT_FILE = Path("/tmp/auto_backfill_ids.txt")
BATCH_SIZE = 10

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
        print(f"Deferred filers (>{MAX_RETRIES} retries, skipping for now): {len(deferred)} — {sorted(deferred)}")

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

filers = []
survey_rows = []
if SURVEY_FILE.exists():
    try:
        survey_rows = json.loads(SURVEY_FILE.read_text())
    except Exception as exc:
        print(f"Could not read {SURVEY_FILE}: {exc}")

if survey_rows:
    surveyed_complete = 0
    for r in survey_rows:
        fid = str(r.get("filer_id") or "")
        missing = int(r.get("missing") or 0)
        if not fid or fid in already_done:
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
else:
    # No survey yet — fall back to the dollar ranking so the workflow still
    # functions, but say so, because this is the ranking that misfires.
    print("No coverage survey found — falling back to dollar-discrepancy order. "
          "Run the Coverage Survey workflow to target by rows actually missing.")
    for f in FILERS_DIR.glob("*.json"):
        with open(f) as fh:
            d = json.load(fh)
        slug = d.get("slug", f.stem)
        fid = slug_to_fid.get(slug)
        if not fid or fid in already_done:
            continue
        disc = abs(d.get("orestar_discrepancy", 0))
        yearly = d.get("yearly_discrepancies", {})
        if yearly:
            max_yearly = max(abs(v.get("discrepancy", 0)) for v in yearly.values())
            disc = max(disc, max_yearly)
        if disc > 0.01:
            filers.append((disc, fid, d.get("name", "")))
    filers.sort(reverse=True)
    batch = filers[:BATCH_SIZE]
    unit = "dollars"

if batch:
    print(f"Found {len(filers)} filers to backfill, selecting top {len(batch)} by {unit}:")
    for score, fid, name in batch:
        retry = " (RETRY)" if fid in incomplete else ""
        shown = f"{score:,} rows missing" if unit == "rows" else f"${score:,.2f}"
        print(f"  {fid}: {shown} — {name}{retry}")
    OUTPUT_FILE.write_text(" ".join(fid for _, fid, _ in batch))
    # Clear retryable incomplete filers that are being retried this batch.
    # Deferred filers (>MAX_RETRIES) stay in the file with their counts.
    batch_fids = {fid for _, fid, _ in batch}
    retried = set(incomplete.keys()) & batch_fids
    if retried and INCOMPLETE_FILE.exists():
        remaining = {fid: cnt for fid, cnt in incomplete.items() if fid not in retried}
        if remaining:
            INCOMPLETE_FILE.write_text(
                "\n".join(f"{fid}:{cnt}" for fid, cnt in sorted(remaining.items())) + "\n"
            )
        else:
            INCOMPLETE_FILE.unlink()
        print(f"Cleared {len(retried)} filers from incomplete list")
else:
    print("No filers with unresolved discrepancies.")
