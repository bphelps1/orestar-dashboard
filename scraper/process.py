"""
process.py — Clean, deduplicate, and aggregate ORESTAR Excel exports.

Pipeline:
  1. Read all Excel files from data/_raw/
  2. Deduplicate by ORESTAR transaction ID
  3. Normalize contributor/payee names
  4. Fuzzy-deduplicate names (rapidfuzz)
  5. Apply entity_map.json overrides
  6. Aggregate to JSON files for the dashboard
  7. Write/update data/transactions.csv.gz
  8. Delete raw Excel files
"""

import gzip
import json
import logging
import re
import shutil
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process as rfuzz_process

import orestar_parse
import supabase_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT          = Path(__file__).parent.parent
RAW_DIR       = ROOT / "data" / "_raw"
DATA_DIR      = ROOT / "data"
# Account-summary fields that add up across committees. Used when one
# canonical name covers several ORESTAR committees, so the comparison totals
# describe the same set on both sides.
# The three in-kind varieties ORESTAR recognises. All are non-cash and all
# appear in BOTH its Total Contributions and Total Expenditures lines.
INKIND_SUBTYPES = frozenset({
    "In-Kind Contribution",
    "In-Kind/Forgiven Account Payable",
    "In-Kind/Forgiven Personal Expenditures",   # plural, as ORESTAR spells it
})

_SUMMABLE = ("ending_cash_balance", "beginning_balance", "contributions",
             "expenditures", "other_receipts", "other_disbursements",
             "balance_adjustments", "inkind_contributions",
             "inkind_expenditures", "loans_received", "loan_payments")

AGG_DIR       = DATA_DIR / "aggregated"
ENTITY_MAP    = Path(__file__).parent / "entity_map.json"
REVIEW_QUEUE  = DATA_DIR / "review_queue.json"
TRANS_DIR     = DATA_DIR / "transactions"   # per-year gzip files: txn_YYYY.csv.gz
COMMITTEES    = DATA_DIR / "committees.csv"

AGG_DIR.mkdir(parents=True, exist_ok=True)
TRANS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Column mapping: ORESTAR Excel headers → internal names
# (ORESTAR may vary header capitalisation between exports)
# ---------------------------------------------------------------------------

COL_MAP = {
    # Actual ORESTAR XLS column names (lowercased) → internal name
    "tran id":                  "tran_id",
    "transaction id":           "tran_id",
    "filed date":               "filed_date",
    "tran date":                "tran_date",   # transaction date (kept for reference)
    "date":                     "filed_date",
    "amount":                   "amount",
    "tran amount":              "amount",
    "sub type":                 "sub_type",
    "subtype":                  "sub_type",
    "contributor/payee":        "contributor_payee",
    "contributor":              "contributor_payee",
    "payee":                    "contributor_payee",
    "contributor/payee name":   "contributor_payee",
    "contributor type":         "contributor_type",
    "filer":                    "filer",
    "committee":                "filer",
    "filer name":               "filer",
    "committee name":           "filer",
    "office":                   "office",
    "office sought":            "office",
    "party":                    "party",
    "purpose":                  "purpose",
    "purp desc":                "purpose",     # ORESTAR actual column name
    "purpose codes":            "purpose_codes",
    "occptn txt":               "occupation",
    "emp name":                 "employer",
    "city":                     "city",
    "state":                    "state",
    "zip":                      "zip",
    "book type":                "book_type",
}

CONTRIBUTOR_TYPE_LABELS = {
    "I": "Individual",
    "B": "Business / Corporation",
    "L": "Labor Organization",
    "P": "Political Action Committee",
    "F": "Political Party",
    "C": "Candidate & Family",
    "U": "Unregistered Committee",
    "O": "Other",
}


def make_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:60]


# Fuzzy-dedup only the N most-frequent names; rarer names are self-canonical
# unless entity_map.json explicitly maps them.  At 0.76 µs/comparison,
# 10 000² ≈ 100 M comparisons ≈ 76 s — safe inside any workflow timeout.
MAX_DEDUP_NAMES = 10_000


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

