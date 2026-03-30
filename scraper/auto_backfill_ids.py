#!/usr/bin/env python3
"""
Find filers with ORESTAR discrepancies that haven't been backfilled yet.
Writes the top 25 filer IDs (by discrepancy size) to /tmp/auto_backfill_ids.txt.

Used by the backfill workflow in auto mode.
"""

import json
import sys
from pathlib import Path

FILERS_DIR = Path("data/aggregated/filers")
INDEX_FILE = Path("data/aggregated/filer_index.json")
TRACKING_FILE = Path("data/backfilled_filers.txt")
INCOMPLETE_FILE = Path("data/incomplete_backfills.txt")
OUTPUT_FILE = Path("/tmp/auto_backfill_ids.txt")
BATCH_SIZE = 25

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

# Incomplete filers from previous runs get priority — they had partial
# downloads due to rate-limiting and need to be retried
incomplete = set()
if INCOMPLETE_FILE.exists():
    incomplete = set(INCOMPLETE_FILE.read_text().split())
    # Remove them from already_done so they get retried
    already_done -= incomplete
    if incomplete:
        print(f"Incomplete filers to retry: {len(incomplete)}")

filers = []
for f in FILERS_DIR.glob("*.json"):
    with open(f) as fh:
        d = json.load(fh)
    slug = d.get("slug", f.stem)
    fid = slug_to_fid.get(slug)
    if not fid or fid in already_done:
        continue
    # Use the largest signal: either the single orestar_discrepancy or
    # the max yearly discrepancy (which catches multi-year gaps that
    # the single-year orestar_discrepancy might miss)
    disc = abs(d.get("orestar_discrepancy", 0))
    yearly = d.get("yearly_discrepancies", {})
    if yearly:
        max_yearly = max(abs(v.get("discrepancy", 0)) for v in yearly.values())
        disc = max(disc, max_yearly)
    if disc > 0.01:
        # Boost priority for incomplete filers so they're retried first
        priority = disc + (1e12 if fid in incomplete else 0)
        filers.append((priority, disc, fid, d.get("name", "")))

filers.sort(reverse=True)
batch = filers[:BATCH_SIZE]

if batch:
    print(f"Found {len(filers)} filers with discrepancies, selecting top {len(batch)}:")
    for priority, disc, fid, name in batch:
        retry = " (RETRY)" if fid in incomplete else ""
        print(f"  {fid}: ${disc:,.2f} — {name}{retry}")
    OUTPUT_FILE.write_text(" ".join(fid for _, _, fid, _ in batch))
    # Clear incomplete filers that are being retried this batch
    retried = incomplete & {fid for _, _, fid, _ in batch}
    if retried and INCOMPLETE_FILE.exists():
        remaining_incomplete = incomplete - retried
        if remaining_incomplete:
            INCOMPLETE_FILE.write_text("\n".join(sorted(remaining_incomplete)) + "\n")
        else:
            INCOMPLETE_FILE.unlink()
        print(f"Cleared {len(retried)} filers from incomplete list")
else:
    print("No filers with unresolved discrepancies.")
    # Don't write the file — caller checks for its existence
