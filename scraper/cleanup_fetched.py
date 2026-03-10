"""
cleanup_fetched.py — Identify and remove zero-data windows from fetched_windows.json.

A "zero-data window" is a standard 7-day window (span == 6 days) that was marked
as fetched but has NO matching transactions in the CSV data.  These are likely
failed downloads where the scraper received an empty/error response but still
recorded the window as done.

Usage:
    python cleanup_fetched.py              # dry run (report only)
    python cleanup_fetched.py --apply      # remove zero-data windows and save

Reads:
    data/fetched_windows.json
    data/transactions/txn_YYYY.csv.gz

Writes (with --apply):
    data/fetched_windows.json  (cleaned)
    data/fetched_windows_backup.json  (original backup)
"""

import argparse
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
FETCHED_LOG = DATA_DIR / "fetched_windows.json"
TRANS_DIR = DATA_DIR / "transactions"


def load_transactions() -> pd.DataFrame:
    """Load all transaction CSVs into a single DataFrame with parsed filed_date."""
    frames = []
    for f in sorted(TRANS_DIR.glob("txn_*.csv.gz")):
        try:
            df = pd.read_csv(f, usecols=["filed_date", "tran_type"], dtype=str)
            frames.append(df)
        except Exception as exc:
            print(f"  Warning: could not read {f.name}: {exc}")
    if not frames:
        raise RuntimeError("No transaction CSVs found")

    combined = pd.concat(frames, ignore_index=True)

    # Parse filed_date — handle both YYYY-MM-DD and MM/DD/YYYY formats
    combined["filed_date"] = pd.to_datetime(
        combined["filed_date"], format="mixed", dayfirst=False
    ).dt.date

    # Normalize tran_type
    combined["tran_type"] = combined["tran_type"].str.strip().str.upper()

    # Drop rows with unparseable dates
    combined = combined.dropna(subset=["filed_date"])

    print(f"Loaded {len(combined):,} transactions from {len(frames)} CSV files")
    return combined


def main():
    parser = argparse.ArgumentParser(
        description="Find and remove zero-data windows from fetched_windows.json"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually remove zero-data windows (default is dry-run report only)"
    )
    args = parser.parse_args()

    # 1. Load fetched windows
    with open(FETCHED_LOG) as f:
        windows = json.load(f)
    print(f"Fetched windows: {len(windows)} total")

    # 2. Load all transactions
    txn = load_transactions()

    # Build a set of (tran_type, filed_date) for fast lookup
    txn_keys = set(zip(txn["tran_type"], txn["filed_date"]))

    # 3. Check each standard 7-day window
    zero_data = []
    non_standard = 0
    has_data = 0

    for entry in windows:
        ttype, start_str, end_str = entry
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
        span = (end - start).days

        # Only check standard 7-day windows (span == 6 days)
        if span != 6:
            non_standard += 1
            continue

        # Check if ANY date in [start, end] has a transaction with this tran_type
        found = False
        d = start
        while d <= end:
            if (ttype.upper(), d) in txn_keys:
                found = True
                break
            d += timedelta(days=1)

        if found:
            has_data += 1
        else:
            zero_data.append(entry)

    print(f"\nResults:")
    print(f"  Standard 7-day windows checked: {len(windows) - non_standard}")
    print(f"  Non-standard windows (skipped): {non_standard}")
    print(f"  Windows with data:              {has_data}")
    print(f"  Zero-data windows:              {len(zero_data)}")

    if zero_data:
        # Show some examples
        print(f"\nFirst 20 zero-data windows:")
        for entry in zero_data[:20]:
            print(f"  {entry}")
        if len(zero_data) > 20:
            print(f"  ... and {len(zero_data) - 20} more")

        # Show breakdown by type
        from collections import Counter
        type_counts = Counter(e[0] for e in zero_data)
        print(f"\nZero-data windows by type:")
        for t in sorted(type_counts):
            print(f"  {t}: {type_counts[t]}")

        # Show date range
        dates = [date.fromisoformat(e[1]) for e in zero_data]
        print(f"\nDate range of zero-data windows: {min(dates)} to {max(dates)}")

    if args.apply and zero_data:
        # Save backup
        backup = DATA_DIR / "fetched_windows_backup.json"
        shutil.copy2(FETCHED_LOG, backup)
        print(f"\nBackup saved to: {backup}")

        # Remove zero-data windows
        zero_set = {tuple(e) for e in zero_data}
        cleaned = [e for e in windows if tuple(e) not in zero_set]
        with open(FETCHED_LOG, "w") as f:
            json.dump(cleaned, f, indent=2)
        print(f"Cleaned fetched_windows.json: {len(cleaned)} entries (removed {len(zero_data)})")
    elif zero_data and not args.apply:
        print(f"\nDry run — no changes made. Re-run with --apply to remove zero-data windows.")


if __name__ == "__main__":
    main()