ABBREV_MAP = {
    r"\bllc\b":  "LLC",
    r"\binc\.?": "Inc",
    r"\bcorp\.?": "Corp",
    r"\bpac\b":  "PAC",
    r"\bltd\.?": "Ltd",
    r"\bco\.?\b": "Co",
    r"\bassn\.?": "Assn",
    r"\bassoc\.?": "Assoc",
    r"\bcommittee\b": "Committee",
    r"\bfor\b":  "for",
    r"\band\b":  "and",
    r"\bof\b":   "of",
    r"\bthe\b":  "the",
}


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/extra spaces, expand standard abbreviations."""
    if not isinstance(name, str):
        return ""
    n = name.strip().lower()
    # Remove punctuation except hyphens and apostrophes
    n = re.sub(r"[^\w\s\-']", " ", n)
    # Collapse whitespace
    n = re.sub(r"\s+", " ", n).strip()
    return n


def canonical_name(raw: str) -> str:
    """Title-case a normalized name, restoring known acronyms."""
    n = normalize_name(raw)
    # Title case first
    n = n.title()
    # Re-apply acronym/abbreviation corrections
    for pattern, replacement in ABBREV_MAP.items():
        n = re.sub(pattern, replacement, n, flags=re.IGNORECASE)
    return n.strip()


# ---------------------------------------------------------------------------
# Entity map and fuzzy deduplication
# ---------------------------------------------------------------------------

def load_entity_map() -> dict[str, str]:
    if ENTITY_MAP.exists():
        with open(ENTITY_MAP) as f:
            return json.load(f)
    return {}


def save_entity_map(em: dict[str, str]) -> None:
    with open(ENTITY_MAP, "w") as f:
        json.dump(em, f, indent=2, sort_keys=True)


def apply_entity_map(name: str, entity_map: dict[str, str]) -> str:
    return entity_map.get(name, name)


def fuzzy_deduplicate(
    names: list[str],
    entity_map: dict[str, str],
    auto_threshold: float = 95.0,
    review_threshold: float = 85.0,
) -> tuple[dict[str, str], list[dict]]:
    """
    Cluster names by fuzzy similarity.

    Returns:
        canonical_map:  {raw_name → canonical_name}
        review_items:   list of {"a": ..., "b": ..., "score": ...} for 85–95% matches
    """
    # Apply existing entity_map first (case-insensitive: try title-cased key, then exact key)
    def _em_lookup(n: str) -> str:
        return entity_map.get(canonical_name(n), entity_map.get(n, n))

    resolved = {n: _em_lookup(n) for n in names}

    # Count frequency for picking canonical form
    freq: dict[str, int] = defaultdict(int)
    for n in names:
        freq[resolved[n]] += 1

    # Build clusters using union-find
    parent: dict[str, str] = {n: n for n in set(resolved.values())}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Canonical = higher frequency
        if freq[ra] >= freq[rb]:
            parent[rb] = ra
        else:
            parent[ra] = rb

    all_resolved = list(set(resolved.values()))
    review_items = []

    def _scorer(a, b, **kw):
        return max(fuzz.token_sort_ratio(a, b), fuzz.token_set_ratio(a, b))

    # Limit the O(n²) comparison pool to the most-frequent names so the step
    # completes within workflow timeouts.  Rare names (one-time contributors)
    # remain self-canonical; entity_map.json handles any manual merges needed
    # beyond this set.
    if len(all_resolved) > MAX_DEDUP_NAMES:
        log.warning(
            "Limiting fuzzy-dedup to the %d most-frequent names (of %d total); "
            "use entity_map.json for merges outside this set.",
            MAX_DEDUP_NAMES, len(all_resolved),
        )
        candidates = sorted(
            all_resolved, key=lambda n: freq.get(n, 0), reverse=True
        )[:MAX_DEDUP_NAMES]
    else:
        candidates = all_resolved

    log.info("Fuzzy-deduplicating %d names…", len(candidates))
    for i, name_a in enumerate(candidates):
        if i % 500 == 0 and i > 0:
            log.debug("  … %d / %d", i, len(candidates))
        # rapidfuzz process.extract returns top matches
        matches = rfuzz_process.extract(
            name_a,
            candidates,
            scorer=_scorer,
            score_cutoff=review_threshold,
            limit=10,
        )
        for name_b, score, _ in matches:
            if name_a == name_b:
                continue
            if score >= auto_threshold:
                union(name_a, name_b)
            else:
                # 85–95: flag for human review (only once per pair)
                pair = tuple(sorted([name_a, name_b]))
                review_items.append({"a": pair[0], "b": pair[1], "score": round(score, 1)})

    # Build canonical map for ALL resolved names (not just comparison candidates)
    cluster_freq: dict[str, dict[str, int]] = defaultdict(dict)
    for n in all_resolved:
        root = find(n)
        cluster_freq[root][n] = freq.get(n, 0)

    canonical_map: dict[str, str] = {}
    for raw, resolved_name in resolved.items():
        root = find(resolved_name)
        # Canonical = member with highest frequency
        canon = max(cluster_freq[root], key=lambda x: cluster_freq[root][x])
        canonical_map[raw] = canon

    # Deduplicate review items
    seen_pairs: set[tuple] = set()
    unique_review = []
    for item in review_items:
        pair = (item["a"], item["b"])
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            unique_review.append(item)

    return canonical_map, unique_review


# ---------------------------------------------------------------------------
# Excel loading
# ---------------------------------------------------------------------------

XLS_MAGIC  = b"\xd0\xcf\x11\xe0"   # OLE2 Compound Document (old .xls)
XLSX_MAGIC = b"PK\x03\x04"         # ZIP archive (new .xlsx)


def _detect_engine(path: Path) -> str | None:
    """
    Peek at the first 4 bytes to determine the Excel engine to use.
    Returns 'xlrd' (old .xls), 'openpyxl' (new .xlsx), or None if HTML/unknown.
    """
    header = path.read_bytes()[:8]
    if header[:4] == XLS_MAGIC:
        return "xlrd"
    if header[:4] == XLSX_MAGIC:
        return "openpyxl"
    if header[:5].lower() in (b"<!doc", b"<html"):
        return None  # HTML error page — skip
    # Unknown: let openpyxl try and fail with a useful error
    return "openpyxl"


def load_excel_files(raw_dir: Path) -> pd.DataFrame:
    """Read all Excel files in raw_dir, returning a combined DataFrame."""
    files = sorted(raw_dir.glob("*.xls*"))
    if not files:
        log.warning("No Excel files found in %s", raw_dir)
        return pd.DataFrame()

    log.info("Loading %d Excel files…", len(files))
    frames = []
    for f in files:
        try:
            engine = _detect_engine(f)
            if engine is None:
                log.warning("Skipping %s — appears to be an HTML page, not Excel", f.name)
                continue
            df = pd.read_excel(f, engine=engine, dtype=str)
            # Skip cap files (4999 rows) — these are truncated parent windows
            # whose data is covered by their sub-window split files
            if len(df) >= 4999 and f.name.startswith("filer"):
                log.debug("Skipping cap file %s (%d rows — covered by split sub-windows)", f.name, len(df))
                continue
            # Normalize column names
            df.columns = [c.strip().lower() for c in df.columns]
            # Rename to internal names
            df = df.rename(columns={k: v for k, v in COL_MAP.items() if k in df.columns})
            df["_source_file"] = f.name
            # Derive tran_type from filename prefix: C_2026-...→"C", OR_2026-...→"OR"
            # ORESTAR does not include a transaction type column in the export.
            _valid_types = {"C", "E", "O", "OA", "OD", "OR"}
            _prefix = f.stem.split("_")[0].upper()
            if _prefix in _valid_types:
                df["tran_type"] = _prefix
            else:
                # Filer-targeted downloads have mixed types — infer from sub_type
                _SUB_TYPE_TO_TRAN_TYPE = {
                    "Cash Contribution": "C", "In-Kind Contribution": "C",
                    "In-Kind/Forgiven Personal Expenditures": "C",
                    "In-Kind/Forgiven Account Payable": "C",
                    "Loan Received (Non-Exempt)": "C", "Pledge of Cash": "C",
                    "Pledge of In-Kind": "C", "Pledge of Loan": "C",
                    "Cash Expenditure": "E", "Account Payable": "E",
                    "Expenditure Made by an Agent": "E",
                    "Personal Expenditure for Reimbursement": "E",
                    "Loan Payment (Non-Exempt)": "E",
                    "Miscellaneous Other Receipt": "OR", "Refunds and Rebates": "OR",
                    "Lost or Returned Check": "OR", "Interest/Investment Income": "OR",
                    "Items Sold at Fair Market Value": "OR",
                    "Loan Received (Exempt)": "OR",
                    "Account Payable Rescinded": "O", "Cash Balance Adjustment": "O",
                    "Loan Forgiven (Non-Exempt)": "O",
                    "Personal Expenditure Balance Adjustment": "O",
                    "Uncollectible Pledge of Cash": "O", "Uncollectible Pledge of In-Kind": "O",
                    "Miscellaneous Account Receivable": "OA",
                    "Unexpended Agent Balance": "OA",
                    "Miscellaneous Other Disbursement": "OD",
                    "Return or Refund of Contribution": "OD",
                    "Nonpartisan Activity": "OD", "Loan Payment (Exempt)": "OD",
                }
                st_col = "sub_type" if "sub_type" in df.columns else "sub type"
                if st_col in df.columns:
                    df["tran_type"] = df[st_col].map(_SUB_TYPE_TO_TRAN_TYPE).fillna("")
                    inferred = (df["tran_type"] != "").sum()
                    log.info("Inferred tran_type from sub_type for %d/%d rows in %s",
                             inferred, len(df), f.name)
                else:
                    df["tran_type"] = ""
            frames.append(df)
        except Exception as exc:
            log.warning("Failed to read %s: %s", f.name, exc)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    log.info("Loaded %d rows from %d files", len(combined), len(frames))
    return combined


# ---------------------------------------------------------------------------
# Per-year transaction file helpers
# ---------------------------------------------------------------------------

def _txn_path(year) -> Path:
    return TRANS_DIR / f"txn_{year}.csv.gz"


def _load_all_transactions() -> pd.DataFrame:
    """Load and concatenate all per-year transaction files."""
    files = sorted(TRANS_DIR.glob("txn_*.csv.gz"))
    if not files:
        return pd.DataFrame()
    return pd.concat(
        [pd.read_csv(f, compression="gzip", dtype=str) for f in files],
        ignore_index=True,
    )


def _save_transactions(df: pd.DataFrame) -> None:
    """Split df by year and write/overwrite per-year gzip CSV files.

    Uses mtime=0 for deterministic gzip output so that files whose content
    has not changed produce identical bytes on each run — keeping git diffs
    clean across years where no new data arrived.
    """
    import io
    TRANS_DIR.mkdir(parents=True, exist_ok=True)
    date_col = df["filed_date"].astype(str)
    years = date_col.apply(lambda d: d[:4] if len(d) >= 4 and d[:4].isdigit() else "0000")
    n_written = n_unchanged = 0
    for yr, grp in df.groupby(years, sort=False):
        # Serialise to CSV bytes in memory
        buf = io.BytesIO()
        grp.to_csv(buf, index=False)
        csv_bytes = buf.getvalue()

        # Compress deterministically (mtime=0 → no embedded timestamp)
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
            gz.write(csv_bytes)
        new_gz = gz_buf.getvalue()

        dest = _txn_path(yr)
        if dest.exists() and dest.read_bytes() == new_gz:
            n_unchanged += 1
            continue  # identical content — skip write, no git diff

        dest.write_bytes(new_gz)
        n_written += 1

    log.info(
        "Wrote %d transactions: %d year file(s) updated, %d unchanged",
        len(df), n_written, n_unchanged,
    )


# ---------------------------------------------------------------------------
# Main processing pipeline
# ---------------------------------------------------------------------------

def _drop_superseded(df):
    """Keep only the version of each transaction that ORESTAR still counts.

    Two rules, and we had only the first:

      1. An original replaced by an amendment is dropped. ORESTAR's Account
         Summary counts the amendment, not the original, and the two arrive in
         different fetch windows because they were filed on different dates.

      2. Where a transaction was amended MORE THAN ONCE, only the newest
         amendment survives. Every amendment points back at the original rather
         than at the amendment before it, so rule 1 alone left a twice-amended
         transaction with two live rows and a three-times-amended one with
         three. Julie for County Commissioner holds three versions of the same
         $167.41 contribution — same date, same amount, three filed dates — and
         we counted all three. Dataset-wide: 160 stale rows, 68 committees,
         $462,613, all of it inflating our side, which is the direction that
         makes a committee look like it holds MORE than ORESTAR reports.

    Lived in two places before this, copied, with the second labelled "same
    logic as step 4b". They were the same, which is why fixing rule 2 in one
    would have left the other wrong. Returns (df, removed_tran_ids) so callers
    that sync to Postgres can delete what they dropped.
    """
    removed: set[str] = set()
    orig_col = "original id" if "original id" in df.columns else None
    status_col = "tran status" if "tran status" in df.columns else None
    if not (orig_col and status_col and "tran_id" in df.columns):
        return df, removed

    # Rule 1 — originals replaced by an amendment.
    amended = df[df[status_col] == "Amended"]
    if not amended.empty:
        superseded = set(
            amended[orig_col].dropna().astype(str).str.strip()
        ) & set(df["tran_id"].astype(str).str.strip())
        if superseded:
            before = len(df)
            df = df[~df["tran_id"].astype(str).str.strip().isin(superseded)]
            removed |= superseded
            log.info("Removed %d superseded originals (replaced by amendments): "
                     "%d → %d rows", before - len(df), before, len(df))

    # Rule 2 — older amendments in a chain.
    amended = df[df[status_col] == "Amended"]
    if not amended.empty:
        key = ["filer_id", orig_col] if "filer_id" in df.columns else [orig_col]
        # Newest wins: filed date first, tran_id to break same-day ties.
        order = [c for c in ("filed_date", "tran_id") if c in amended.columns]
        ranked = amended.dropna(subset=[orig_col])
        if order:
            ranked = ranked.sort_values(order)
        stale = set(
            ranked.loc[ranked.duplicated(subset=key, keep="last"),
                       "tran_id"].astype(str).str.strip()
        )
        if stale:
            before = len(df)
            df = df[~df["tran_id"].astype(str).str.strip().isin(stale)]
            removed |= stale
            log.info("Removed %d superseded amendments (kept the newest of each "
                     "chain): %d → %d rows", before - len(df), before, len(df))

    return df, removed


def process() -> None:
    entity_map = load_entity_map()

    # ── 1. Load raw Excel files ──────────────────────────────────────────────
    new_df = load_excel_files(RAW_DIR)

    if new_df.empty:
        log.info("No new data to process.")
        df = _load_all_transactions()
        if df.empty:
            log.info("No existing transactions data. Nothing to do.")
            return
        log.info("Re-aggregating from existing transaction files (%d rows)", len(df))

        # Ensure amount is numeric for re-aggregation path
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)

        df, _ = _drop_superseded(df)
    else:
        # ── 2. Type coercion ─────────────────────────────────────────────────
        for col in ["tran_id", "contributor_payee", "filer", "contributor_type",
                    "tran_type", "sub_type", "office", "party", "purpose"]:
            if col not in new_df.columns:
                new_df[col] = ""
            new_df[col] = new_df[col].fillna("").astype(str).str.strip()

        if "amount" in new_df.columns:
            new_df["amount"] = (
                pd.to_numeric(new_df["amount"].str.replace(r"[$,]", "", regex=True), errors="coerce")
                .fillna(0.0)
            )
        else:
            new_df["amount"] = 0.0

        if "filed_date" in new_df.columns:
            new_df["filed_date"] = pd.to_datetime(new_df["filed_date"], errors="coerce").dt.date
        else:
            new_df["filed_date"] = pd.NaT

        if "book_type" not in new_df.columns:
            new_df["book_type"] = ""
        new_df["book_type"] = (
            new_df["book_type"].fillna("").astype(str).str.strip()
            .replace({"Candidate's Immediate Family": "Candidate & Immediate Family", "": "Other"})
        )

        # ── 3. Load existing transactions and merge ───────────────────────────
        existing = _load_all_transactions()
        if not existing.empty:
            log.info("Loading existing transactions (%d rows)…", len(existing))
            if "book type" in existing.columns and "book_type" not in existing.columns:
                existing = existing.rename(columns={"book type": "book_type"})
            existing["amount"] = pd.to_numeric(existing["amount"], errors="coerce").fillna(0.0)
            existing["filed_date"] = pd.to_datetime(existing["filed_date"], errors="coerce").dt.date
            df = pd.concat([existing, new_df], ignore_index=True)
        else:
            df = new_df

        # ── 4. Deduplicate by tran_id ─────────────────────────────────────────
        if "tran_id" in df.columns and df["tran_id"].ne("").any():
            before = len(df)
            df = df.drop_duplicates(subset=["tran_id"], keep="last")
            log.info("Deduplicated by tran_id: %d → %d rows", before, len(df))

        # ── 4b. Remove originals superseded by amendments ─────────────────────
        # When a transaction is amended, ORESTAR creates a new row with a new
        # tran_id and status "Amended", whose "original id" points to the
        # original row.  Both rows may appear in our data because they were
        # filed on different dates (hence fetched in different weekly windows).
        # ORESTAR's Account Summary counts only the latest version, so we must
        # drop the originals to avoid double-counting.
        df, superseded_ids = _drop_superseded(df)

        # ── 5. Name normalization + fuzzy dedup ───────────────────────────────
        all_names = (
            df["contributor_payee"].dropna().unique().tolist()
            + df["filer"].dropna().unique().tolist()
        )
        all_names = [n for n in all_names if n]

        canonical_map, review_items = fuzzy_deduplicate(
            all_names,
            entity_map,
            auto_threshold=95.0,
            review_threshold=80.0,
        )

        df["contributor_payee_canonical"] = df["contributor_payee"].map(
            lambda x: canonical_map.get(x, canonical_map.get(canonical_name(x), x))
        )
        df["filer_canonical"] = df["filer"].map(
            lambda x: canonical_map.get(x, canonical_map.get(canonical_name(x), x))
        )

        # ── 6. Map contributor type codes to labels ───────────────────────────
        df["contributor_type"] = df["contributor_type"].fillna("").astype(str)
        df["contributor_type_label"] = df["contributor_type"].map(
            lambda x: CONTRIBUTOR_TYPE_LABELS.get(x.strip().upper(), x)
        )

        # ── 7. Save review queue ──────────────────────────────────────────────
        if review_items:
            log.info("Writing %d review items to %s", len(review_items), REVIEW_QUEUE)
            with open(REVIEW_QUEUE, "w") as f:
                json.dump(review_items, f, indent=2)

        # ── 8. Write updated per-year transaction files ───────────────────────
        df["filed_date"] = df["filed_date"].astype(str)
        _save_transactions(df)

        # ── 8b. Sync the changed window to Postgres ───────────────────────────
        # Upsert only the freshly-fetched rows (their canonical values were
        # computed on the merged df above), and drop any superseded originals.
        try:
            _new_ids = set(new_df["tran_id"].astype(str).str.strip())
            _changed = df[df["tran_id"].astype(str).str.strip().isin(_new_ids)]
            supabase_sync.upsert_transactions(_changed)
            supabase_sync.delete_transactions(superseded_ids)
            # Assign donor_id to the freshly-synced rows (committee-id or exact
            # alias lookup; unknown names become provisional donors until the
            # weekly full resolution).
            if supabase_sync.sync_enabled():
                import resolve_donors
                resolve_donors.assign_incremental()
        except Exception as e:
            log.warning("transaction sync failed: %s", e)

    # ── 9. Aggregate JSON files ───────────────────────────────────────────────
    aggregate(df)

    # ── 10. Delete raw Excel files ────────────────────────────────────────────
    #
    # Filer-targeted files used to be kept here so a backfill could resume from
    # them. They were committed to git to survive between runners, and git keeps
    # every version of every one forever: 10,205 files and an 80 GB repository,
    # against a 100 GB hard limit that would have stopped every push at once.
    #
    # Nothing needs them now. The rows are in the year shards and in Postgres,
    # and the fetcher decides what to skip from ORESTAR's own record counts plus
    # what the database holds — both of which outlive any file on disk.
    deleted = 0
    for f in RAW_DIR.glob("*.xlsx"):
        f.unlink()
        deleted += 1
    if deleted:
        log.info("Deleted %d raw Excel files (merged and synced)", deleted)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def to_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def aggregate(df: pd.DataFrame) -> None:
    log.info("Aggregating data for dashboard JSON files…")

    # Ensure amount is numeric
    df = df.copy()

    # Normalize book_type (legacy CSV may still have raw "book type" column name)
    if "book type" in df.columns and "book_type" not in df.columns:
        df = df.rename(columns={"book type": "book_type"})
    if "book_type" not in df.columns:
        df["book_type"] = "Other"
    else:
        df["book_type"] = (
            df["book_type"].fillna("").astype(str).str.strip()
            .replace({"Candidate's Immediate Family": "Candidate & Immediate Family", "": "Other"})
        )
    # Out-of-state flag: non-blank state that isn't OR → True; blank or OR → False
    _st = df["state"].fillna("").astype(str).str.strip().str.upper() if "state" in df.columns else pd.Series("", index=df.index)
    df["is_out_of_state"] = _st.ne("") & _st.ne("OR")

    df["amount"] = df["amount"].apply(to_float)
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce")
    df = df.dropna(subset=["filed_date"])

    # Use tran_date for year/month groupings (matches ORESTAR Account Summary);
    # fall back to filed_date for records where tran_date is missing.
    if "tran_date" in df.columns:
        _tran_date = pd.to_datetime(df["tran_date"], errors="coerce")
        _eff_date  = _tran_date.where(_tran_date.notna(), df["filed_date"])
    else:
        _eff_date = df["filed_date"]
    df["year"]  = _eff_date.dt.year.astype(int)
    df["month"] = _eff_date.dt.to_period("M").astype(str)

    # ── Filer-ID-based name normalization ───────────────────────────────────
    # All transactions for a given filer ID adopt the committee name from the
    # most recent transaction with that ID, so renames are reflected everywhere.
    # Uses the raw "filer" column (always populated) to build the map, since
    # backfilled rows may have blank filer_canonical values.
    if "filer id" in df.columns:
        _fid = df["filer id"].fillna("").astype(str).str.strip()
        _has_id = _fid.ne("")
        _id_map = (
            df.loc[_has_id]
            .assign(_fid_tmp=_fid[_has_id])
            .sort_values("filed_date")
            .groupby("_fid_tmp")["filer"]
            .last()
            .to_dict()
        )
        _mapped = _fid.map(_id_map)
        _fc = "filer_canonical" if "filer_canonical" in df.columns else "filer"
        df["filer_canonical"] = _mapped.where(_mapped.notna(), df.get(_fc, ""))
        log.info(
            "Filer-ID normalization applied: %d filer IDs → %d unique canonical names",
            len(_id_map), df["filer_canonical"].nunique(),
        )

    # Use canonical names if available, fall back to raw
    contrib_col = "contributor_payee_canonical" if "contributor_payee_canonical" in df.columns else "contributor_payee"
    filer_col   = "filer_canonical" if "filer_canonical" in df.columns else "filer"

    ttype = df["tran_type"].str.strip().str.upper()
    contributions   = df[ttype == "C"]
    expenditures    = df[ttype == "E"]
    other_receipts  = df[ttype == "OR"]                    # Other Receipt only (matches ORESTAR "Other Receipts" line)
    other_disburse  = df[ttype == "OD"]                    # Other Disbursement only (matches ORESTAR "Other Disbursements" line)
    # Note: types O (Account Payable Rescinded, Loan Forgiven) and OA (Unexpended
    # Agent Balance, Misc Account Receivable) are non-cash accounting entries
    # that ORESTAR does NOT include in its Other Receipts or COH calculation.
    #
    # "Cash Balance Adjustment" is the exception, and excluding it was wrong.
    # ORESTAR's account summary carries a Balance Adjustments line that lands
    # between the two subtotals and moves the ending balance directly, and
    # these rows are its transaction-level source: summed per filer-year they
    # reproduce that line exactly in 1,092 of 1,629 cases (67%), and no other
    # sub_type or combination comes close.
    #
    # Leaving it out cost a permanent, carried-forward error. Oregon Realtors'
    # 2015 adjustment is -$27,432.32 and its 2015 discrepancy was +$27,432 to
    # the dollar; from 2017 on, its yearly contributions and expenditures match
    # ORESTAR exactly while the balance stays off by a constant. 205 committees
    # are explained by this alone.
    #
    # Taken from transactions rather than from the scraped summary line on
    # purpose: cash on hand is calculated, and ORESTAR stays the check.
    balance_adjust  = df[(ttype == "O") & (df["sub_type"].str.strip() == "Cash Balance Adjustment")]

    # Separate cash contributions from in-kind.
    # IMPORTANT: Use exact "In-Kind Contribution" match only. Other sub_types containing
    # "In-Kind" (e.g. "In-Kind/Forgiven Account Payable", "In-Kind/Forgiven Personal
    # Expenditures", "Pledge of In-Kind") should NOT be mirrored to the expenditure side,
    # as they are not standard in-kind contributions in ORESTAR's methodology.
    # Using str.contains("In-Kind") was a bug that inflated expenditures by $5.2M across
    # 1,483 filers without a matching contribution offset.
    # All three in-kind varieties, not just the plain one. This drives both the
    # separate in-kind metric and, by inversion, the cash-only contribution
    # figures — forgiven payables and forgiven personal expenditures are no
    # more cash than a donated banner is.
    inkind_mask     = contributions["sub_type"].isin(INKIND_SUBTYPES)
    cash_contribs   = contributions[~inkind_mask]
    inkind_contribs = contributions[inkind_mask]

    # ORESTAR-matching: Cash Contributions = Cash Contribution + In-Kind Contribution
    _orestar_contrib_mask = contributions["sub_type"].isin({"Cash Contribution"} | set(INKIND_SUBTYPES))
    _orestar_contribs = contributions[_orestar_contrib_mask]
    # ORESTAR-matching: Cash Expenditures = Cash Expenditure + In-Kind mirrored
    _cash_expend_mask = expenditures["sub_type"] == "Cash Expenditure"
    _cash_expend_global = expenditures[_cash_expend_mask]
    _inkind_global_amount = round(inkind_contribs["amount"].sum(), 2)

    # ── summary.json ─────────────────────────────────────────────────────────
    summary = {
        # Contributions are CASH ONLY app-wide; in-kind is reported separately
        # as total_inkind and never folded in. ORESTAR's own "Cash
        # Contributions" line does include in-kind, so these figures will
        # differ from ORESTAR's published summary by exactly total_inkind —
        # the ORESTAR reconciliation below uses its own _orestar_* frames and
        # is unaffected.
        "total_contributions":  round(cash_contribs["amount"].sum(), 2),
        "total_inkind":         round(inkind_contribs["amount"].sum(), 2),
        "total_expenditures":   round(_cash_expend_global["amount"].sum(), 2),
        "total_other_receipts":  round(other_receipts["amount"].sum(), 2),
        "total_other_disburse":  round(other_disburse["amount"].sum(), 2),
        "total_transactions":   int(len(df)),
        "num_contributions":    int(len(cash_contribs)),
        "num_inkind":           int(len(inkind_contribs)),
        "num_expenditures":     int(len(expenditures)),
        "date_range_start":     _eff_date.min().strftime("%Y-%m-%d") if len(df) else "",
        "date_range_end":       _eff_date.max().strftime("%Y-%m-%d") if len(df) else "",
        "last_updated":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_json("summary.json", summary)

    # ── top_donors.json ───────────────────────────────────────────────────────
    top_donors_all = (
        cash_contribs.groupby(contrib_col)["amount"]
        .sum()
        .nlargest(1000)
        .reset_index()
        .rename(columns={contrib_col: "name", "amount": "total"})
    )
    top_donors_all["total"] = top_donors_all["total"].round(2)

    by_year_donors: dict[str, list] = {}
    for yr in sorted(cash_contribs["year"].unique()):
        yr_df = cash_contribs[cash_contribs["year"] == yr]
        top = (
            yr_df.groupby(contrib_col)["amount"]
            .sum()
            .nlargest(1000)
            .reset_index()
            .rename(columns={contrib_col: "name", "amount": "total"})
        )
        top["total"] = top["total"].round(2)
        by_year_donors[str(yr)] = top.to_dict(orient="records")

    _write_json("top_donors.json", {
        "all_time": top_donors_all.to_dict(orient="records"),
        "by_year":  by_year_donors,
    })

    # ── top_recipients.json ───────────────────────────────────────────────────
    top_recipients_all = (
        cash_contribs.groupby(filer_col)["amount"]
        .sum()
        .nlargest(100)
        .reset_index()
        .rename(columns={filer_col: "name", "amount": "total"})
    )
    top_recipients_all["total"] = top_recipients_all["total"].round(2)

    by_year_recipients: dict[str, list] = {}
    for yr in sorted(cash_contribs["year"].unique()):
        yr_df = cash_contribs[cash_contribs["year"] == yr]
        top = (
            yr_df.groupby(filer_col)["amount"]
            .sum()
            .nlargest(100)
            .reset_index()
            .rename(columns={filer_col: "name", "amount": "total"})
        )
        top["total"] = top["total"].round(2)
        by_year_recipients[str(yr)] = top.to_dict(orient="records")

    _write_json("top_recipients.json", {
        "all_time": top_recipients_all.to_dict(orient="records"),
        "by_year":  by_year_recipients,
    })

    # ── timeline.json ─────────────────────────────────────────────────────────
    cash_monthly = (
        cash_contribs.groupby("month")["amount"].sum().reset_index()
        .rename(columns={"amount": "contributions"})
    )
    inkind_monthly = (
        inkind_contribs.groupby("month")["amount"].sum().reset_index()
        .rename(columns={"amount": "inkind"})
    )
    expend_monthly = (
        expenditures.groupby("month")["amount"].sum().reset_index()
        .rename(columns={"amount": "expenditures"})
    )
    count_monthly = (
        df.groupby("month").size().reset_index(name="count")
    )
    timeline = (
        cash_monthly
        .merge(inkind_monthly, on="month", how="outer")
        .merge(expend_monthly, on="month", how="outer")
        .merge(count_monthly,  on="month", how="outer")
        .fillna(0)
        .sort_values("month")
    )
    timeline["contributions"] = timeline["contributions"].round(2)
    timeline["inkind"]        = timeline["inkind"].round(2)
    timeline["expenditures"]  = timeline["expenditures"].round(2)
    timeline["count"]         = timeline["count"].astype(int)
    _write_json("timeline.json", timeline.to_dict(orient="records"))

    # ── by_contributor_type.json ──────────────────────────────────────────────
    def _type_rows(frame):
        """Build [{type, total, top_donors}] list interleaving in-state then out-of-state per type."""
        if frame.empty or "book_type" not in frame.columns:
            return []
        oos = frame["is_out_of_state"] if "is_out_of_state" in frame.columns else pd.Series(False, index=frame.index)
        in_s  = frame[~oos].groupby("book_type")["amount"].sum()
        out_s = frame[ oos].groupby("book_type")["amount"].sum()
        all_types = sorted(
            set(in_s.index) | set(out_s.index),
            key=lambda t: -(in_s.get(t, 0) + out_s.get(t, 0)),
        )
        rows = []
        for t in all_types:
            iv = round(float(in_s.get(t, 0)), 2)
            ov = round(float(out_s.get(t, 0)), 2)
            if iv:
                sub_in = frame[~oos & (frame["book_type"] == t)]
                top_in = (
                    sub_in.groupby(contrib_col)["amount"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={contrib_col: "name", "amount": "total"})
                )
                top_in["total"] = top_in["total"].round(2)
                rows.append({"type": t, "total": iv, "top_donors": top_in.to_dict(orient="records")})
            if ov:
                sub_oos = frame[oos & (frame["book_type"] == t)]
                top_oos = (
                    sub_oos.groupby(contrib_col)["amount"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={contrib_col: "name", "amount": "total"})
                )
                top_oos["total"] = top_oos["total"].round(2)
                rows.append({"type": t + " (out of state)", "total": ov, "top_donors": top_oos.to_dict(orient="records")})
        return rows

    by_type_by_year: dict[str, list] = {}
    for yr in sorted(cash_contribs["year"].dropna().unique()):
        yr_rows = _type_rows(cash_contribs[cash_contribs["year"] == yr])
        by_type_by_year[str(int(yr))] = yr_rows
    by_type_by_month: dict[str, list] = {}
    if "month" in cash_contribs.columns:
        for mo in sorted(cash_contribs["month"].dropna().unique()):
            by_type_by_month[str(mo)] = _type_rows(cash_contribs[cash_contribs["month"] == mo])
    _write_json("by_contributor_type.json", {
        "all_time": _type_rows(cash_contribs),
        "by_year":  by_type_by_year,
        "by_month": by_type_by_month,
    })

    # ── recent_transactions.json ──────────────────────────────────────────────
    recent = df.copy()
    recent["filed_date"] = recent["filed_date"].dt.strftime("%Y-%m-%d")
    recent["amount"] = recent["amount"].round(2)

    keep_cols = [c for c in [
        "tran_id", "filed_date", "tran_type", "amount",
        contrib_col, filer_col, "book_type", "purpose"
    ] if c in recent.columns]
    recent = recent[keep_cols].rename(columns={
        contrib_col: "contributor_payee",
        filer_col:   "filer",
    })
    recent = recent.sort_values("filed_date", ascending=False).head(10000)
    recent = recent.fillna("")
    _write_json("recent_transactions.json", recent.to_dict(orient="records"))

    # ── per-filer index + detail files ───────────────────────────────────────
    global_coh_data = aggregate_filers(df, contributions, inkind_contribs, expenditures,
                                       other_receipts, other_disburse, balance_adjust,
                                       filer_col, contrib_col)

    # Update summary.json with correct global cash-on-hand (sum of per-filer COH)
    summary["global_cash_on_hand"] = global_coh_data["global_cash_on_hand"]
    summary["global_beginning_balances"] = global_coh_data["global_beginning_balances"]
    _write_json("summary.json", summary)

    log.info("Aggregation complete. JSON files written to %s", AGG_DIR)


def _latest_year_summaries(yearly: dict, filer_ids: list[str]) -> dict:
    """Newest year's account summary per filer, keyed as the balance
    comparison expects.

    The yearly file names fields as ORESTAR labels them (contributions,
    expenditures, other_receipts); the balance comparison inherited
    orestar_-prefixed names from the scraper it replaces. Mapped here so the
    consumers stay untouched.
    """
    out: dict[str, dict] = {}
    for fid in filer_ids:
        years = (yearly.get(str(fid)) or {}).get("years", {})
        if not years:
            continue
        yr = max(years)                      # string years sort correctly
        y = years[yr]
        out[str(fid)] = {
            "year": int(yr),
            "beginning_balance": float(y.get("beginning_balance") or 0),
            "ending_cash_balance": float(y.get("ending_cash_balance") or 0),
            "orestar_contributions": float(y.get("contributions") or 0),
            "orestar_expenditures": float(y.get("expenditures") or 0),
            "orestar_other_receipts": float(y.get("other_receipts") or 0),
            "orestar_other_disbursements": float(y.get("other_disbursements") or 0),
            "balance_adjustments": float(y.get("balance_adjustments") or 0),
            "inkind_contributions": float(y.get("inkind_contributions") or 0),
            "inkind_expenditures": float(y.get("inkind_expenditures") or 0),
            "loans_received": float(y.get("loans_received") or 0),
            "loans_received_exempt": float(y.get("loans_received_exempt") or 0),
            "loan_payments": float(y.get("loan_payments") or 0),
            "loan_payments_exempt": float(y.get("loan_payments_exempt") or 0),
            "accounts_receivable": float(y.get("accounts_receivable") or 0),
            "accounts_payable": float(y.get("accounts_payable") or 0),
            "scrape_ts": (yearly.get(str(fid)) or {}).get("ts", 0),
        }
    return out


def scrape_account_summaries(
    filer_ids: list[str],
    *,
    cache_path: Path = DATA_DIR / "orestar_cash_balances.json",
    max_workers: int = 10,
    # A full refresh is ~7,245 live page fetches, about 58 minutes. At a 1-day
    # TTL the daily job re-scraped all of them on any day the weekly scrapers
    # had not just run, which does not fit in its 60-minute budget — the runs
    # on 22, 24, 25 and 27 July all died here, mid-scrape.
    #
    # Nothing needs it daily. These figures are the CHECK that cash-on-hand is
    # compared against, never an input to it, and the weekly filer-metadata and
    # earliest-balances jobs already own refreshing them.
    max_age_days: int = 7,
) -> dict[str, dict]:
    """Fetch full Account Summary from ORESTAR publicAccountSummary for each filer ID.

    Extracts all financial line items including Beginning Balance (Previous Year),
    which serves as the anchor for calculating per-year cash balances.

    Results are cached in *cache_path*.  Only filer IDs missing from the cache
    (or whose cached entry is older than *max_age_days*) are re-fetched.

    Returns ``{filer_id_str: {year, beginning_balance, ending_cash_balance, ...}}``
    for every ID that could be resolved.
    """
    import concurrent.futures
    import urllib.request

    # Load cache
    cache: dict = {}
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)

    now_ts = datetime.now().timestamp()
    cutoff = now_ts - max_age_days * 86_400

    ids_to_fetch = [
        fid for fid in filer_ids
        if fid not in cache or cache[fid].get("ts", 0) < cutoff
    ]

    if not ids_to_fetch:
        log.info("ORESTAR account-summary cache is fresh (%d entries)", len(cache))
    else:
        log.info(
            "Fetching ORESTAR Account Summary for %d / %d filers …",
            len(ids_to_fetch), len(filer_ids),
        )

        # Regex to extract the year from the page heading
        _YEAR_RE = re.compile(
            r"Account Summary Information for the year\s+(\d{4})",
        )

        def _parse_dollar(html: str, label: str) -> float | None:
            """Extract a dollar amount following a label in ORESTAR HTML.

            The old docstring here called ($X,XXX.XX) the POSITIVE format. It
            is ORESTAR's negative format — accounting parentheses — and reading
            it as positive is what flipped the sign on every committee in the
            red. See scraper/orestar_parse.py.
            """
            return orestar_parse.parse_dollar(html, label)

        def _fetch_one(fid: str) -> tuple[str, dict | None]:
            url = (
                "https://secure.sos.state.or.us/orestar/"
                f"publicAccountSummary.do?filerId={fid}"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ORESTAR-dashboard/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
            except Exception:
                return fid, None

            # Extract year
            year_m = _YEAR_RE.search(html)
            if not year_m:
                return fid, None
            year = int(year_m.group(1))

            # Extract all financial line items
            ending = _parse_dollar(html, "Ending Cash Balance")
            if ending is None:
                return fid, None  # Page didn't load properly

            # Extract Financial Status section separately to avoid
            # ambiguous label matches (e.g. "Cash Balance" appears as both
            # a section header in Cash Balance and a line item in Financial Status)
            fs_html = ""
            fs_start = html.find("<h5>Financial Status</h5>")
            if fs_start == -1:
                fs_start = html.find("Financial Status-->")
            if fs_start >= 0:
                fs_html = html[fs_start:]

            result = {
                "year": year,
                # Contributions section
                "beginning_balance": _parse_dollar(html, "Beginning Balance (Previous Year)") or 0.0,
                "ending_cash_balance": ending,
                "orestar_contributions": _parse_dollar(html, "Cash Contributions") or 0.0,
                "loans_received": _parse_dollar(html, "Loans Received (non-exempt)") or 0.0,
                "inkind_contributions": _parse_dollar(html, "In-Kind Contributions") or 0.0,
                # Expenditures section
                "orestar_expenditures": _parse_dollar(html, "Cash Expenditures") or 0.0,
                "loan_payments": _parse_dollar(html, "Loan Payments (non-exempt)") or 0.0,
                "inkind_expenditures": _parse_dollar(html, "In-Kind Expenditures") or 0.0,
                # Cash Balance section
                "orestar_other_receipts": _parse_dollar(html, "Other Receipts") or 0.0,
                "loans_received_exempt": _parse_dollar(html, "Loans Received (exempt)") or 0.0,
                "orestar_other_disbursements": _parse_dollar(html, "Other Disbursements") or 0.0,
                "loan_payments_exempt": _parse_dollar(html, "Loan Payments (exempt)") or 0.0,
                "balance_adjustments": _parse_dollar(html, "Balance Adjustments") or 0.0,
                # Financial Status section (parsed from FS-only HTML to avoid ambiguous matches)
                "cash_balance_fs": _parse_dollar(fs_html, "Cash Balance") or 0.0,
                "accounts_receivable": _parse_dollar(fs_html, "Accounts Receivable") or 0.0,
                "total_outstanding_loans": _parse_dollar(fs_html, "Total Outstanding Loans") or 0.0,
                "outstanding_personal_expenditures": _parse_dollar(fs_html, "Outstanding Personal Expenditures") or 0.0,
                "accounts_payable": _parse_dollar(fs_html, "Accounts Payable") or 0.0,
                "balance_deficit": _parse_dollar(fs_html, "Balance Deficit") or 0.0,
            }
            return fid, result

        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, fid): fid for fid in ids_to_fetch}
            for future in concurrent.futures.as_completed(futures):
                fid, val = future.result()
                done += 1
                if done % 500 == 0:
                    log.info("  … %d / %d fetched", done, len(ids_to_fetch))
                if val is not None:
                    cache[fid] = {**val, "ts": now_ts}

        log.info("ORESTAR account-summary fetch complete (%d cached total)", len(cache))

        # Persist cache
        with open(cache_path, "w") as f:
            json.dump(cache, f, separators=(",", ":"))

    # Return full summary dicts (including scrape timestamp for frontend display)
    result = {}
    for fid, entry in cache.items():
        if "ending_cash_balance" in entry:
            result[fid] = dict(entry)  # keep all fields including "ts"
        elif "balance" in entry:
            # Legacy cache entry (old format) — treat as ending balance only
            result[fid] = {"ending_cash_balance": entry["balance"], "year": 0, "beginning_balance": 0.0, "ts": entry.get("ts", 0)}
    return result


def aggregate_filers(
    df: pd.DataFrame,
    contributions: pd.DataFrame,   # all type C contributions (including in-kind)
    inkind_contribs: pd.DataFrame,
    expenditures: pd.DataFrame,    # E type
    other_receipts: pd.DataFrame,  # OR/O/OA type (e.g. Return/Refund, Misc Receipt)
    other_disburse: pd.DataFrame,  # OD type (e.g. Misc Other Disbursement)
    balance_adjust: pd.DataFrame,  # type O "Cash Balance Adjustment" only
    filer_col: str,
    contrib_col: str,
) -> None:
    """Generate filer_index.json and per-filer detail files under data/aggregated/filers/."""
    filers_dir = AGG_DIR / "filers"
    filers_dir.mkdir(parents=True, exist_ok=True)

    def _filer_type_rows(frame):
        if frame.empty or "book_type" not in frame.columns:
            return []
        oos = frame["is_out_of_state"] if "is_out_of_state" in frame.columns else pd.Series(False, index=frame.index)
        in_s  = frame[~oos].groupby("book_type")["amount"].sum()
        out_s = frame[ oos].groupby("book_type")["amount"].sum()
        all_types = sorted(
            set(in_s.index) | set(out_s.index),
            key=lambda t: -(in_s.get(t, 0) + out_s.get(t, 0)),
        )
        rows = []
        for t in all_types:
            iv = round(float(in_s.get(t, 0)), 2)
            ov = round(float(out_s.get(t, 0)), 2)
            if iv:
                sub_in = frame[~oos & (frame["book_type"] == t)]
                top_in = (
                    sub_in.groupby(contrib_col)["amount"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={contrib_col: "name", "amount": "total"})
                )
                top_in["total"] = top_in["total"].round(2)
                rows.append({"type": t, "total": iv, "top_donors": top_in.to_dict(orient="records")})
            if ov:
                sub_oos = frame[oos & (frame["book_type"] == t)]
                top_oos = (
                    sub_oos.groupby(contrib_col)["amount"].sum()
                    .nlargest(5).reset_index()
                    .rename(columns={contrib_col: "name", "amount": "total"})
                )
                top_oos["total"] = top_oos["total"].round(2)
                rows.append({"type": t + " (out of state)", "total": ov, "top_donors": top_oos.to_dict(orient="records")})
        return rows

    # ── Build slug registry with collision handling ──────────────────────────
    all_filer_names = sorted([n for n in df[filer_col].dropna().unique() if n != ""])
    slug_registry: dict[str, str] = {}  # slug → name (first claimant)
    filer_slugs: dict[str, str] = {}    # name → slug

    for name in all_filer_names:
        base = make_slug(name)
        slug = base
        counter = 2
        while slug in slug_registry and slug_registry[slug] != name:
            slug = f"{base}_{counter}"
            counter += 1
        slug_registry[slug] = name
        filer_slugs[name] = slug

    # ── Pre-group for performance ─────────────────────────────────────────────
    contrib_groups  = contributions.groupby(filer_col)
    inkind_groups   = inkind_contribs.groupby(filer_col)
    expend_groups   = expenditures.groupby(filer_col)
    or_groups       = other_receipts.groupby(filer_col)
    od_groups       = other_disburse.groupby(filer_col)
    ba_groups       = balance_adjust.groupby(filer_col)
    all_groups      = df.groupby(filer_col)

    def get_group(groups, name):
        return groups.get_group(name) if name in groups.groups else pd.DataFrame()

    def monthly_sum(frame, col_name):
        if frame.empty or "month" not in frame.columns:
            return pd.Series(dtype=float, name=col_name)
        return frame.groupby("month")["amount"].sum().rename(col_name)

    # ── Scrape ORESTAR Account Summary ───────────────────────────────────────
    # Build canonical-name → filer-id mapping so we can look up ORESTAR data.
    _filer_id_col = "filer id" if "filer id" in df.columns else None
    _orestar_data: dict[str, dict] = {}  # canonical name → full summary dict
    # Loaded here, ahead of the first use. The balance comparison below now
    # derives from this file, so reading it later left _yearly_summaries
    # unbound and killed the whole run:
    #   UnboundLocalError: cannot access local variable '_yearly_summaries'
    _yearly_path = DATA_DIR / "orestar_yearly_summaries.json"
    _yearly_summaries: dict[str, dict] = {}  # filer_id → {years: {yr: {...}}, ts}
    if _yearly_path.exists():
        with open(_yearly_path) as f:
            _yearly_summaries = json.load(f)
        log.info("Loaded yearly ORESTAR summaries for %d filers", len(_yearly_summaries))

    if _filer_id_col:
        # EVERY filer id a canonical name covers, not just one.
        #
        # 265 canonical names span more than one ORESTAR committee — 554
        # committees, 106,337 rows, $133M — usually because the committees
        # differ only in punctuation ("Yes! Keep Our Groceries Tax Free!" vs
        # "...Tax-Free!"). Aggregation groups by canonical NAME, so those rows
        # are summed together; ORESTAR reports per committee.
        #
        # Keeping one id via drop_duplicates compared our merged totals against
        # a single committee's summary, which reads as a huge overage that
        # looks exactly like ORESTAR under-reporting. That one name was 78% of
        # 2018's entire apparent gap: ours $8,632,365 against ORESTAR's
        # $6,362,423, purely because the other committee's $4.6M was folded in
        # on our side only.
        _name_to_fids: dict[str, list[str]] = (
            df[[filer_col, _filer_id_col]]
            .dropna(subset=[_filer_id_col])
            .assign(_fid=lambda x: x[_filer_id_col].astype(str).str.strip())
            .query("_fid != ''")
            .groupby(filer_col)["_fid"]
            .agg(lambda v: sorted(set(v)))
            .to_dict()
        )
        # Kept for callers that only need a representative id.
        _name_to_fid: dict[str, str] = {n: ids[-1] for n, ids in _name_to_fids.items()}
        unique_fids = sorted({f for ids in _name_to_fids.values() for f in ids})
        # Derived from the yearly summaries rather than scraped again.
        #
        # Two caches were reading the SAME ORESTAR page. scrape_account_summaries
        # took the current year into orestar_cash_balances.json;
        # fetch_earliest_balances takes every year into
        # orestar_yearly_summaries.json. The yearly file is a strict superset —
        # 7,518 filers against 7,383, and nothing in the other file is absent
        # from it.
        #
        # Keeping both meant they drifted. The cash-balances cache was last
        # written 2026-03-26..04-04 while the yearly file is current, so every
        # balance comparison — the whole discrepancy tab — was measuring today's
        # transactions against ORESTAR figures four months old. 359 of 3,666
        # same-year balances disagreed purely from that lag.
        #
        # It also carried the in-kind bug fixed in #56: it asks for "In-Kind
        # Contributions", a label the page never prints, so its in-kind fields
        # are zero for every filer.
        #
        # Dropping it removes ~7,245 page fetches (about 58 minutes) from the
        # daily refresh, which is the step that used to blow the job's budget.
        _fid_to_summary = _latest_year_summaries(_yearly_summaries, unique_fids)
        # Map canonical name → ORESTAR account summary, SUMMED over every
        # committee the name covers, so both sides of the comparison describe
        # the same set of committees. Single-committee names are unaffected.
        _partial_merge = 0
        for canon_name, fids in _name_to_fids.items():
            parts = [_fid_to_summary[f] for f in fids if f in _fid_to_summary]
            if not parts:
                continue
            # A name's transactions are summed across every committee it covers,
            # so the ORESTAR side must cover the same committees or the two are
            # not comparable. Falling back to whichever summaries happen to
            # exist reports the missing committee's entire volume as a
            # discrepancy: "Oregon People's Rebate" spans filers 20684 and
            # 22517, only 22517 has a summary, and the comparison showed a
            # $251,794 gap that was purely 20684's own contributions. Our 42
            # rows for 22517 matched ORESTAR to the cent.
            #
            # 36 of 265 multi-committee names are in this state. Skipping is
            # the honest outcome: no comparison beats a false one, and these
            # committees simply go unchecked until their summaries are scraped.
            if len(parts) < len(fids):
                _partial_merge += 1
                log.debug("Skipping ORESTAR comparison for %s — %d of %d committees "
                          "have summaries", canon_name, len(parts), len(fids))
                continue
            if len(parts) == 1:
                _orestar_data[canon_name] = parts[0]
                continue
            merged = dict(parts[0])
            for k in _SUMMABLE:
                merged[k] = round(sum(float(p.get(k) or 0) for p in parts), 2)
            merged["merged_filer_ids"] = fids     # so the join is auditable
            _orestar_data[canon_name] = merged
        if _partial_merge:
            log.warning("%d canonical names skipped: they span several committees but only "
                        "some have ORESTAR summaries — scrape the rest to compare them",
                        _partial_merge)
        log.info("ORESTAR account summaries mapped for %d / %d filers", len(_orestar_data), len(all_filer_names))

    # ── Load earliest-year beginning balances (from Playwright scraper) ──────
    _earliest_balances_path = DATA_DIR / "earliest_balances.json"
    _earliest_balances: dict[str, dict] = {}  # filer_id → {earliest_year, beginning_balance, ts}
    if _earliest_balances_path.exists():
        with open(_earliest_balances_path) as f:
            _earliest_balances = json.load(f)
        log.info("Loaded %d earliest-year beginning balances", len(_earliest_balances))
    else:
        log.warning("No earliest_balances.json found — beginning balances will default to $0")

    # Load per-year ORESTAR summaries

    # Build canonical-name → earliest balance mapping
    _name_to_earliest: dict[str, dict] = {}
    _name_to_yearly: dict[str, dict] = {}  # canonical name → {yr: summary}
    if _filer_id_col:
        for canon_name, fids in _name_to_fids.items():
            # Opening balance: sum across the committees the name covers, since
            # the rolling calculation sums their transactions.
            # Same completeness rule as the account summaries above: an anchor
            # summed over some of a name's committees, against transactions
            # summed over all of them, understates the opening balance by the
            # missing committees' share and carries that error forward for ever.
            begins = [_earliest_balances[f] for f in fids if f in _earliest_balances]
            if begins and len(begins) == len(fids):
                if len(begins) == 1:
                    _name_to_earliest[canon_name] = begins[0]
                else:
                    _name_to_earliest[canon_name] = {
                        "earliest_year": min(b.get("earliest_year", 9999) for b in begins),
                        "beginning_balance": round(
                            sum(float(b.get("beginning_balance") or 0) for b in begins), 2),
                        # Only trustworthy if EVERY part reached its first statement.
                        "reached_earliest": all(b.get("reached_earliest") for b in begins),
                    }
            # Yearly summaries: add year by year across the same committees.
            yearlies = [_yearly_summaries[f].get("years", {}) for f in fids
                        if f in _yearly_summaries]
            if not yearlies or len(yearlies) < len(fids):
                continue          # incomplete coverage — see the note above
            if len(yearlies) == 1:
                _name_to_yearly[canon_name] = yearlies[0]
                continue
            combined: dict[str, dict] = {}
            for yr in {y for d in yearlies for y in d}:
                rows = [d[yr] for d in yearlies if yr in d]
                base = dict(rows[0])
                for k in _SUMMABLE:
                    base[k] = round(sum(float(r.get(k) or 0) for r in rows), 2)
                combined[yr] = base
            _name_to_yearly[canon_name] = combined

    # Load filer metadata (party, office, committee type) from scraper cache
    _filer_metadata_path = DATA_DIR / "filer_metadata.json"
    _filer_metadata: dict[str, dict] = {}  # filer_id → {committee_type, office, party, ...}
    if _filer_metadata_path.exists():
        with open(_filer_metadata_path) as f:
            _filer_metadata = json.load(f)
        log.info("Loaded %d filer metadata entries", len(_filer_metadata))
    else:
        log.warning("No filer_metadata.json found — party/office data will be unavailable")

    # Load leadership roles (filer_id → role info)
    _leadership_path = DATA_DIR / "leadership_roles.json"
    _leadership: dict[str, dict] = {}  # filer_id → {role_title, chamber, party, ...}
    if _leadership_path.exists():
        with open(_leadership_path) as f:
            _leadership = json.load(f)
        log.info("Loaded %d leadership roles", len(_leadership))
    else:
        log.info("No leadership_roles.json found — leadership data will be unavailable")

    # ── Per-filer detail files ────────────────────────────────────────────────
    # Remove stale files whose slugs are no longer in the current filer set.
    current_slugs = set(filer_slugs.values())
    for stale in filers_dir.glob("*.json"):
        if stale.stem not in current_slugs:
            stale.unlink()
            log.debug("Removed stale filer file: %s", stale.name)

    index_rows = []
    _filer_detail_rows: list[dict] = []  # accumulated for one bulk upsert to Supabase
    _global_beginning_balances: dict[str, float] = {}  # year → sum of per-filer beginning balances
    log.info("Generating per-filer detail files for %d filers…", len(all_filer_names))

    for name in all_filer_names:
        slug = filer_slugs[name]
        filer_contrib = get_group(contrib_groups, name)
        filer_inkind  = get_group(inkind_groups,  name)
        filer_expend  = get_group(expend_groups,  name)
        filer_or      = get_group(or_groups,      name)
        filer_od      = get_group(od_groups,      name)
        filer_ba      = get_group(ba_groups,      name)
        filer_all     = get_group(all_groups,     name)

        # ── ORESTAR-matching line item definitions (empirically verified) ──────
        # ORESTAR "Cash Contributions" = Cash Contribution + In-Kind Contribution
        #   (In-kind is on BOTH sides — contribution AND expenditure — netting to zero)
        # ORESTAR "Cash Expenditures" = Cash Expenditure + In-Kind (mirrored)
        #   EXCLUDES: Account Payable, PER, Agent, Loan Payment
        # ORESTAR "Other Receipts" = type OR only
        # ORESTAR "Other Disbursements" = type OD only
        #
        # COH effective formula (in-kind cancels):
        #   Ending = Begin + CashContribution + LoansReceived + OtherReceipts
        #            - CashExpenditure - LoanPayments - OtherDisbursements

        # Stat card / ORESTAR-matching totals
        # These two frames exist ONLY to reconcile against ORESTAR's account
        # summary, so they must mirror ORESTAR's own arithmetic exactly:
        #
        #   Total Contributions = Cash Contributions
        #                       + Loans Received (non-exempt)
        #                       + In-Kind
        #   Total Expenditures  = Cash Expenditures
        #                       + Loan Payments (non-exempt)
        #                       + In-Kind
        #
        # Both were short. Contributions omitted non-exempt loans, so any
        # committee with a loan looked to be missing exactly the loan amount —
        # Oregonians Are Ready showed "missing $1,000,000" in 2024 while our
        # rows summed to ORESTAR's $1,114,500 to the cent. Expenditures counted
        # only cash, omitting both loan payments and in-kind.
        #
        # Measured over 25,451 committee-years, matching ORESTAR exactly:
        #   contributions  78.6% -> 86.5%
        #   expenditures   55.7% -> 90.3%
        #
        # This is a DIAGNOSTIC, not the balance. Cash on hand was never
        # affected: _COH_C_TYPES/_COH_E_TYPES already carried non-exempt loans
        # and correctly leave in-kind out of a cash figure. What was wrong was
        # the instrument used to decide where money is missing — and it sent
        # this investigation after $15M of gaps that were never real.
        # ORESTAR recognises THREE in-kind varieties, not one. Its Total
        # Contributions and Total Expenditures lines both count all of them,
        # and each lands on both sides — which is why the omission produced
        # symmetric gaps, equal on contributions and expenditures.
        #
        # Friends of Sam Carpenter 2018 is the clean example: short exactly
        # $140,060.27 on each side, matching that year's
        # In-Kind/Forgiven Account Payable ($75,646.13) plus
        # In-Kind/Forgiven Personal Expenditures ($64,414.14).
        #
        # Note the plural on the second. An earlier check of mine used the
        # singular, matched nothing, and made this look worth one percentage
        # point rather than six.
        _CONTRIB_TYPES = ({"Cash Contribution", "Loan Received (Non-Exempt)"}
                          | set(INKIND_SUBTYPES))
        _EXPEND_TYPES  = {"Cash Expenditure", "Loan Payment (Non-Exempt)"}
        _orestar_contrib = filer_contrib[filer_contrib["sub_type"].isin(_CONTRIB_TYPES)] if not filer_contrib.empty else filer_contrib
        # In-kind sits under tran_type C in the source data, so ORESTAR's
        # expenditure total is rebuilt from the expenditure rows plus the
        # in-kind frame.
        # Cash expenditures plus non-exempt loan payments — and deliberately
        # NOT in-kind, which is added downstream where our_e is assembled:
        #     our_e = _yearly_cash_exp + _yearly_inkind
        #
        # In-kind genuinely belongs in this comparison; ORESTAR's Total
        # Expenditures includes it, confirmed against freshly parsed summaries
        # (Yes on 117 2024: cash+loan 3,957,175.42 + in-kind 5,467,744.61 =
        # 9,424,920.03, ORESTAR's figure to the cent). It was already being
        # added there. Folding it in here as well counted it TWICE, storing
        # 14,892,664.64 for that committee-year and inflating 9,155
        # committee-years by $179,850,067 in total.
        _cash_expend_only = (filer_expend[filer_expend["sub_type"].isin(_EXPEND_TYPES)]
                             if not filer_expend.empty else filer_expend)
        _inkind_amount = round(float(filer_inkind["amount"].sum()) if not filer_inkind.empty else 0.0, 2)

        # Cash only — in-kind is a separate, distinct metric (total_inkind).
        # _orestar_contrib / _yearly_orestar_c keep the in-kind-inclusive
        # definition for the ORESTAR reconciliation further down.
        _cash_contrib_only = (filer_contrib[filer_contrib["sub_type"] != "In-Kind Contribution"]
                              if not filer_contrib.empty else filer_contrib)
        total_in    = round(float(_cash_contrib_only["amount"].sum()) if not _cash_contrib_only.empty else 0.0, 2)
        total_inkind = _inkind_amount
        total_out   = round(float(_cash_expend_only["amount"].sum()) if not _cash_expend_only.empty else 0.0, 2)
        total_or    = round(float(filer_or["amount"].sum()) if not filer_or.empty else 0.0, 2)
        total_od    = round(float(filer_od["amount"].sum()) if not filer_od.empty else 0.0, 2)
        tran_count  = int(len(filer_all))

        # COH-affecting frames (in-kind nets to zero, so exclude it)
        _COH_C_TYPES = {"Cash Contribution", "Loan Received (Non-Exempt)"}
        _COH_E_TYPES = {"Cash Expenditure", "Loan Payment (Non-Exempt)"}
        _c_for_coh = filer_contrib[filer_contrib["sub_type"].isin(_COH_C_TYPES)] if not filer_contrib.empty else filer_contrib
        _e_for_coh = filer_expend[filer_expend["sub_type"].isin(_COH_E_TYPES)] if not filer_expend.empty else filer_expend
        _or_for_coh = filer_or  # All type OR

        # Calculate net transactions per year for COH computation
        def _yearly_net(contrib_df, expend_df, or_df, od_df, ba_df):
            """Return {year_str: net_cash_flow} for each year with transactions.

            Balance adjustments carry their own sign already (they are usually
            negative), so they are added rather than subtracted — the same way
            ORESTAR's summary adds its Balance Adjustments line to reach the
            ending balance.
            """
            nets: dict[str, float] = {}
            all_frames = []
            for frame, sign in [(contrib_df, 1), (or_df, 1), (expend_df, -1), (od_df, -1),
                                (ba_df, 1)]:
                if not frame.empty and "year" in frame.columns:
                    yearly = frame.groupby("year")["amount"].sum()
                    for yr, amt in yearly.items():
                        yr_s = str(int(yr))
                        nets[yr_s] = nets.get(yr_s, 0.0) + sign * float(amt)
            return nets

        yearly_nets = _yearly_net(_c_for_coh, _e_for_coh, _or_for_coh, filer_od, filer_ba)

        # Determine beginning balances and cash-on-hand
        # Strategy: use the earliest-year beginning balance scraped directly from
        # ORESTAR (via Playwright), then roll forward through yearly transaction nets.
        # This avoids error-prone back-calculation from the current year.
        orestar_info = _orestar_data.get(name)
        beginning_balances: dict[str, float] = {}
        cash_on_hand_source = "calculated"   # always — see the check below
        has_orestar_check = False
        orestar_discrepancy = 0.0
        orestar_year = 0

        sorted_years = sorted(yearly_nets.keys())

        # Look up earliest beginning balance from the Playwright-scraped cache.
        # Only use it if the scraped year is at or before the first year with
        # transactions. If the scraped "earliest year" is AFTER our first
        # transaction year, the scraper returned the current-year balance
        # (not the actual earliest), so the beginning balance should be $0.
        # The anchor is the "Beginning Balance (Previous Year)" from the FIRST
        # account statement ORESTAR holds for this committee. Read from the
        # right page it needs no special-casing by year: a committee that
        # predates ORESTAR shows the real money it walked in with, and one
        # formed later shows $0, because there was nothing before.
        #
        # So the only question is whether the scraper actually got to that
        # page. `reached_earliest` answers it. Without that flag the two
        # outcomes were indistinguishable, and a timed-out click banked
        # whatever middle year it stopped on as the committee's opening
        # balance — filer 142 recorded $220,614.68 from 2024 for a committee
        # filing since 2006.
        earliest_info = _name_to_earliest.get(name)
        first_txn_year = int(sorted_years[0]) if sorted_years else 9999
        first_year_begin = 0.0
        # Whether our pre-statement transactions agree with the opening balance
        # ORESTAR states. None when the question does not arise.
        opening_reconciles = None
        opening_our_prior_net = None
        if earliest_info:
            scraped_year = earliest_info.get("earliest_year", 9999)
            # Older cache entries predate the flag; fall back to the year test,
            # which catches the same failure whenever transactions run earlier.
            complete = earliest_info.get("reached_earliest", scraped_year <= first_txn_year)
            if complete and scraped_year <= first_txn_year:
                first_year_begin = earliest_info["beginning_balance"]
            elif (complete and scraped_year > first_txn_year
                  and earliest_info.get("beginning_balance") is not None):
                # ORESTAR's first statement postdates our first transaction.
                #
                # Both figures describe the same money and they disagree, so
                # one of them has to be chosen rather than quietly averaged by
                # accident. ORESTAR's is the bank position it certified at the
                # time; ours is whatever pre-statement rows happen to be in the
                # transaction record, and those are systematically partial —
                # ORESTAR's transaction search returns back-dated entries filed
                # later, but its account summaries only begin in 2006, so the
                # early rows are contributions whose matching expenditures were
                # never filed as transactions.
                #
                # Measured on all five committees in this position, our
                # pre-statement net exceeds ORESTAR's stated opening in four:
                # Citizens for Mannix holds $150,000 of 2003-04 contributions
                # and NO expenditures against a stated opening of $203.84. We
                # were carrying $149,796 of money that had already been spent.
                #
                # So: take ORESTAR's figure as the anchor and drop the
                # pre-statement rows it already accounts for, rather than
                # trusting a record we can prove is incomplete.
                _pre = round(sum(yearly_nets.get(str(y), 0.0)
                                 for y in range(first_txn_year, scraped_year)), 2)
                first_year_begin = earliest_info["beginning_balance"]
                opening_reconciles = abs(_pre - first_year_begin) <= 0.01
                opening_our_prior_net = _pre
                for _y in range(first_txn_year, scraped_year):
                    yearly_nets.pop(str(_y), None)
                log.info(
                    "%s: anchoring on ORESTAR's %d opening $%.2f; our %d-%d rows "
                    "net $%.2f (%s) and are superseded by it",
                    name, scraped_year, first_year_begin, first_txn_year,
                    scraped_year - 1, _pre,
                    "reconciles" if opening_reconciles else "does NOT reconcile",
                )
            elif earliest_info.get("beginning_balance"):
                log.warning(
                    "Ignoring $%.2f opening balance for %s: paging stopped at %d but "
                    "transactions start %d — not the first statement",
                    earliest_info["beginning_balance"], name, scraped_year, first_txn_year,
                )

        # Roll forward: beginning balance for each year, then cash on hand
        running = first_year_begin
        for yr_s in sorted_years:
            beginning_balances[yr_s] = round(running, 2)
            running += yearly_nets.get(yr_s, 0.0)
        cash_on_hand = round(running, 2)

        # --- signature of the per-year deltas -------------------------------
        # Computed after the yearly loop below fills yearly_discrepancies;
        # declared here so the record above always has a value.
        discrepancy_signature = None
        discrepancy_first_bad_year = None

        # Compare against ORESTAR-reported ending balance for validation.
        #
        # This is a CHECK, never an input: cash_on_hand above is calculated
        # from transactions and nothing here changes it. The flag used to be
        # set to "orestar" at this point, which read as though the figure came
        # from ORESTAR — it never did.
        is_closed = False
        closed_since = None
        closed_final_balance = None
        if orestar_info and orestar_info.get("year", 0) > 0:
            orestar_year = orestar_info["year"]
            orestar_ending = orestar_info.get("ending_cash_balance", 0.0)
            has_orestar_check = True

            # Anchor on the last year ORESTAR reports ACTIVITY in, not merely
            # the last year it issued a statement.
            #
            # ORESTAR keeps producing an annual Account Summary after a
            # committee stops operating, and those trailing statements are
            # entirely zero — every line, including the ending balance. The
            # cash-balances file stores the LATEST year, so for a wound-down
            # committee the check was comparing our full-history balance
            # against a blank form.
            #
            # Citizens for Mannix is the clearest case. Its real history is all
            # there in ORESTAR:
            #
            #     2006  ending $568.75    contributions $186,050
            #     2007  ending $1,560.47  contributions $205,940
            #     2008  ending $106.66    contributions $645,535
            #     2009  ending $2,600.66  contributions $56,941
            #     2010  ending $0.00      contributions $12,451   <- wound down
            #     2011-2015                                        <- empty stubs
            #
            # We anchored on 2015 and recorded the committee as $299,796 adrift.
            # 253 committees carry $1.66M of "discrepancy" for this reason, and
            # a re-scrape confirmed the zeros are real rather than a scraping
            # gap — the statements genuinely are blank, so the fix belongs here
            # rather than in the scraper.
            #
            # A year that ends at zero after real activity is a perfectly good
            # anchor: that IS the committee's closing balance. Only years with
            # no activity at all are skipped.
            _all_years = _name_to_yearly.get(name, {})
            if _all_years:
                def _has_activity(y: dict) -> bool:
                    return any(abs(float(y.get(k) or 0)) > 0 for k in
                               ("contributions", "expenditures", "other_receipts",
                                "other_disbursements", "beginning_balance",
                                "ending_cash_balance", "loans_received",
                                "loan_payments", "balance_adjustments"))
                _live = sorted((int(y) for y, v in _all_years.items()
                                if str(y).isdigit() and _has_activity(v)))
                if _live and _live[-1] < orestar_year:
                    _anchor = _live[-1]
                    log.debug(
                        "%s: ORESTAR's %d statement is blank — anchoring on %d instead",
                        name, orestar_year, _anchor,
                    )
                    orestar_year = _anchor
                    orestar_ending = float(
                        _all_years[str(_anchor)].get("ending_cash_balance") or 0.0
                    )

            # Closed: ORESTAR's most recent statements are entirely blank —
            # no activity AND no cash balance.
            #
            # This is the strongest signal the record offers that a committee
            # is finished. It is deliberately NOT the same as dormant: a
            # dormant committee has ORESTAR carrying a real balance forward
            # year after year (Oregon Strong, $889,626.78), which is a
            # committee holding money and filing nothing. A blank statement
            # says the opposite — nothing held, nothing moved.
            #
            # Requiring the cash balance to be zero is what separates them,
            # and it is why "no contributions or expenditures this year" is not
            # sufficient on its own.
            if _all_years:
                _yrs = sorted((y for y in _all_years if str(y).isdigit()), key=int)
                _live_yrs = [y for y in _yrs if _has_activity(_all_years[y])]
                if _live_yrs and _yrs and _yrs[-1] != _live_yrs[-1]:
                    _blank_from = _yrs[_yrs.index(_live_yrs[-1]) + 1]
                    is_closed = True
                    closed_since = int(_blank_from)
                    closed_final_balance = float(
                        _all_years[_live_yrs[-1]].get("ending_cash_balance") or 0.0
                    )
            # A dormant committee still gets a statement: ORESTAR carries its
            # balance forward every year whether or not anything moves. We have
            # no transactions for those years, so looking the year up in our
            # own tables returned 0 and the committee appeared to be off by its
            # entire balance. Oregon Strong sat at $889,626.78 on both sides
            # and was recorded as $889,626.78 adrift; 1,447 committees — 41% of
            # everything flagged — were wrong for exactly this reason.
            #
            # When we have no data for ORESTAR's year, our balance at the end of
            # it is simply the balance we last rolled forward to: cash_on_hand.
            if str(orestar_year) in beginning_balances:
                our_anchor_ending = round(
                    beginning_balances[str(orestar_year)]
                    + yearly_nets.get(str(orestar_year), 0.0), 2
                )
            else:
                our_anchor_ending = cash_on_hand
            orestar_discrepancy = round(our_anchor_ending - orestar_ending, 2)

        # Per-year discrepancy tracking: compare our rolling calculation
        # against ORESTAR's yearly data — both ending balance AND line items
        # (contributions, expenditures, other receipts, other disbursements).
        orestar_yearly = _name_to_yearly.get(name, {})
        yearly_discrepancies: dict[str, dict] = {}

        # Pre-compute per-year sums from our filtered transaction frames
        def _yearly_sums(frame):
            if frame.empty or "year" not in frame.columns:
                return {}
            return frame.groupby("year")["amount"].sum().to_dict()

        # For ORESTAR comparison: Cash Contribution + In-Kind, Cash Expenditure + In-Kind
        _yearly_orestar_c = _yearly_sums(_orestar_contrib)
        _yearly_cash_exp  = _yearly_sums(_cash_expend_only)
        _yearly_inkind    = _yearly_sums(filer_inkind)
        _yearly_or = _yearly_sums(_or_for_coh)
        _yearly_od = _yearly_sums(filer_od)

        if orestar_yearly:
            for yr_s in sorted_years:
                yr_orestar = orestar_yearly.get(yr_s, {})
                if not yr_orestar:
                    continue

                yr_int = int(yr_s) if yr_s.isdigit() else None
                our_begin = beginning_balances.get(yr_s, 0.0)
                our_c  = round(float(_yearly_orestar_c.get(yr_int, 0)), 2)
                our_e  = round(float(_yearly_cash_exp.get(yr_int, 0)) + float(_yearly_inkind.get(yr_int, 0)), 2)
                our_or = round(float(_yearly_or.get(yr_int, 0)), 2)
                our_od = round(float(_yearly_od.get(yr_int, 0)), 2)
                our_net = yearly_nets.get(yr_s, 0.0)
                our_end = round(our_begin + our_net, 2)

                orestar_end = yr_orestar.get("ending_cash_balance")
                orestar_c   = yr_orestar.get("contributions")
                orestar_e   = yr_orestar.get("expenditures")
                orestar_or  = yr_orestar.get("other_receipts")
                orestar_od  = yr_orestar.get("other_disbursements")
                orestar_beg = yr_orestar.get("beginning_balance")

                delta_c   = round(our_c  - (orestar_c or 0), 2) if orestar_c is not None else None
                delta_e   = round(our_e  - (orestar_e or 0), 2) if orestar_e is not None else None
                delta_or  = round(our_or - (orestar_or or 0), 2) if orestar_or is not None else None
                delta_od  = round(our_od - (orestar_od or 0), 2) if orestar_od is not None else None
                delta_end = round(our_end - (orestar_end or 0), 2) if orestar_end is not None else None
                delta_beg = round(our_begin - (orestar_beg or 0), 2) if orestar_beg is not None else None

                # Include if any line-item delta exceeds $0.01
                deltas = [d for d in [delta_c, delta_e, delta_or, delta_od, delta_end, delta_beg] if d is not None]
                if any(abs(d) > 0.01 for d in deltas):
                    yearly_discrepancies[yr_s] = {
                        "our_begin": our_begin,
                        "our_contributions": our_c,
                        "our_expenditures": our_e,
                        "our_other_receipts": our_or,
                        "our_other_disbursements": our_od,
                        "our_net": round(our_net, 2),
                        "our_end": our_end,
                        "orestar_begin": round(orestar_beg, 2) if orestar_beg is not None else None,
                        "orestar_contributions": round(orestar_c, 2) if orestar_c is not None else None,
                        "orestar_expenditures": round(orestar_e, 2) if orestar_e is not None else None,
                        "orestar_other_receipts": round(orestar_or, 2) if orestar_or is not None else None,
                        "orestar_other_disbursements": round(orestar_od, 2) if orestar_od is not None else None,
                        "orestar_end": round(orestar_end, 2) if orestar_end is not None else None,
                        "delta_contributions": delta_c,
                        "delta_expenditures": delta_e,
                        "delta_other_receipts": delta_or,
                        "delta_other_disbursements": delta_od,
                        "delta_begin": delta_beg,
                        "discrepancy": delta_end,
                    }

        # Accumulate global beginning balances (sum across all filers per year)
        for yr_s, bal in beginning_balances.items():
            _global_beginning_balances[yr_s] = round(
                _global_beginning_balances.get(yr_s, 0.0) + bal, 2
            )

        # Timeline — matches ORESTAR methodology (empirically verified).
        # contributions = Cash Contribution + In-Kind (matches ORESTAR "Cash Contributions")
        # expenditures  = Cash Expenditure + In-Kind mirrored (matches ORESTAR "Cash Expenditures")
        # loans_received / loan_payments = separate for COH calculation
        # inkind shown separately for transparency
        # COH = begin + contributions + loans_received + other_receipts
        #       - expenditures - loan_payments - other_disbursements
        #   (in-kind is in both contributions and expenditures, nets to zero)
        _loans_in  = filer_contrib[filer_contrib["sub_type"] == "Loan Received (Non-Exempt)"] if not filer_contrib.empty else filer_contrib
        _loans_out = filer_expend[filer_expend["sub_type"] == "Loan Payment (Non-Exempt)"] if not filer_expend.empty else filer_expend

        c_monthly  = monthly_sum(_orestar_contrib,   "contributions")
        i_monthly  = monthly_sum(filer_inkind,        "inkind")
        li_monthly = monthly_sum(_loans_in,           "loans_received")
        # Expenditures = Cash Expenditure + In-Kind (mirrored)
        ce_monthly = monthly_sum(_cash_expend_only,   "cash_exp")
        ik_monthly = monthly_sum(filer_inkind,        "inkind_exp")
        lo_monthly = monthly_sum(_loans_out,          "loan_payments")
        or_monthly = monthly_sum(filer_or,            "other_receipts")
        od_monthly = monthly_sum(filer_od,            "other_disbursements")
        count_monthly_filer = filer_all.groupby("month").size().rename("count")
        tl_df = pd.concat([c_monthly, i_monthly, li_monthly, ce_monthly, ik_monthly,
                           lo_monthly, or_monthly, od_monthly,
                           count_monthly_filer], axis=1).fillna(0).sort_index()
        timeline = [
            {
                "month": m,
                "contributions":       round(float(row.get("contributions",       0)), 2),
                "inkind":              round(float(row.get("inkind",              0)), 2),
                "loans_received":      round(float(row.get("loans_received",      0)), 2),
                "expenditures":        round(float(row.get("cash_exp", 0)) + float(row.get("inkind_exp", 0)), 2),
                "loan_payments":       round(float(row.get("loan_payments",       0)), 2),
                "other_receipts":      round(float(row.get("other_receipts",      0)), 2),
                "other_disbursements": round(float(row.get("other_disbursements", 0)), 2),
                "count":               int(row.get("count", 0)),
            }
            for m, row in tl_df.iterrows()
        ]

        # Top donors (who gave TO this filer) — all-time and by year
        if not filer_contrib.empty and contrib_col in filer_contrib.columns:
            td = (
                filer_contrib.groupby(contrib_col)["amount"]
                .sum().nlargest(1000).reset_index()
                .rename(columns={contrib_col: "name", "amount": "total"})
            )
            td["total"] = td["total"].round(2)
            top_donors_list = td.to_dict(orient="records")

            top_donors_by_year: dict[str, list] = {}
            if "year" in filer_contrib.columns:
                for yr in sorted(filer_contrib["year"].dropna().unique()):
                    yr_df = filer_contrib[filer_contrib["year"] == yr]
                    td_yr = (
                        yr_df.groupby(contrib_col)["amount"]
                        .sum().nlargest(1000).reset_index()
                        .rename(columns={contrib_col: "name", "amount": "total"})
                    )
                    td_yr["total"] = td_yr["total"].round(2)
                    top_donors_by_year[str(int(yr))] = td_yr.to_dict(orient="records")
        else:
            top_donors_list = []
            top_donors_by_year = {}

        # Top payees (what this filer paid out) — all-time and by year
        if not filer_expend.empty and contrib_col in filer_expend.columns:
            tp = (
                filer_expend.groupby(contrib_col)["amount"]
                .sum().nlargest(200).reset_index()
                .rename(columns={contrib_col: "name", "amount": "total"})
            )
            tp["total"] = tp["total"].round(2)
            top_payees_list = tp.to_dict(orient="records")
            top_payees_by_year: dict[str, list] = {}
            if "year" in filer_expend.columns:
                for yr in sorted(filer_expend["year"].dropna().unique()):
                    yr_df = filer_expend[filer_expend["year"] == yr]
                    tp_yr = (
                        yr_df.groupby(contrib_col)["amount"]
                        .sum().nlargest(200).reset_index()
                        .rename(columns={contrib_col: "name", "amount": "total"})
                    )
                    tp_yr["total"] = tp_yr["total"].round(2)
                    top_payees_by_year[str(int(yr))] = tp_yr.to_dict(orient="records")
        else:
            top_payees_list = []
            top_payees_by_year = {}

        # By contributor type — all-time (with top_donors), by year, and by month
        by_type_list = _filer_type_rows(filer_contrib)
        by_type_by_year_filer: dict[str, list] = {}
        by_type_by_month_filer: dict[str, list] = {}
        if not filer_contrib.empty:
            if "year" in filer_contrib.columns:
                for yr in sorted(filer_contrib["year"].dropna().unique()):
                    yr_rows = _filer_type_rows(filer_contrib[filer_contrib["year"] == yr])
                    by_type_by_year_filer[str(int(yr))] = yr_rows
            if "month" in filer_contrib.columns:
                for mo in sorted(filer_contrib["month"].dropna().unique()):
                    by_type_by_month_filer[str(mo)] = _filer_type_rows(filer_contrib[filer_contrib["month"] == mo])

        # Build ORESTAR account summary block for frontend display
        _acct_summary = {}
        if orestar_info and orestar_info.get("year", 0) > 0:
            _acct_summary = {
                "year": orestar_info["year"],
                "beginning_balance": orestar_info.get("beginning_balance", 0.0),
                "ending_cash_balance": orestar_info.get("ending_cash_balance", 0.0),
                "orestar_contributions": orestar_info.get("orestar_contributions", 0.0),
                "orestar_other_receipts": orestar_info.get("orestar_other_receipts", 0.0),
                "orestar_expenditures": orestar_info.get("orestar_expenditures", 0.0),
                "orestar_other_disbursements": orestar_info.get("orestar_other_disbursements", 0.0),
                "balance_adjustments": orestar_info.get("balance_adjustments", 0.0),
                "loans_received": orestar_info.get("loans_received", 0.0),
                "loans_received_exempt": orestar_info.get("loans_received_exempt", 0.0),
                "loan_payments": orestar_info.get("loan_payments", 0.0),
                "loan_payments_exempt": orestar_info.get("loan_payments_exempt", 0.0),
                "inkind_contributions": orestar_info.get("inkind_contributions", 0.0),
                "inkind_expenditures": orestar_info.get("inkind_expenditures", 0.0),
                "cash_balance_fs": orestar_info.get("cash_balance_fs", 0.0),
                "accounts_receivable": orestar_info.get("accounts_receivable", 0.0),
                "total_outstanding_loans": orestar_info.get("total_outstanding_loans", 0.0),
                "outstanding_personal_expenditures": orestar_info.get("outstanding_personal_expenditures", 0.0),
                "accounts_payable": orestar_info.get("accounts_payable", 0.0),
                "balance_deficit": orestar_info.get("balance_deficit", 0.0),
                "scrape_ts": orestar_info.get("ts", 0),
            }

        # Classify the per-year deltas now that the loop above has filled them.
        _dvals = [round(float(v.get("discrepancy", 0) or 0), 2)
                  for _, v in sorted(yearly_discrepancies.items())]
        if len(_dvals) >= 2:
            if max(_dvals) - min(_dvals) <= 0.01:
                # Same every year: the years reconcile, the start does not.
                discrepancy_signature = "opening_balance"
            else:
                discrepancy_signature = "missing_transactions"
                # Name the first year the delta moves — that is where rows went
                # missing, and it turns "this committee is off by $X" into
                # somewhere to look.
                _yrs = sorted(yearly_discrepancies)
                for _i in range(1, len(_yrs)):
                    if abs(_dvals[_i] - _dvals[_i - 1]) > 0.01:
                        discrepancy_first_bad_year = int(_yrs[_i])
                        break

        detail = {
            "name": name, "slug": slug,
            "total_in": total_in, "total_inkind": total_inkind,
            "total_out": total_out,
            "total_or": total_or, "total_od": total_od,
            "cash_on_hand": cash_on_hand, "tran_count": tran_count,
            "cash_on_hand_source": cash_on_hand_source,
            "has_orestar_check": has_orestar_check,
            "orestar_discrepancy": orestar_discrepancy,
            # Surfaced on the site as a "Closed" label, so a reader can
            # tell a finished committee from one that is merely quiet.
            "closed": is_closed,
            "closed_since": closed_since,
            "closed_final_balance": closed_final_balance,
            "orestar_year": orestar_year,
            "orestar_account_summary": _acct_summary,
            "orestar_yearly": _name_to_yearly.get(name, {}),
            "yearly_discrepancies": yearly_discrepancies,
            # What KIND of problem this committee has, read off the shape of
            # the per-year deltas rather than their size.
            #
            # A discrepancy that is the SAME every year means each year's
            # activity reconciles and the divergence predates all of them —
            # the opening balance is wrong. One that CHANGES in a given year
            # means transactions are missing in that year, and names the year.
            #
            # Citizens for Mannix is $299,796.16 adrift in 2006, 2007, 2008,
            # 2009 and 2010 — identical to the cent. Its 2006+ row count
            # matches ORESTAR exactly (339), so nothing is missing; it starts
            # from the wrong place. Across everything flagged, 201 committees
            # ($2.6M) carry a constant offset against 101 ($1.2M) that vary,
            # so most of what survived the row recovery was never fetchable.
            "discrepancy_signature": discrepancy_signature,
            "discrepancy_first_bad_year": discrepancy_first_bad_year,
            # Surfaced so a reader can see WHY the opening balance was
            # taken from ORESTAR rather than from our own rows.
            "opening_reconciles": opening_reconciles,
            "opening_our_prior_net": opening_our_prior_net,
            "beginning_balances": beginning_balances,
            "timeline": timeline,
            "top_donors": top_donors_list,
            "top_donors_by_year": top_donors_by_year,
            "top_payees": top_payees_list,
            "top_payees_by_year": top_payees_by_year,
            "by_contributor_type": by_type_list,
            "by_contributor_type_by_year": by_type_by_year_filer,
            "by_contributor_type_by_month": by_type_by_month_filer,
        }

        out_path = filers_dir / f"{slug}.json"
        with open(out_path, "w") as f:
            json.dump(detail, f, separators=(",", ":"), default=str)

        _fid_for_index = ""
        if _filer_id_col:
            _fid_for_index = _name_to_fid.get(name, "")
        # Attach scraped metadata (party, office, committee type) if available
        _meta = _filer_metadata.get(_fid_for_index, {}) if _fid_for_index else {}
        _party = _meta.get("party", "")
        _office_raw = _meta.get("office", "")
        _committee_type = _meta.get("committee_type", "")
        _pac_type = _meta.get("pac_type", "")
        _nature = _meta.get("nature", "")
        _candidate_name = _meta.get("candidate_name", "")
        _election = _meta.get("election", "")
        # Normalize office to just the title (strip district number)
        # e.g. "State Representative, 25th District" → "State Representative"
        _office = _office_raw.split(",")[0].strip() if _office_raw else ""

        # Attach leadership role if available
        _leader = _leadership.get(_fid_for_index, {}) if _fid_for_index else {}
        _leadership_role = _leader.get("role_title", "")
        # Assign leadership tier:
        #   1 = Speaker of House / President of Senate (top)
        #   2 = Majority Leaders / Ways & Means Co-Chairs
        #   3 = Chairs, Pro Tems, other leadership
        _leadership_tier = 0
        if _leadership_role:
            _lr = _leadership_role.lower()
            if "speaker of the house" in _lr or "senate president" == _lr.strip() or (
                "president" in _lr and "pro" not in _lr
            ):
                _leadership_tier = 1
            elif ("majority leader" in _lr and "deputy" not in _lr and "assistant" not in _lr) \
                    or "ways and means co-chair" in _lr:
                _leadership_tier = 2
            else:
                _leadership_tier = 3

        _filer_detail_rows.append(
            {"slug": slug, "name": name, "filer_id": _fid_for_index, "detail": detail}
        )

        index_rows.append({
            "slug": slug, "name": name,
            "filer_id": _fid_for_index,
            "total_in": total_in, "total_inkind": total_inkind,
            "total_out": total_out,
            "cash_on_hand": cash_on_hand,
            "party": _party,
            "office": _office,
            "office_district": _office_raw,
            "committee_type": _committee_type,
            "pac_type": _pac_type,
            "nature": _nature,
            "candidate_name": _candidate_name,
            "election": _election,
            "leadership_role": _leadership_role,
            "leadership_tier": _leadership_tier,
        })

    # Sort index by total_in descending
    index_rows.sort(key=lambda r: r["total_in"], reverse=True)
    _write_json("filer_index.json", index_rows)
    # Mirror per-filer detail blobs into Postgres (filer_detail table).
    try:
        supabase_sync.bulk_upsert_filer_detail(_filer_detail_rows)
    except Exception as e:
        log.warning("filer_detail sync failed: %s", e)
    log.info(
        "Wrote filer_index.json (%d filers) and %d filer detail files",
        len(index_rows), len(index_rows),
    )

    # ── balance_discrepancies.json — where we disagree with ORESTAR ─────────
    #
    # Every committee is checked against its own ORESTAR account summary. Most
    # agree; the ones that do not are worth a human look, because a gap means
    # either our transaction data is incomplete or ORESTAR's own arithmetic is.
    # Precomputed here rather than derived in the browser: filer_detail holds
    # 7,268 large JSON blobs, and the admin page should not have to pull them
    # all down to find the couple of thousand that matter.
    _disc_rows = []
    for _row in _filer_detail_rows:
        _d = _row["detail"]
        _acct = _d.get("orestar_account_summary") or {}
        if _acct.get("ending_cash_balance") is None:
            continue                      # never checked — not a discrepancy
        _delta = round(_d.get("cash_on_hand", 0.0) - _acct["ending_cash_balance"], 2)
        if abs(_delta) <= 0.01:
            continue
        _disc_rows.append({
            "slug": _row["slug"], "name": _row["name"], "filer_id": _row.get("filer_id"),
            "calculated": _d.get("cash_on_hand", 0.0),
            "orestar": _acct["ending_cash_balance"],
            "delta": _delta,
            "orestar_year": _acct.get("year"),
            "scrape_ts": _acct.get("scrape_ts"),
            "tran_count": _d.get("tran_count", 0),
            # A committee with no activity in ORESTAR's year is a different
            # animal from one that is actively filing and still disagrees.
            "dormant": str(_acct.get("year")) not in (_d.get("beginning_balances") or {}),
            # A closed committee's discrepancy is a different kind of thing
            # from an active one's, and the Admin tab should say so rather
            # than listing them side by side as if equivalent.
            "closed": bool(_d.get("closed")),
            "closed_since": _d.get("closed_since"),
            # "opening_balance" or "missing_transactions" — the Admin tab can
            # then group by the kind of problem instead of listing an
            # undifferentiated dollar figure per committee.
            "signature": _d.get("discrepancy_signature"),
            "first_bad_year": _d.get("discrepancy_first_bad_year"),
        })
    _disc_rows.sort(key=lambda r: -abs(r["delta"]))
    _write_json("balance_discrepancies.json", {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "checked": sum(1 for r in _filer_detail_rows
                       if (r["detail"].get("orestar_account_summary") or {})
                          .get("ending_cash_balance") is not None),
        "flagged": len(_disc_rows),
        "rows": _disc_rows,
    })

    # ── by_party_type.json — Donor type composition by party by year ────────
    # Cross-references filer_index party data with per-filer donor type
    # breakdowns to produce a partisan comparison dataset.
    _party_type_by_year: dict[str, dict[str, dict[str, dict]]] = {}
    _party_filer_count = {"Democrat": 0, "Republican": 0}

    for row in index_rows:
        _party = row.get("party", "")
        _pac_type = row.get("pac_type", "")

        # Include candidate committees with party
        if _party in ("Democrat", "Republican"):
            pass  # use _party as-is
        # Include caucus PACs — party determined from ORESTAR's "Nature of
        # Committee" field (e.g., "Supporting House Democratic Candidates").
        # ORESTAR PAC pages have no explicit party field; the nature text
        # is the authoritative source scraped directly from the SOO page.
        elif _pac_type == "Caucus":
            _nature = (row.get("nature", "") or "").lower()
            if "democrat" in _nature:
                _party = "Democrat"
            elif "republican" in _nature:
                _party = "Republican"
            else:
                continue  # Can't determine party
        else:
            continue
        _slug = row["slug"]
        _detail_path = filers_dir / f"{_slug}.json"
        if not _detail_path.exists():
            continue
        _party_filer_count[_party] += 1
        with open(_detail_path) as f:
            _detail = json.load(f)
        _by_type_year = _detail.get("by_contributor_type_by_year", {})
        for _yr, _type_rows in _by_type_year.items():
            if _yr not in _party_type_by_year:
                _party_type_by_year[_yr] = {}
            if _party not in _party_type_by_year[_yr]:
                _party_type_by_year[_yr][_party] = {}
            for _tr in _type_rows:
                _t = _tr["type"]
                if _t not in _party_type_by_year[_yr][_party]:
                    _party_type_by_year[_yr][_party][_t] = {
                        "total": 0.0, "donors": {}
                    }
                _party_type_by_year[_yr][_party][_t]["total"] += _tr.get("total", 0)
                for _d in _tr.get("top_donors", []):
                    _donors = _party_type_by_year[_yr][_party][_t]["donors"]
                    _donors[_d["name"]] = _donors.get(_d["name"], 0) + _d["total"]

    # Flatten to output format
    _party_output: dict = {"by_year": {}, "meta": {
        "democrat_committees": _party_filer_count["Democrat"],
        "republican_committees": _party_filer_count["Republican"],
    }}
    for _yr in sorted(_party_type_by_year.keys()):
        _party_output["by_year"][_yr] = {}
        for _p in ("Democrat", "Republican"):
            _types = _party_type_by_year[_yr].get(_p, {})
            _party_output["by_year"][_yr][_p] = [
                {
                    "type": _t,
                    "total": round(_data["total"], 2),
                    "top_donors": sorted(
                        [{"name": _n, "total": round(_v, 2)}
                         for _n, _v in _data["donors"].items()],
                        key=lambda x: -x["total"],
                    )[:5],
                }
                for _t, _data in sorted(
                    _types.items(), key=lambda x: -x[1]["total"]
                )
            ]

    _write_json("by_party_type.json", _party_output)
    log.info(
        "Wrote by_party_type.json (D: %d committees, R: %d committees)",
        _party_filer_count["Democrat"], _party_filer_count["Republican"],
    )

    # ── activity_snapshot.json — Campaign Pulse module data ────────────────
    from generate_activity_snapshot import generate as _gen_snapshot
    _snapshot = _gen_snapshot(agg_dir=AGG_DIR, filers_dir=filers_dir)
    _write_json("activity_snapshot.json", _snapshot)
    log.info(
        "Wrote activity_snapshot.json (%d candidates)",
        _snapshot["meta"]["total_candidates"],
    )

    # ── Build donor → filer mapping for clickable donor links ──────────────
    # For each filer, build a lookup by normalized name and by filer ID.
    # Then check top donor names against filer names for linking.
    _norm_cache: dict[str, str] = {}  # normalized → canonical filer name

    def _normalize_name(n: str) -> str:
        """Normalize a name for matching: lowercase, strip punctuation/whitespace."""
        import re
        n = n.lower().strip()
        n = re.sub(r"[''`]s\b", "s", n)  # possessives
        n = re.sub(r"[^a-z0-9\s]", "", n)  # strip punctuation
        n = re.sub(r"\s+", " ", n).strip()
        # Common suffixes
        for suffix in [" pac", " committee", " cmte", " comm", " political action committee",
                       " for oregon", " for or"]:
            if n.endswith(suffix):
                n = n[:-len(suffix)].strip()
        return n

    filer_by_norm: dict[str, list] = {}  # normalized name → list of {slug, name, filer_id}
    filer_by_fid: dict[str, dict] = {}   # filer_id → {slug, name}

    for row in index_rows:
        norm = _normalize_name(row["name"])
        entry = {"slug": row["slug"], "name": row["name"], "filer_id": row.get("filer_id", "")}
        filer_by_norm.setdefault(norm, []).append(entry)
        if entry["filer_id"]:
            filer_by_fid[entry["filer_id"]] = entry

    # Collect all unique donor names from top_donors.json data
    # (We'll read it back since it was already written)
    donor_filer_map = {}  # donor_name_lower → {slug, name, confidence}
    top_donors_path = AGG_DIR / "top_donors.json"
    if top_donors_path.exists():
        import json as _json
        with open(top_donors_path) as _f:
            _td = _json.load(_f)
        all_donor_names = set()
        for d in _td.get("all_time", []):
            all_donor_names.add(d["name"])
        for yr_list in _td.get("by_year", {}).values():
            for d in yr_list:
                all_donor_names.add(d["name"])

        # Regex for extracting parenthetical filer IDs from donor names
        # e.g. "Oregon Hospital Political Action Committee (161)"
        _PAREN_FID_RE = re.compile(r"\((\d+)\)\s*$")

        for donor_name in all_donor_names:
            dn_lower = donor_name.lower().strip()
            dn_norm = _normalize_name(donor_name)

            # 1. Exact name match (case-insensitive)
            exact_matches = [r for r in index_rows if r["name"].lower().strip() == dn_lower]
            if len(exact_matches) == 1:
                donor_filer_map[dn_lower] = {
                    "slug": exact_matches[0]["slug"],
                    "name": exact_matches[0]["name"],
                    "confidence": "high",
                }
                continue

            # 2. Normalized name match
            norm_matches = filer_by_norm.get(dn_norm, [])
            if len(norm_matches) == 1:
                donor_filer_map[dn_lower] = {
                    "slug": norm_matches[0]["slug"],
                    "name": norm_matches[0]["name"],
                    "confidence": "high",
                }
                continue

            # 3. Parenthetical filer ID match — e.g. "Name Here (12345)"
            # Check if the donor name ends with a number in parentheses
            # and that number corresponds to a filer ID.
            paren_m = _PAREN_FID_RE.search(donor_name)
            if paren_m:
                paren_fid = paren_m.group(1)
                if paren_fid in filer_by_fid:
                    filer_entry = filer_by_fid[paren_fid]
                    # Sanity check: some name similarity between donor and filer
                    donor_base = donor_name[:paren_m.start()].strip().lower()
                    filer_name_lower = filer_entry["name"].lower()
                    # Check if any significant word (3+ chars) from donor base
                    # appears in the filer name, or vice versa
                    donor_words = {w for w in donor_base.split() if len(w) >= 3}
                    filer_words = {w for w in filer_name_lower.split() if len(w) >= 3}
                    overlap = donor_words & filer_words
                    if overlap or donor_base in filer_name_lower or filer_name_lower in donor_base:
                        donor_filer_map[dn_lower] = {
                            "slug": filer_entry["slug"],
                            "name": filer_entry["name"],
                            "confidence": "high",
                        }
                        continue

            # 4. Multiple matches → ambiguous
            if len(norm_matches) > 1 or len(exact_matches) > 1:
                matches = exact_matches if len(exact_matches) > 1 else norm_matches
                donor_filer_map[dn_lower] = {
                    "candidates": [{"slug": m["slug"], "name": m["name"]} for m in matches],
                    "confidence": "ambiguous",
                }

    _write_json("donor_filer_map.json", donor_filer_map)
    log.info("Wrote donor_filer_map.json (%d linked, %d ambiguous)",
             sum(1 for v in donor_filer_map.values() if v.get("confidence") == "high"),
             sum(1 for v in donor_filer_map.values() if v.get("confidence") == "ambiguous"))

    # Return global COH data so aggregate() can update summary.json
    return {
        "global_cash_on_hand": round(sum(r["cash_on_hand"] for r in index_rows), 2),
        "global_beginning_balances": _global_beginning_balances,
    }


def _write_json(filename: str, data) -> None:
    path = AGG_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"), default=str)
    log.info("Wrote %s (%d bytes)", filename, path.stat().st_size)
    # Mirror the aggregate blob into Postgres (dashboard_cache) so the frontend
    # reads it from Supabase instead of this file. No-ops without credentials.
    try:
        supabase_sync.upsert_dashboard_cache(filename.removesuffix(".json"), data)
    except Exception as e:  # never let a sync hiccup break the file pipeline
        log.warning("dashboard_cache sync for %s failed: %s", filename, e)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_combined_csv() -> Path:
    """Concatenate all per-year shards into one gzip CSV for the full download.
    Streams shard-by-shard to bound memory."""
    out = DATA_DIR / "transactions_full.csv.gz"
    shards = sorted(TRANS_DIR.glob("txn_*.csv.gz"))
    with gzip.open(out, "wt", newline="") as w:
        for i, shard in enumerate(shards):
            with gzip.open(shard, "rt") as r:
                header = r.readline()
                if i == 0:
                    w.write(header)
                shutil.copyfileobj(r, w)
    log.info("Built combined full CSV %s (%d shards)", out.name, len(shards))
    return out


if __name__ == "__main__":
    import os
    import sys

    # Every Supabase write in this file is a silent no-op when SUPABASE_DB_URL
    # is unset. That is right for local runs, but in CI it meant three
    # scheduled workflows spent an hour scraping, wrote correct JSON to the
    # repo, exited 0 — and left the live site reading days-old data, with
    # nothing in the log to say so. Say it loudly instead of returning quietly.
    if not supabase_sync.sync_enabled():
        _where = "CI" if os.environ.get("GITHUB_ACTIONS") else "this machine"
        print("=" * 72, file=sys.stderr)
        print(f"WARNING: SUPABASE_DB_URL is not set on {_where}.", file=sys.stderr)
        print("         Files on disk will be updated; the LIVE SITE WILL NOT.",
              file=sys.stderr)
        print("=" * 72, file=sys.stderr)
    if "--supabase-full-load" in sys.argv:
        # One-time / periodic: load every transaction shard into Postgres and
        # publish the combined full-dataset CSV to Storage. Run once after the
        # migrations are applied, then rely on the daily incremental sync.
        log.info("Supabase full load: reloading all transaction shards…")
        supabase_sync.full_reload_transactions(TRANS_DIR)
        try:
            supabase_sync.upload_full_csv(_build_combined_csv())
        except Exception as e:
            log.warning("full CSV upload failed: %s", e)
    elif "--merge-only" in sys.argv:
        # Just merge raw Excel into txn_YYYY.csv.gz — skip aggregation.
        # Used by filer-targeted backfill to save time; the daily refresh
        # will run the full aggregation later.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
        log.info("Merge-only mode: loading raw Excel files and updating transaction files")
        new_df = load_excel_files(RAW_DIR)
        if new_df.empty:
            log.info("No new data to merge.")
        else:
            existing = _load_all_transactions()
            df = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
            df["tran_id"] = df["tran_id"].astype(str).str.strip()
            before = len(df)
            df = df.drop_duplicates(subset=["tran_id"], keep="last")
            log.info("Deduplicated by tran_id: %d → %d rows", before, len(df))
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
            # Normalize filed_date to YYYY-MM-DD (ORESTAR Excel uses MM/DD/YYYY)
            if "filed_date" in df.columns:
                _fd = pd.to_datetime(df["filed_date"], format="mixed", dayfirst=False, errors="coerce")
                df["filed_date"] = _fd.dt.strftime("%Y-%m-%d").fillna(df["filed_date"])
                df, _superseded = _drop_superseded(df)
                _save_transactions(df)
            else:
                log.warning("No filed_date column — cannot split by year")

            # Push the freshly-merged rows to Postgres NOW, not on tomorrow's
            # daily refresh.
            #
            # This used to write year shards and stop, which made the database
            # lag the shards by up to a day. That was tolerable when raw Excel
            # files were kept on disk as the resume record — but those files are
            # the reason the repository reached 80 GB, and the fetcher's
            # "do we already hold this window?" check reads POSTGRES. Leaving
            # the database behind would make that check answer "no" for rows we
            # had just downloaded, and the next chain run would fetch them all
            # over again.
            try:
                _new_ids = set(new_df["tran_id"].astype(str).str.strip())
                _changed = df[df["tran_id"].astype(str).str.strip().isin(_new_ids)]
                supabase_sync.upsert_transactions(_changed)
                if _superseded:
                    supabase_sync.delete_transactions(_superseded)
                log.info("Synced %d merged rows to Postgres", len(_changed))
            except Exception as e:
                log.warning("merge-only transaction sync failed: %s", e)

            # Raw files are now disposable. Every one of them has been merged
            # into the year shards and pushed to Postgres, and the fetcher
            # decides what to skip from ORESTAR's record counts plus what the
            # database holds — not from what happens to be left on disk.
            #
            # Keeping them cost 10,205 committed files and 80 GB of repository,
            # because git retains every version of every one forever.
            cleaned = 0
            for f in RAW_DIR.glob("*.xls*"):
                f.unlink()
                cleaned += 1
            log.info("Merge complete. Cleaned %d raw files (all merged and synced).", cleaned)
    else:
        process()
