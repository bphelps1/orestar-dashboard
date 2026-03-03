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
            # Normalize column names
            df.columns = [c.strip().lower() for c in df.columns]
            # Rename to internal names
            df = df.rename(columns={k: v for k, v in COL_MAP.items() if k in df.columns})
            df["_source_file"] = f.name
            # Derive tran_type from filename prefix: C_2026-...→"C", OR_2026-...→"OR"
            # ORESTAR does not include a transaction type column in the export.
            _valid_types = {"C", "E", "O", "OA", "OD", "OR"}
            _prefix = f.stem.split("_")[0].upper()
            df["tran_type"] = _prefix if _prefix in _valid_types else ""
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
            review_threshold=85.0,
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

    # ── 9. Aggregate JSON files ───────────────────────────────────────────────
    aggregate(df)

    # ── 10. Delete raw Excel files ────────────────────────────────────────────
    deleted = 0
    for f in RAW_DIR.glob("*.xlsx"):
        f.unlink()
        deleted += 1
    if deleted:
        log.info("Deleted %d raw Excel files from %s", deleted, RAW_DIR)


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

    # Use canonical names if available, fall back to raw
    contrib_col = "contributor_payee_canonical" if "contributor_payee_canonical" in df.columns else "contributor_payee"
    filer_col   = "filer_canonical" if "filer_canonical" in df.columns else "filer"

    ttype = df["tran_type"].str.strip().str.upper()
    contributions   = df[ttype == "C"]
    expenditures    = df[ttype == "E"]
    other_receipts  = df[ttype.isin({"OR", "O", "OA"})]   # Other Receipt, Other, Other AR
    other_disburse  = df[ttype == "OD"]                    # Other Disbursement

    # Separate cash contributions from in-kind (forgiven expenditures, etc.)
    inkind_mask     = contributions["sub_type"].str.contains("In-Kind", case=False, na=False)
    cash_contribs   = contributions[~inkind_mask]
    inkind_contribs = contributions[inkind_mask]

    # ── summary.json ─────────────────────────────────────────────────────────
    summary = {
        "total_contributions":  round(cash_contribs["amount"].sum(), 2),
        "total_inkind":         round(inkind_contribs["amount"].sum(), 2),
        "total_expenditures":   round(expenditures["amount"].sum(), 2),
        "total_other_receipts":  round(other_receipts["amount"].sum(), 2),
        "total_other_disburse":  round(other_disburse["amount"].sum(), 2),
        "total_transactions":   int(len(df)),
        "num_contributions":    int(len(cash_contribs)),
        "num_inkind":           int(len(inkind_contribs)),
        "num_expenditures":     int(len(expenditures)),
        "date_range_start":     df["filed_date"].min().strftime("%Y-%m-%d") if len(df) else "",
        "date_range_end":       df["filed_date"].max().strftime("%Y-%m-%d") if len(df) else "",
        "last_updated":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_json("summary.json", summary)

    # ── top_donors.json ───────────────────────────────────────────────────────
    top_donors_all = (
        cash_contribs.groupby(contrib_col)["amount"]
        .sum()
        .nlargest(500)
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
            .nlargest(500)
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
    timeline = (
        cash_monthly
        .merge(inkind_monthly, on="month", how="outer")
        .merge(expend_monthly, on="month", how="outer")
        .fillna(0)
        .sort_values("month")
    )
    timeline["contributions"] = timeline["contributions"].round(2)
    timeline["inkind"]        = timeline["inkind"].round(2)
    timeline["expenditures"]  = timeline["expenditures"].round(2)
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
        by_type_by_year[str(int(yr))] = [
            {k: v for k, v in r.items() if k != "top_donors"} for r in yr_rows
        ]
    _write_json("by_contributor_type.json", {
        "all_time": _type_rows(cash_contribs),
        "by_year":  by_type_by_year,
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
    aggregate_filers(df, cash_contribs, inkind_contribs, expenditures,
                     other_receipts, other_disburse, filer_col, contrib_col)

    log.info("Aggregation complete. JSON files written to %s", AGG_DIR)


def aggregate_filers(
    df: pd.DataFrame,
    contributions: pd.DataFrame,   # cash contributions only (C type, non-inkind)
    inkind_contribs: pd.DataFrame,
    expenditures: pd.DataFrame,    # E type
    other_receipts: pd.DataFrame,  # OR/O/OA type (e.g. Return/Refund, Misc Receipt)
    other_disburse: pd.DataFrame,  # OD type (e.g. Misc Other Disbursement)
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
    all_groups      = df.groupby(filer_col)

    def get_group(groups, name):
        return groups.get_group(name) if name in groups.groups else pd.DataFrame()

    def monthly_sum(frame, col_name):
        if frame.empty or "month" not in frame.columns:
            return pd.Series(dtype=float, name=col_name)
        return frame.groupby("month")["amount"].sum().rename(col_name)

    # ── Per-filer detail files ────────────────────────────────────────────────
    index_rows = []
    log.info("Generating per-filer detail files for %d filers…", len(all_filer_names))

    for name in all_filer_names:
        slug = filer_slugs[name]
        filer_contrib = get_group(contrib_groups, name)
        filer_inkind  = get_group(inkind_groups,  name)
        filer_expend  = get_group(expend_groups,  name)
        filer_or      = get_group(or_groups,      name)
        filer_od      = get_group(od_groups,      name)
        filer_all     = get_group(all_groups,     name)

        # total_in/total_out: strict "Cash Contribution" / "Cash Expenditure" sub_type only
        _cash_contrib   = filer_contrib[filer_contrib["sub_type"] == "Cash Contribution"] if not filer_contrib.empty else filer_contrib
        _cash_expend    = filer_expend[filer_expend["sub_type"] == "Cash Expenditure"]    if not filer_expend.empty  else filer_expend
        total_in        = round(float(_cash_contrib["amount"].sum()) if not _cash_contrib.empty else 0.0, 2)
        total_inkind    = round(float(filer_inkind["amount"].sum())  if not filer_inkind.empty  else 0.0, 2)
        total_out       = round(float(_cash_expend["amount"].sum())  if not _cash_expend.empty  else 0.0, 2)
        total_or        = round(float(filer_or["amount"].sum())      if not filer_or.empty      else 0.0, 2)
        total_od        = round(float(filer_od["amount"].sum())      if not filer_od.empty      else 0.0, 2)
        # cash_on_hand: include all C-type receipts and all E-type disbursements
        # EXCEPT "Personal Expenditure for Reimbursement" — that sub_type records the
        # obligation (candidate spent own money) but the actual cash outflow is the
        # subsequent reimbursement "Cash Expenditure" to the candidate. Including both
        # would double-count the amount.
        _e_for_coh      = filer_expend[filer_expend["sub_type"] != "Personal Expenditure for Reimbursement"] if not filer_expend.empty else filer_expend
        _total_in_full  = round(float(filer_contrib["amount"].sum()) if not filer_contrib.empty else 0.0, 2)
        _total_out_full = round(float(_e_for_coh["amount"].sum())    if not _e_for_coh.empty    else 0.0, 2)
        cash_on_hand    = round(_total_in_full + total_or - _total_out_full - total_od, 2)
        tran_count    = int(len(filer_all))

        # Timeline — use _e_for_coh so personal-expenditure-for-reimbursement
        # obligations don't inflate the expenditures series
        c_monthly = monthly_sum(filer_contrib, "contributions")
        i_monthly = monthly_sum(filer_inkind,  "inkind")
        e_monthly = monthly_sum(_e_for_coh,    "expenditures")
        tl_df = pd.concat([c_monthly, i_monthly, e_monthly], axis=1).fillna(0).sort_index()
        timeline = [
            {
                "month": m,
                "contributions": round(float(row.get("contributions", 0)), 2),
                "inkind":        round(float(row.get("inkind",        0)), 2),
                "expenditures":  round(float(row.get("expenditures",  0)), 2),
            }
            for m, row in tl_df.iterrows()
        ]

        # Top donors (who gave TO this filer) — all-time and by year
        if not filer_contrib.empty and contrib_col in filer_contrib.columns:
            td = (
                filer_contrib.groupby(contrib_col)["amount"]
                .sum().nlargest(50).reset_index()
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
                        .sum().nlargest(50).reset_index()
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
                .sum().nlargest(50).reset_index()
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
                        .sum().nlargest(50).reset_index()
                        .rename(columns={contrib_col: "name", "amount": "total"})
                    )
                    tp_yr["total"] = tp_yr["total"].round(2)
                    top_payees_by_year[str(int(yr))] = tp_yr.to_dict(orient="records")
        else:
            top_payees_list = []
            top_payees_by_year = {}

        # By contributor type — all-time (with top_donors) and by year (totals only)
        by_type_list = _filer_type_rows(filer_contrib)
        by_type_by_year_filer: dict[str, list] = {}
        if "year" in filer_contrib.columns and not filer_contrib.empty:
            for yr in sorted(filer_contrib["year"].dropna().unique()):
                yr_rows = _filer_type_rows(filer_contrib[filer_contrib["year"] == yr])
                by_type_by_year_filer[str(int(yr))] = [
                    {k: v for k, v in r.items() if k != "top_donors"} for r in yr_rows
                ]

        detail = {
            "name": name, "slug": slug,
            "total_in": total_in, "total_inkind": total_inkind,
            "total_out": total_out,
            "total_or": total_or, "total_od": total_od,
            "cash_on_hand": cash_on_hand, "tran_count": tran_count,
            "timeline": timeline,
            "top_donors": top_donors_list,
            "top_donors_by_year": top_donors_by_year,
            "top_payees": top_payees_list,
            "top_payees_by_year": top_payees_by_year,
            "by_contributor_type": by_type_list,
            "by_contributor_type_by_year": by_type_by_year_filer,
        }

        out_path = filers_dir / f"{slug}.json"
        with open(out_path, "w") as f:
            json.dump(detail, f, separators=(",", ":"), default=str)

        index_rows.append({
            "slug": slug, "name": name,
            "total_in": total_in, "total_inkind": total_inkind,
            "total_out": total_out,
            "cash_on_hand": cash_on_hand,
        })

    # Sort index by total_in descending
    index_rows.sort(key=lambda r: r["total_in"], reverse=True)
    _write_json("filer_index.json", index_rows)
    log.info(
        "Wrote filer_index.json (%d filers) and %d filer detail files",
        len(index_rows), len(index_rows),
    )


def _write_json(filename: str, data) -> None:
    path = AGG_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"), default=str)
    log.info("Wrote %s (%d bytes)", filename, path.stat().st_size)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    process()
