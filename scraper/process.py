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
TRANSACTIONS  = DATA_DIR / "transactions.csv.gz"
COMMITTEES    = DATA_DIR / "committees.csv"

AGG_DIR.mkdir(parents=True, exist_ok=True)

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
    # Apply existing entity_map first
    resolved = {n: entity_map.get(n, n) for n in names}

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

    unique_names = list(set(resolved.values()))
    review_items = []

    log.info("Fuzzy-deduplicating %d unique names…", len(unique_names))
    for i, name_a in enumerate(unique_names):
        if i % 500 == 0 and i > 0:
            log.debug("  … %d / %d", i, len(unique_names))
        # rapidfuzz process.extract returns top matches
        matches = rfuzz_process.extract(
            name_a,
            unique_names,
            scorer=fuzz.token_sort_ratio,
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

    # Build canonical map: raw_name → canonical_name (highest frequency in cluster)
    cluster_freq: dict[str, dict[str, int]] = defaultdict(dict)
    for n in unique_names:
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
            # Derive tran_type from filename: C_2026-... → "C", E_2026-... → "E"
            # ORESTAR does not include a transaction type column in the export.
            first_char = f.stem[0].upper()
            df["tran_type"] = first_char if first_char in ("C", "E") else ""
            frames.append(df)
        except Exception as exc:
            log.warning("Failed to read %s: %s", f.name, exc)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    log.info("Loaded %d rows from %d files", len(combined), len(frames))
    return combined


# ---------------------------------------------------------------------------
# Main processing pipeline
# ---------------------------------------------------------------------------

def process() -> None:
    entity_map = load_entity_map()

    # ── 1. Load raw Excel files ──────────────────────────────────────────────
    new_df = load_excel_files(RAW_DIR)

    if new_df.empty:
        log.info("No new data to process.")
        # If transactions.csv.gz already exists, still re-aggregate for JSON output
        if TRANSACTIONS.exists():
            log.info("Re-aggregating from existing transactions.csv.gz")
            df = pd.read_csv(TRANSACTIONS, compression="gzip", dtype=str)
        else:
            log.info("No existing transactions data. Nothing to do.")
            return
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

        # ── 3. Load existing transactions and merge ───────────────────────────
        if TRANSACTIONS.exists():
            log.info("Loading existing transactions.csv.gz…")
            existing = pd.read_csv(TRANSACTIONS, compression="gzip", dtype=str)
            # Convert amount back to float
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
        df["contributor_type_label"] = df["contributor_type"].map(
            lambda x: CONTRIBUTOR_TYPE_LABELS.get(x.strip().upper(), x)
        )

        # ── 7. Save review queue ──────────────────────────────────────────────
        if review_items:
            log.info("Writing %d review items to %s", len(review_items), REVIEW_QUEUE)
            with open(REVIEW_QUEUE, "w") as f:
                json.dump(review_items, f, indent=2)

        # ── 8. Write updated transactions.csv.gz ─────────────────────────────
        log.info("Writing %d transactions to %s", len(df), TRANSACTIONS)
        df["filed_date"] = df["filed_date"].astype(str)
        df.to_csv(TRANSACTIONS, index=False, compression="gzip")

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
    df["amount"] = df["amount"].apply(to_float)
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce")
    df = df.dropna(subset=["filed_date"])
    df["year"]  = df["filed_date"].dt.year.astype(int)
    df["month"] = df["filed_date"].dt.to_period("M").astype(str)

    # Use canonical names if available, fall back to raw
    contrib_col = "contributor_payee_canonical" if "contributor_payee_canonical" in df.columns else "contributor_payee"
    filer_col   = "filer_canonical" if "filer_canonical" in df.columns else "filer"

    contributions = df[df["tran_type"].str.strip().str.upper() == "C"]
    expenditures  = df[df["tran_type"].str.strip().str.upper() == "E"]

    # ── summary.json ─────────────────────────────────────────────────────────
    summary = {
        "total_contributions":  round(contributions["amount"].sum(), 2),
        "total_expenditures":   round(expenditures["amount"].sum(), 2),
        "total_transactions":   int(len(df)),
        "num_contributions":    int(len(contributions)),
        "num_expenditures":     int(len(expenditures)),
        "date_range_start":     df["filed_date"].min().strftime("%Y-%m-%d") if len(df) else "",
        "date_range_end":       df["filed_date"].max().strftime("%Y-%m-%d") if len(df) else "",
        "last_updated":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_json("summary.json", summary)

    # ── top_donors.json ───────────────────────────────────────────────────────
    top_donors_all = (
        contributions.groupby(contrib_col)["amount"]
        .sum()
        .nlargest(100)
        .reset_index()
        .rename(columns={contrib_col: "name", "amount": "total"})
    )
    top_donors_all["total"] = top_donors_all["total"].round(2)

    by_year_donors: dict[str, list] = {}
    for yr in sorted(contributions["year"].unique()):
        yr_df = contributions[contributions["year"] == yr]
        top = (
            yr_df.groupby(contrib_col)["amount"]
            .sum()
            .nlargest(100)
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
        contributions.groupby(filer_col)["amount"]
        .sum()
        .nlargest(100)
        .reset_index()
        .rename(columns={filer_col: "name", "amount": "total"})
    )
    top_recipients_all["total"] = top_recipients_all["total"].round(2)

    by_year_recipients: dict[str, list] = {}
    for yr in sorted(contributions["year"].unique()):
        yr_df = contributions[contributions["year"] == yr]
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
    contrib_monthly = (
        contributions.groupby("month")["amount"].sum().reset_index()
        .rename(columns={"amount": "contributions"})
    )
    expend_monthly = (
        expenditures.groupby("month")["amount"].sum().reset_index()
        .rename(columns={"amount": "expenditures"})
    )
    timeline = pd.merge(contrib_monthly, expend_monthly, on="month", how="outer").fillna(0)
    timeline = timeline.sort_values("month")
    timeline["contributions"] = timeline["contributions"].round(2)
    timeline["expenditures"]  = timeline["expenditures"].round(2)
    _write_json("timeline.json", timeline.to_dict(orient="records"))

    # ── by_contributor_type.json ──────────────────────────────────────────────
    type_col = "contributor_type_label" if "contributor_type_label" in contributions.columns else "contributor_type"
    by_type = (
        contributions.groupby(type_col)["amount"]
        .sum()
        .reset_index()
        .rename(columns={type_col: "type", "amount": "total"})
        .sort_values("total", ascending=False)
    )
    by_type["total"] = by_type["total"].round(2)
    _write_json("by_contributor_type.json", by_type.to_dict(orient="records"))

    # ── by_party.json ─────────────────────────────────────────────────────────
    if "party" in contributions.columns:
        by_party = (
            contributions[contributions["party"].ne("")]
            .groupby("party")["amount"]
            .sum()
            .reset_index()
            .rename(columns={"amount": "total"})
            .sort_values("total", ascending=False)
        )
        by_party["total"] = by_party["total"].round(2)
        _write_json("by_party.json", by_party.to_dict(orient="records"))
    else:
        _write_json("by_party.json", [])

    # ── by_office_type.json ───────────────────────────────────────────────────
    if "office" in contributions.columns:
        def categorize_office(o: str) -> str:
            o = str(o).lower()
            if "governor" in o:               return "Governor"
            if "legislat" in o or "rep" in o or "senator" in o: return "State Legislature"
            if "secretary" in o:              return "Secretary of State"
            if "treasurer" in o:              return "State Treasurer"
            if "attorney" in o:               return "Attorney General"
            if "ballot" in o or "measure" in o: return "Ballot Measure"
            if "county" in o or "city" in o or "local" in o: return "Local / County"
            if o.strip() == "":               return "Unknown"
            return "Other Statewide"

        contributions = contributions.copy()
        contributions["office_category"] = contributions["office"].apply(categorize_office)
        by_office = (
            contributions.groupby("office_category")["amount"]
            .sum()
            .reset_index()
            .rename(columns={"office_category": "office_type", "amount": "total"})
            .sort_values("total", ascending=False)
        )
        by_office["total"] = by_office["total"].round(2)
        _write_json("by_office_type.json", by_office.to_dict(orient="records"))
    else:
        _write_json("by_office_type.json", [])

    # ── recent_transactions.json ──────────────────────────────────────────────
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=12)
    recent = df[df["filed_date"] >= cutoff].copy()
    recent["filed_date"] = recent["filed_date"].dt.strftime("%Y-%m-%d")
    recent["amount"] = recent["amount"].round(2)

    keep_cols = [c for c in [
        "tran_id", "filed_date", "tran_type", "amount",
        contrib_col, filer_col, "contributor_type_label", "office", "party", "purpose"
    ] if c in recent.columns]
    recent = recent[keep_cols].rename(columns={
        contrib_col: "contributor_payee",
        filer_col:   "filer",
    })
    recent = recent.sort_values("filed_date", ascending=False).head(5000)
    _write_json("recent_transactions.json", recent.to_dict(orient="records"))

    log.info("Aggregation complete. JSON files written to %s", AGG_DIR)


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
