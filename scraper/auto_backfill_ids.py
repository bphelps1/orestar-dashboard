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
TRACKING_FILE = Path("data/backfilled_filers.txt")
OUTPUT_FILE = Path("/tmp/auto_backfill_ids.txt")
BATCH_SIZE = 25

print(f"Working directory: {Path.cwd()}")
print(f"Filers dir exists: {FILERS_DIR.exists()}")
print(f"Filers dir file count: {len(list(FILERS_DIR.glob('*.json'))) if FILERS_DIR.exists() else 0}")

already_done = set()
if TRACKING_FILE.exists():
    already_done = set(TRACKING_FILE.read_text().split())
    print(f"Already backfilled: {len(already_done)} filers")

filers = []
for f in FILERS_DIR.glob("*.json"):
    with open(f) as fh:
        d = json.load(fh)
    disc = abs(d.get("orestar_discrepancy", 0))
    fid = d.get("filer_id")
    if disc > 0.01 and fid and str(fid) not in already_done:
        filers.append((disc, str(fid), d.get("name", "")))

filers.sort(reverse=True)
batch = filers[:BATCH_SIZE]

if batch:
    print(f"Found {len(filers)} filers with discrepancies, selecting top {len(batch)}:")
    for disc, fid, name in batch:
        print(f"  {fid}: ${disc:,.2f} — {name}")
    OUTPUT_FILE.write_text(" ".join(fid for _, fid, _ in batch))
else:
    print("No filers with unresolved discrepancies.")
    # Don't write the file — caller checks for its existence
