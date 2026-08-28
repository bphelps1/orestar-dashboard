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

def _chain_breaks(yearly: dict) -> dict:
    """Where ORESTAR's own summaries fail to carry a balance forward.

    Returns {"total": signed sum, "boundaries": count, "detail": [...]}, or an
    empty dict when the chain is intact. Purely internal to ORESTAR's data, so
    unlike summary_vs_itemised it needs no evidence about our row coverage.
    """
    yrs = sorted(y for y in (yearly or {}) if str(y).isdigit())
    if len(yrs) < 2:
        return {}
    total = 0.0
    detail = []
    for i, y in enumerate(yrs[:-1]):
        nxt = yrs[i + 1]
        end = float((yearly.get(y) or {}).get("ending_cash_balance") or 0.0)
        beg = float((yearly.get(nxt) or {}).get("beginning_balance") or 0.0)
        jump = round(beg - end, 2)
        if abs(jump) > 0.01:
            total += jump
            detail.append({"from": str(y), "to": str(nxt), "amount": jump})
    if not detail:
        return {}
    return {"total": round(total, 2), "boundaries": len(detail), "detail": detail[:12]}


_ROW_COMPLETE: dict = {}
_WITHDRAWN: dict = {}


def _row_diff() -> tuple[dict, dict]:
    """The row-level diff, when one has been run.

    Returns ({filer_id: (complete, checked_date)}, {filer_id: {withdrawn ids}}).

    diff_coverage.py compares tran_id SETS rather than counts, which is the
    only thing that can settle completeness here: ORESTAR's search returns
    superseded originals we drop on purpose, so a committee holding everything
    it should still reports fewer rows than ORESTAR, permanently. Counts have
    now been wrong in both directions — `held >= orestar` certified committees
    carrying surplus, `held == orestar` rejects committees where supersession
    worked. The diff distinguishes all three cases explicitly.

    `complete: null` means the diff could not be trusted (ORESTAR's results UI
    stops at 5,000 rows silently, so a short collection is refused rather than
    merged). That is carried through as None — unknown, not clean.
    """
    path = DATA_DIR / "coverage_diff.json"
    if not path.exists():
        return {}, {}
    try:
        complete, withdrawn = {}, {}
        for e in json.loads(path.read_text()):
            fid = str(e["filer_id"])
            checked = None
            if e.get("checked"):
                try:
                    checked = datetime.fromisoformat(str(e["checked"])).date()
                except ValueError:
                    checked = None
            if e.get("complete") is not None:
                complete[fid] = (bool(e["complete"]), checked)
            ids = {str(i) for i in (e.get("surplus") or [])}
            if ids:
                withdrawn[fid] = ids
        return complete, withdrawn
    except Exception:
        return {}, {}


def _row_completeness() -> dict:
    """filer_id -> True when we hold every row ORESTAR reports for that filer.

    From the coverage survey, which asks ORESTAR how many records it holds and
    compares that against ours. It is the only evidence that can separate "our
    data is short" from "ORESTAR's summary disagrees with ORESTAR's own
    itemised transactions", and without it the second claim cannot honestly be
    made — a line difference looks identical either way.

    Missing file is not an error: every caller treats an absent filer as
    unknown and says nothing rather than guessing.
    """
    path = DATA_DIR / "coverage_survey.json"
    if not path.exists():
        return {}
    try:
        out = {}
        for e in json.loads(path.read_text()):
            checked = None
            if e.get("checked"):
                try:
                    checked = datetime.fromisoformat(str(e["checked"])).date()
                except ValueError:
                    checked = None
            # Judged on the RAW COUNTS, not on `missing`.
            #
            # `missing` is written as max(orestar - ours, 0) — clamped — so a
            # surplus on our side arrives as 0 and is indistinguishable from a
            # perfect match. Testing `missing == 0` therefore behaves exactly
            # like `<= 0` and cannot see a surplus at all. The raw `orestar`
            # and `held` counts are recorded alongside it and are unambiguous,
            # so they decide.
            #
            # A surplus disqualifies the itemised comparison exactly as a
            # shortfall does: the claim it supports is "our line sums ARE
            # ORESTAR's itemised sums", and holding rows ORESTAR does not have
            # breaks that as thoroughly as lacking rows it does.
            #
            # Plumbers & Steamfitters PAC is the live case — 11,766 held
            # against ORESTAR's 11,750, sixteen surplus rows worth $32,284.04,
            # every one of them a withdrawn filing our append-only pipeline
            # never removed. Its re-survey recorded missing=0, which would have
            # certified it complete and published a note blaming ORESTAR for a
            # difference that is ours.
            #
            # `missing` is ORESTAR's count minus ours, so a surplus on our side
            # comes through NEGATIVE and `<= 0` waved it through as complete.
            # A surplus disqualifies the itemised comparison exactly as a
            # shortfall does: the claim it supports is "our line sums ARE
            # ORESTAR's itemised sums", and holding rows ORESTAR does not have
            # breaks that just as thoroughly as lacking rows it does.
            #
            # Plumbers & Steamfitters PAC is the live case. A backfill left it
            # holding 11,766 rows against ORESTAR's 11,750 — 16 surplus, all in
            # 2026, inflating its contributions by $32,284.04. Under `<= 0` a
            # fresh survey would have certified it complete and published a
            # note blaming ORESTAR for a difference we introduced.
            _o, _h = e.get("orestar"), e.get("held")
            if _o is not None and _h is not None:
                _complete = (int(_o) == int(_h))
            else:                       # older survey rows without the counts
                _complete = (e.get("missing") or 0) == 0
            out[str(e["filer_id"])] = (_complete, checked)
        return out
    except Exception:
        return {}


# Summaries scraped before the #90 parser fix report $0.00 in every loan field,
# so the figure can only be trusted from this instant on. Used by the non-exempt
# correction (#96) and the exempt one below.
_LOAN_FIELD_TRUSTWORTHY_FROM = 1787500800.0

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
        # Undated in ORESTAR's own record, so ORESTAR cannot place it in a
        # reporting period — and does not.
        #
        # The filed_date fallback above keeps these rows in the dataset, which
        # is right: the money is real and belongs in contributor totals. But it
        # also lands them in a YEAR, and ORESTAR's account summaries are
        # period-based, so a transaction with no transaction date appears in
        # none of them. Four rows dataset-wide carry no tran_date, and all four
        # committees were flagged for exactly their own row, to the cent:
        #
        #   6286 Friends of Sherrie Sprenger   $100.00
        #   5192 Northwest Sportfishing Ind.    $95.00
        #   2069 Oregonians for Affordable H.   $50.00
        #    145 Oregon Faculties PAC            $5.00
        #
        # Each has ONE year off — 2007, the filing year — entirely in
        # contributions. 2006 reconciles for all four, so ORESTAR is not
        # filing them under a different period; it is not counting them at all.
        # Marked here and held out of the cash balance only, so the rows keep
        # counting everywhere else.
        df["_undated"] = _tran_date.isna()
    else:
        _eff_date = df["filed_date"]
        df["_undated"] = False
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
            # When this summary was captured, carried down from the FILER level.
            #
            # The yearly file stores ts once per filer — {"years": {...}, "ts": …}
            # — not inside each year, so lifting only the year dict dropped it and
            # `orestar_info.get("ts", 0)` found nothing. 7,297 of 7,299 account
            # summaries reached the site with no timestamp at all.
            #
            # That is not cosmetic: without it there is no way to tell "ORESTAR's
            # figure is older than our transactions" from "our figures are wrong",
            # which is exactly the question the 2026 balance divergence turns on.
            # The popover already tries to show "Scraped: …" and has been printing
            # the epoch.
            "ts": float((yearly.get(str(fid)) or {}).get("ts") or 0),
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
        # Loaded once here rather than per filer: 7,263 committees would
        # otherwise re-read and re-parse the same file.
        global _ROW_COMPLETE, _WITHDRAWN
        _ROW_COMPLETE = _row_completeness()
        # The row diff wins wherever it exists. It is expensive — it pages
        # entire result sets where the survey spends one search — so it covers
        # only committees someone chose to run it on, and the count survey
        # still answers for the rest.
        _diff_complete, _WITHDRAWN = _row_diff()
        if _diff_complete:
            _ROW_COMPLETE.update(_diff_complete)
            log.info("row-level diff available for %d committees (authoritative "
                     "over the count survey); %d hold withdrawn rows",
                     len(_diff_complete), len(_WITHDRAWN))
        log.info("row-completeness known for %d committees (from the coverage survey)",
                 len(_ROW_COMPLETE))
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
        # See the _undated note above: ORESTAR's period-based summaries exclude
        # a transaction it cannot date, so the balance excludes it too. Applied
        # to the cash frames only — the rows stay in contributor totals, donor
        # aggregates and transaction listings, where they are perfectly valid.
        # Rows ORESTAR has WITHDRAWN, held out of the balance but not deleted.
        #
        # A filer can withdraw or supersede a filing; ORESTAR then stops
        # returning it, and nothing in this pipeline ever removes it. Our store
        # only drifts upward, invisibly, until something forces it into view —
        # Plumbers & Steamfitters PAC carried 16 such rows worth $32,284.04 and
        # surveyed as "missing: 0", because a count cannot see them.
        #
        # ORESTAR does not count these, so the balance must not either. The
        # rows themselves stay: they are real history, they belong in
        # contributor totals, donor aggregates and transaction listings, and
        # deleting on the strength of a scraper's output is a far riskier
        # operation than declining to add a number up. Same treatment the
        # undated rows get above, and for the same reason.
        #
        # Only from diff_coverage.py, never from a count. Identity is the only
        # evidence precise enough to name a row as withdrawn.
        _withdrawn_here = _WITHDRAWN.get(_name_to_fid.get(name, "")) or set()

        def _dated(_f):
            if _f is None or _f.empty:
                return _f
            if "_undated" in _f.columns:
                _f = _f[~_f["_undated"].fillna(False)]
            if _withdrawn_here and not _f.empty and "tran_id" in _f.columns:
                _f = _f[~_f["tran_id"].astype(str).isin(_withdrawn_here)]
            return _f
        _c_for_coh = _dated(_c_for_coh)
        _e_for_coh = _dated(_e_for_coh)
        # Disbursements and balance adjustments go through the same filter.
        # Today every withdrawn row found is a type-C contribution, so these
        # are guards rather than corrections — but a filter covering three of
        # five cash frames is the #99 bug waiting to happen again, where a
        # correction reached some consumers and not the rest.
        _od_for_coh = _dated(filer_od)
        _ba_for_coh = _dated(filer_ba)
        # Exempt loans, where ORESTAR's own summary says it did not count one.
        #
        # #96 made ORESTAR's reported figure decide for NON-exempt loans, which
        # are type C. Exempt loans are type OR and passed through here
        # untouched — which is how Committee for SAIF Keeping came to show
        # $665,242.33 of cash on hand, 32% of every flagged dollar on the
        # dashboard, for a closed committee whose 2006 statement reads $173.13
        # in, $45.00 spent, $128.13 out.
        #
        # This is the #96 rule applied to the other loan field, and it replaces
        # the narrower "ORESTAR recorded no inflow at all" test that shipped
        # first. That test was built on a measurement that was simply wrong:
        # `Loans Received (exempt)` was reported as non-zero on "three records
        # in the entire dataset", so the field looked useless as a signal. It
        # is non-zero on 39, and — the denominator that actually matters —
        # ORESTAR populates it for 39 of the 54 committee-years where we hold
        # an exempt loan, matching our figure TO THE CENT every time. It never
        # once reports a different amount. Either ORESTAR counted the loan and
        # says so, or it did not count it and reports zero.
        #
        # The split is almost perfectly chronological, which is why the narrow
        # test caught SAIF Keeping and missed the rest:
        #
        #     2006-2012     2 reported, 14 omitted
        #     2014-2026    37 reported,  1 omitted
        #
        # ORESTAR evidently changed practice around 2013. That is NOT encoded
        # as a year cutoff here. A constant would be arbitrary, would fix
        # nothing the reported figure does not already identify, and would
        # mishandle the lone 2018 omission. ORESTAR's own number draws the line
        # exactly where ORESTAR drew it.
        #
        # Measured from a pre-#106 baseline across every committee-year holding
        # an exempt loan, not only the flagged ones: 14 fixed ($853,037), 0
        # broken, 39 left alone because the two figures already agree.
        #
        # Two guards, and the first is why this can be trusted where the
        # earlier attempt could not:
        #
        #   Self-consistency. ORESTAR's lines must add up to ORESTAR's own
        #   stated ending balance. This holds for 25,058 of 25,059
        #   committee-years; the single exception is Yes! Keep Our Groceries
        #   Tax Free 2018, whose summary is $128,000 short of itself and whose
        #   balance only reconciles with the $465,000 exempt loan left IN.
        #   Without this guard that committee breaks by $465,000.
        #
        #   Evidence the record parsed. A summary that FAILED to parse is all
        #   zeros, and all-zero is trivially self-consistent, so require a
        #   non-zero figure elsewhere. One blank scrape and this is all that
        #   stands between a parse failure and deleted transactions.
        #
        # Filtering the frame rather than adjusting a total afterwards is
        # deliberate: the balance, the as-of comparison, the reconciliation and
        # the timeline all derive from _or_for_coh, and #99 was the lesson in
        # what happens when a correction reaches some of those and not others.
        # A row-level filter is exact here precisely because ORESTAR never
        # reports a partial amount — it is all or nothing.
        _or_for_coh = _dated(filer_or)  # All type OR, minus anything undated
        _exempt_dropped: dict[str, float] = {}
        _oi_early = _orestar_data.get(name)
        if (not filer_or.empty and "year" in filer_or.columns
                and "sub_type" in filer_or.columns
                and float((_oi_early or {}).get("ts") or 0) >= _LOAN_FIELD_TRUSTWORTHY_FROM):
            _uncounted_years: set[int] = set()
            for _yr_s, _ty in (_name_to_yearly.get(name, {}) or {}).items():
                if not str(_yr_s).isdigit() or not isinstance(_ty, dict):
                    continue
                def _amt_of(_k, _row=_ty):
                    return float(_row.get(_k) or 0.0)
                # ORESTAR says it counted an exempt loan that year: leave ours.
                if abs(_amt_of("loans_received_exempt")) > 0.01:
                    continue
                _implied = (_amt_of("beginning_balance") + _amt_of("contributions")
                            + _amt_of("other_receipts") + _amt_of("loans_received_exempt")
                            + _amt_of("balance_adjustments") - _amt_of("expenditures")
                            - _amt_of("other_disbursements") - _amt_of("loan_payments_exempt"))
                if abs(_implied - _amt_of("ending_cash_balance")) > 0.01:
                    continue                      # ORESTAR contradicts itself: not authoritative
                _parsed = (abs(_amt_of("beginning_balance")) + abs(_amt_of("ending_cash_balance"))
                           + abs(_amt_of("expenditures")) + abs(_amt_of("other_disbursements")))
                if _parsed <= 0.0:
                    continue                      # cannot tell a parsed record from a blank one
                _uncounted_years.add(int(_yr_s))
            if _uncounted_years:
                _mask = ((filer_or["sub_type"] == "Loan Received (Exempt)")
                         & (filer_or["year"].isin(_uncounted_years)))
                if bool(_mask.any()):
                    for _y, _a in filer_or[_mask].groupby("year")["amount"].sum().items():
                        _exempt_dropped[str(int(_y))] = round(float(_a), 2)
                    _or_for_coh = filer_or[~_mask]
                    # Recomputed so the stored lifetime figure describes the same
                    # money the timeline and the balance do.
                    total_or = round(float(_or_for_coh["amount"].sum()), 2)
                    log.info(
                        "%s: excluding %s of exempt loans from cash (%s) — "
                        "ORESTAR's own summary reports none for those years",
                        name,
                        f"${sum(_exempt_dropped.values()):,.2f}",
                        ", ".join(sorted(_exempt_dropped)),
                    )

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

        yearly_nets = _yearly_net(_c_for_coh, _e_for_coh, _or_for_coh, _od_for_coh, _ba_for_coh)

        # The same nets, attributed by FILING year instead of transaction year.
        #
        # ORESTAR's annual statement covers what was FILED in that period, and
        # for most years filing follows activity closely enough that the two
        # bases agree. 2006 is the exception and a large one: ORESTAR launched,
        # committees entered years of accumulated activity, and essentially all
        # of it carries a 2006 transaction date with a 2007 filing date. Every
        # one of Citizens for Mannix's 2006 loans -- $309,000 across five rows
        # -- was filed in January 2007.
        #
        # Measured over the 73 committees that diverge in 2006: attributing by
        # transaction date reconciles NONE of them and leaves $2,872,708;
        # attributing by filing date reconciles 22 exactly and leaves $200,041.
        #
        # This is NOT used as the primary basis, because the same measurement
        # says filing date is worse in most other years -- 2025 goes from $18K
        # to $147K, 2026 from $1.0M to $1.3M. It is computed alongside so a
        # divergence that DISAPPEARS under the filing basis can be recognised
        # for what it is: a period-boundary effect, not missing data.
        # Candidate rules for which rows ORESTAR's 2006 statement counts.
        #
        # Computed HERE, from the same frames and the same sign convention as
        # yearly_nets, because every attempt to reimplement this net in ad-hoc
        # SQL has produced a different number — once by $457,723 on a single
        # committee, and once making a diverging committee appear to reconcile.
        # A candidate basis is only meaningful if it differs from the live
        # figure in the basis alone.
        #
        # 2006 is the only year in question: our figures match ORESTAR from
        # 2007 on, often to within tens of dollars across a hundred committees.
        def _net_2006(row_filter):
            total = 0.0
            for _f, _sign in [(_c_for_coh, 1), (_or_for_coh, 1), (_e_for_coh, -1),
                              (_od_for_coh, -1), (_ba_for_coh, 1)]:
                if _f.empty or "year" not in _f.columns:
                    continue
                _g = _f[_f["year"] == 2006]
                if _g.empty:
                    continue
                _g = row_filter(_g)
                if _g is None or _g.empty:
                    continue
                total += _sign * float(_g["amount"].sum())
            return round(total, 2)

        def _filed_by(frame, cutoff):
            if "filed_date" not in frame.columns:
                return None
            _fd = pd.to_datetime(frame["filed_date"], errors="coerce")
            return frame[_fd.notna() & (_fd <= pd.Timestamp(cutoff))]

        basis_2006 = {
            "tran_2006": _net_2006(lambda g: g),
            "tran_2006_filed_by_2006": _net_2006(lambda g: _filed_by(g, "2006-12-31")),
            "tran_2006_filed_by_2007_01_31": _net_2006(lambda g: _filed_by(g, "2007-01-31")),
            "tran_2006_filed_by_2007_06_30": _net_2006(lambda g: _filed_by(g, "2007-06-30")),
            "tran_2006_filed_by_2007_12_31": _net_2006(lambda g: _filed_by(g, "2007-12-31")),
        }

        _filed_frames = []
        for _f, _sign in [(_c_for_coh, 1), (_or_for_coh, 1), (_e_for_coh, -1),
                          (_od_for_coh, -1), (_ba_for_coh, 1)]:
            if not _f.empty and "filed_date" in _f.columns:
                _g = _f.copy()
                _g["_fy"] = pd.to_datetime(_g["filed_date"], errors="coerce").dt.year
                _filed_frames.append((_g.dropna(subset=["_fy"]), _sign))
        yearly_nets_filed: dict[str, float] = {}
        for _g, _sign in _filed_frames:
            for _yr, _amt in _g.groupby("_fy")["amount"].sum().items():
                _k = str(int(_yr))
                yearly_nets_filed[_k] = yearly_nets_filed.get(_k, 0.0) + _sign * float(_amt)

        # Determine beginning balances and cash-on-hand
        # Strategy: use the earliest-year beginning balance scraped directly from
        # ORESTAR (via Playwright), then roll forward through yearly transaction nets.
        # This avoids error-prone back-calculation from the current year.
        orestar_info = _orestar_data.get(name)

        # Count loan principal as cash only where ORESTAR counts it.
        #
        # A loan received is not automatically the committee's money. Ted
        # Wheeler's 2006 statement records $233,000 of loans and reports
        # Loans Received $0.00 in the cash section, Total Outstanding Loans
        # $230,000 under Financial Status, and an ending cash balance of
        # $1,819.65 — against $107.90 of cash expenditures that year. Had that
        # $233,000 been cash, it would still have been sitting there. It never
        # entered the account; the candidate's spending was recorded as a loan
        # to the committee.
        #
        # We added it anyway, so his balance ran $233,000 high — and kept
        # running high forever, because ORESTAR carries its own (loan-free)
        # ending forward every year. Measured today: our $234,530.14 against
        # ORESTAR's $1,530.14, a gap equal to the 2006 loans to the cent.
        #
        # The rule is NOT "ignore loans". In most years ORESTAR does count them
        # and the money genuinely arrives: Wheeler's 2010, 2011, 2020 and 2021
        # loans all appear in ORESTAR's own figures and match our rows exactly.
        # Excluding loans wholesale was measured across all 46,761
        # committee-years and breaks 2,581 of them, taking the residual from
        # $3.55M to $24.36M.
        #
        # So ORESTAR's own reported figure decides, per committee-year. It
        # agrees with our transaction rows 738 times and disagrees 123, almost
        # all of them in 2006 (65, $2,228,906).
        #
        # Guarded on scrape freshness, because before the #90 parser fix this
        # field read $0.00 on EVERY record — substituting a false zero would
        # wipe genuine loans from 33,889 committee-years. Where the summary
        # predates the fix, our own figure stands.
        _sum_ts = float((orestar_info or {}).get("ts") or 0)
        if _sum_ts >= _LOAN_FIELD_TRUSTWORTHY_FROM:
            _our_loans_by_year = {}
            if not filer_contrib.empty and "year" in filer_contrib.columns:
                _lr = filer_contrib[filer_contrib["sub_type"] == "Loan Received (Non-Exempt)"]
                if not _lr.empty:
                    _our_loans_by_year = _lr.groupby("year")["amount"].sum().to_dict()
            _their_years = _name_to_yearly.get(name, {})
            for _yr_s in list(yearly_nets):
                _ty = _their_years.get(_yr_s)
                if not _ty:
                    continue
                _theirs = _ty.get("loans_received")
                if _theirs is None:
                    continue
                try:
                    _ours = float(_our_loans_by_year.get(int(_yr_s), 0.0))
                except (TypeError, ValueError):
                    continue
                _adj = round(float(_theirs) - _ours, 2)
                if abs(_adj) > 0.01:
                    yearly_nets[_yr_s] = round(yearly_nets[_yr_s] + _adj, 2)

        # Our net as it stood WHEN THE SUMMARY WAS CAPTURED.
        #
        # An account summary is a snapshot; transactions keep arriving. Comparing
        # today's transactions against a summary scraped days ago produces a
        # divergence that is neither side's fault, and that accounted for 95% of
        # the 2026 gap — $3,473,683 across 465 committees fell to $189,706 once
        # the summaries were re-scraped, and it began climbing again immediately
        # because committees kept filing.
        #
        # Re-scraping cannot fix this; it only resets the clock. The fix is to
        # compare like with like: restrict our side to rows that had been FILED
        # by the moment ORESTAR's figure was taken. If the two agree as of that
        # instant, the data is sound and the visible difference is only the
        # window between then and now.
        #
        # Rows are cut by filed_date rather than tran_date because a summary can
        # only reflect what had been filed when it was read.
        _asof_ts = float((orestar_info or {}).get("ts") or 0)
        yearly_nets_asof: dict[str, float] = {}
        if _asof_ts:
            _asof_cut = pd.Timestamp(datetime.fromtimestamp(_asof_ts))
            for _f, _sign in [(_c_for_coh, 1), (_or_for_coh, 1), (_e_for_coh, -1),
                              (_od_for_coh, -1), (_ba_for_coh, 1)]:
                if _f.empty or "year" not in _f.columns or "filed_date" not in _f.columns:
                    continue
                _g = _f.copy()
                _g["_fd"] = pd.to_datetime(_g["filed_date"], errors="coerce")
                _g = _g[_g["_fd"].notna() & (_g["_fd"] <= _asof_cut)]
                if _g.empty:
                    continue
                for _yr, _amt in _g.groupby("year")["amount"].sum().items():
                    _k = str(int(_yr))
                    yearly_nets_asof[_k] = yearly_nets_asof.get(_k, 0.0) + _sign * float(_amt)
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
        # Loan receipts per year, so a committee-year can be checked against
        # ORESTAR's own reported loans line.
        _loans_recv = (filer_contrib[filer_contrib["sub_type"] == "Loan Received (Non-Exempt)"]
                       if not filer_contrib.empty else filer_contrib)
        _yearly_loans = _yearly_sums(_loans_recv)

        # Per-year loan scaling, shared by the balance, the timeline and this
        # comparison so all three treat loan principal identically.
        _coh_loan_scale: dict[int, float] = {}
        if _sum_ts >= _LOAN_FIELD_TRUSTWORTHY_FROM:
            _their_yrs_cmp = _name_to_yearly.get(name, {})
            for _y, _amt in (_yearly_loans or {}).items():
                _ty = _their_yrs_cmp.get(str(int(_y)))
                if not _ty or _ty.get("loans_received") is None:
                    continue
                _amt = float(_amt)
                if abs(_amt) <= 0.01:
                    continue
                _coh_loan_scale[int(_y)] = float(_ty["loans_received"]) / _amt

        def _loan_scale_for_year(_y):
            try:
                return _coh_loan_scale.get(int(_y), 1.0)
            except (TypeError, ValueError):
                return 1.0

        _yearly_or = _yearly_sums(_or_for_coh)
        _yearly_od = _yearly_sums(filer_od)

        if orestar_yearly:
            for yr_s in sorted_years:
                yr_orestar = orestar_yearly.get(yr_s, {})
                if not yr_orestar:
                    continue

                yr_int = int(yr_s) if yr_s.isdigit() else None
                our_begin = beginning_balances.get(yr_s, 0.0)
                # Contributions on the same basis as the balance and the stat
                # card: loan principal counted the way ORESTAR counts it.
                #
                # _orestar_contrib includes loans received, so this line was
                # comparing our raw loan figure against ORESTAR's reported one
                # while every other line — and the balance itself — already used
                # theirs. Wheeler's 2006 read ours $235,500 against ORESTAR
                # $2,500, the difference being exactly his ten loan rows, on a
                # year where the ending balance matched to the cent.
                #
                # It also left the page disagreeing with itself: since #99 the
                # timeline and the Contributions card carry the adjusted figure,
                # so the row below the card contradicted it.
                our_c  = round(float(_yearly_orestar_c.get(yr_int, 0))
                               - float(_yearly_loans.get(yr_int, 0))
                               + float(_yearly_loans.get(yr_int, 0)) * _loan_scale_for_year(yr_int), 2)
                our_e  = round(float(_yearly_cash_exp.get(yr_int, 0)) + float(_yearly_inkind.get(yr_int, 0)), 2)
                our_or = round(float(_yearly_or.get(yr_int, 0)), 2)
                our_od = round(float(_yearly_od.get(yr_int, 0)), 2)
                # Transaction basis, for every year including 2006.
                #
                # 2006 was briefly switched to the FILING basis, because the
                # committees that diverge there reconcile under it: Citizens for
                # Mannix's whole 2006 loan book — $309,000 across five rows —
                # carries 2006 transaction dates and 2007-01 filing dates, and
                # ORESTAR's 2006 statement reports only the $159,000 filed on
                # the 29th.
                #
                # That switch was reverted. It was measured only on the 74
                # committee-years ALREADY diverging, where it looked like a drop
                # from $3.18M to $205K. Applied to everyone it took 2006 from 73
                # affected committees to 513 — 426 of them newly wrong by under
                # $5,000 each — because ORESTAR's 2006 statement is not uniformly
                # filing-based. Committees that filed contemporaneously agree on
                # both bases; late filers follow the filing; and committees with
                # 2005 activity filed in 2006 are broken BY the filing basis.
                #
                # Trading $690K of concentrated, explainable error for small
                # errors across 440 additional committees is the worse deal, and
                # it is still not what ORESTAR does. The rule ORESTAR actually
                # applied in its first year is not yet known; see
                # audit_2006_basis.py, which measures it per committee instead
                # of assuming it.
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

                # How much ORESTAR says the balance MOVED this year, against
                # how much our transactions say it moved.
                #
                # delta_end below cannot answer "is this year's data right?",
                # because our_end is built on OUR rolled-forward beginning
                # balance: one bad year in 2008 makes every year after it look
                # wrong, and the real culprit is indistinguishable from its
                # eighteen innocent successors.
                #
                # ORESTAR states a beginning AND an ending balance for every
                # year, so the difference between them is that year's movement
                # as the audited record has it — with no dependence on anything
                # earlier. Comparing our own net against it isolates the single
                # year, which is what makes it possible to say WHERE our
                # transaction history diverges rather than merely that it does.
                #
                # Both sides include balance adjustments: yearly_nets is built
                # with filer_ba, and ORESTAR's ending reflects adjustments too,
                # so the two definitions match.
                orestar_movement = (
                    round((orestar_end or 0) - (orestar_beg or 0), 2)
                    if orestar_end is not None and orestar_beg is not None else None
                )
                delta_movement = (
                    round(round(our_net, 2) - orestar_movement, 2)
                    if orestar_movement is not None else None
                )

                # Does this divergence disappear if the year is read the way
                # ORESTAR's statement is assembled — by what was FILED in it?
                #
                # If so it is a period-boundary effect, not missing data: the
                # transactions exist on both sides, they simply fall on
                # different sides of a year line. Marking that is what stops
                # 2006 being re-investigated as a data gap, which has now
                # happened three times, each with a different wrong
                # explanation attached (pre-ORESTAR boundary, loan treatment,
                # a parse bug).
                our_net_filed = round(yearly_nets_filed.get(yr_s, 0.0), 2)
                delta_movement_filed = (
                    round(our_net_filed - orestar_movement, 2)
                    if orestar_movement is not None else None
                )
                # Same year, but our side frozen to the summary's capture time.
                our_net_asof = (round(yearly_nets_asof.get(yr_s, 0.0), 2)
                                if _asof_ts else None)
                delta_movement_asof = (
                    round(our_net_asof - orestar_movement, 2)
                    if our_net_asof is not None and orestar_movement is not None else None
                )
                # Reconciles as of the capture, diverges now => the gap is the
                # reporting window, not the data. Regenerates every day by
                # design, so it is not something to chase.
                # Same reasoning. The as-of figures are kept because they are
                # genuinely informative — they say how much of a difference the
                # reporting window accounts for — but they do not excuse it.
                # Measured, the window explains little of what is left in 2026:
                # $241,675 live against $214,909 as-of.
                snapshot_lag = False

                # Deliberately NOT flagging "this would reconcile on another
                # basis" as an explanation.
                #
                # That is a suppression mechanism wearing a classifier's clothes:
                # it leaves the calculation disagreeing with ORESTAR and hides
                # the difference behind a tolerance. Where another basis is
                # demonstrably the right one — 2006 — the fix is to compute on
                # it, which is what happens above. Where it is not, the
                # difference is real and belongs in the total.
                # ORESTAR contradicting its own summary.
                #
                # Our cash-balance formula IS ORESTAR's, read off the page:
                #   Beginning + Total Contributions + Other Receipts
                #   + Loans Received (exempt) - Total Expenditures
                #   - Other Disbursements - Loan Payments (exempt)
                #   + Balance Adjustments = Ending Cash Balance
                # with Total Contributions including non-exempt loans and
                # in-kind, and in-kind cancelling against the expenditure side.
                # That is why 2007-2025 reconcile to within tens of dollars
                # across thousands of committee-years.
                #
                # Where it does not reconcile, the inconsistency is ORESTAR's.
                # Ted Wheeler's 2006 summary reports Loans Received $0.00 while
                # the SAME page reports Total Outstanding Loans $230,000.00,
                # against $233,000 of loan transactions ORESTAR itself lists.
                # Its own numbers disagree with each other, so no calculation
                # can match all of them at once — mirroring the loans line
                # reconciles 66 committee-years and breaks 44 whose movement
                # follows OUR figure instead.
                #
                # Recorded rather than resolved, and surfaced in the tooltip, so
                # a reader sees that the difference is in the source record.
                our_loans_received = round(float(_yearly_loans.get(yr_int, 0)), 2)
                orestar_loans_received = (float(yr_orestar.get("loans_received"))
                                          if yr_orestar.get("loans_received") is not None else None)
                loan_gap = (round(our_loans_received - orestar_loans_received, 2)
                            if orestar_loans_received is not None else None)
                orestar_omits_loans = bool(
                    loan_gap is not None and abs(loan_gap) > 1.0
                    and delta_movement is not None
                    and abs(abs(loan_gap) - abs(delta_movement)) <= max(1.0, abs(loan_gap) * 0.05)
                )

                attribution_artifact = False

                # EVERY year ORESTAR gives us a figure for, not only the ones
                # that disagree.
                #
                # Storing only mismatches made the table impossible to read as
                # evidence: Friends of Ted Wheeler showed a single 2006 row out
                # of 21 comparable years, which looks like one broken year
                # rather than twenty reconciled ones and one outstanding. The
                # years that agree are the reassurance — they are what says the
                # calculation is sound where it is not being questioned.
                deltas = [d for d in [delta_c, delta_e, delta_or, delta_od, delta_end,
                                      delta_beg, delta_movement] if d is not None]
                reconciles = not any(abs(d) > 0.01 for d in deltas)
                # Completeness is PERISHABLE, and this treated it as permanent.
                #
                # The survey asks ORESTAR how many records it holds on a given
                # day. An active committee files constantly, so "complete on
                # 13 August" says nothing about a summary captured on 26
                # August. Friends of Christine Drazan was certified complete at
                # 4,062 rows, and by the time its summary was re-scraped
                # ORESTAR held 4,294 for 2026 against our 4,249 — 45 rows
                # short. The note nevertheless told readers that ORESTAR's
                # summary disagreed with its own transactions, over $243,465,
                # when the truth was simply that we were behind.
                #
                # 213 of the 220 committees carrying that note, worth $491,534,
                # had a certificate older than the summary being compared. So
                # the certificate only counts when it is at least as recent as
                # the summary. This is the same failure #113 fixed for stale
                # SNAPSHOTS, in the other direction: there we compared against
                # an old summary, here we vouch with an old survey.
                #
                # It costs real coverage — Ferrioli's 2006 gap is verified and
                # loses its note because nobody re-surveyed it — and that is
                # the right trade. A silent row is recoverable; a confident
                # wrong attribution is not. Re-running the survey restores
                # every note it withdraws, with evidence behind it.
                _rc = _ROW_COMPLETE.get(_name_to_fid.get(name, ""))
                _rows_complete_here = None
                if _rc is not None:
                    _complete, _checked = _rc
                    if _checked is not None and _sum_ts:
                        _sday = datetime.fromtimestamp(float(_sum_ts)).date()
                        _rows_complete_here = _complete if _checked >= _sday else None
                # A stale snapshot is not ORESTAR contradicting itself.
                #
                # summary_vs_itemised compares our line sums against the STORED
                # summary. When that summary was captured before the committee
                # filed, the difference is the capture time — exactly what #108
                # labels as snapshot_stale — and saying "ORESTAR's summary
                # disagrees with its own transactions" would assert something
                # demonstrably untrue. Bring Balance to Salem PAC is the proof:
                # its stored summary is $180,040 adrift, and ORESTAR's LIVE page
                # reports our figure to the cent.
                #
                # Measured over the committees the note would otherwise appear
                # on: 56 of 257, worth $536,395 — 69% of the labelled dollars —
                # are stale rather than self-contradictory.
                if _rows_complete_here and _sum_ts:
                    _snap_day = datetime.fromtimestamp(_sum_ts).date()
                    _lf = None
                    if not filer_all.empty and "filed_date" in filer_all.columns:
                        _lf_raw = pd.to_datetime(filer_all["filed_date"],
                                                 errors="coerce").max()
                        _lf = None if pd.isna(_lf_raw) else _lf_raw.date()
                    if _lf is not None and _lf >= _snap_day:
                        _rows_complete_here = None   # unknowable until re-scraped
                if True:
                    yearly_discrepancies[yr_s] = {
                        # True when every line item agrees, so the UI can show
                        # a reconciled year as confirmation rather than styling
                        # it as a small problem.
                        "reconciles": reconciles,
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
                        # The drift-free pair: what ORESTAR says this year moved,
                        # and how far our transactions are from that. Unlike
                        # `discrepancy`, these do not inherit earlier years' error.
                        "orestar_movement": orestar_movement,
                        "delta_movement": delta_movement,
                        # Same year read on ORESTAR's own basis. When this
                        # reconciles and delta_movement does not, the year is a
                        # filing-period boundary artifact rather than a gap.
                        "our_net_filed": our_net_filed,
                        "delta_movement_filed": delta_movement_filed,
                        "attribution_artifact": attribution_artifact,
                        # Our side as of the summary's capture. When this
                        # reconciles and the live comparison does not, the
                        # difference is the reporting window.
                        "our_loans_received": our_loans_received,
                        "orestar_loans_received": orestar_loans_received,
                        "orestar_omits_loans": orestar_omits_loans,
                        "our_net_asof": our_net_asof,
                        "delta_movement_asof": delta_movement_asof,
                        "snapshot_lag": snapshot_lag,
                        # ORESTAR's summary against ORESTAR's OWN itemised
                        # transactions, where we know our row set equals its.
                        #
                        # Friends of Ted Ferrioli 2006 is the case this exists
                        # for. ORESTAR's summary reports $213,961.90 of
                        # expenditures and $204,049.26 of contributions; the
                        # 161 transactions ORESTAR itemises for that year total
                        # $173,367.64 and $203,155.00. Verified against ORESTAR
                        # directly, not inferred: a date-bounded record count
                        # (161 = 161, where the survey's all-years figure could
                        # not have settled it), a narrow search finding exactly
                        # ONE $40,000 row, a fresh read of the summary, and a
                        # full sum paged off ORESTAR's own results screens.
                        # Net -40,000 + 300 = -39,700, which then propagates as
                        # an opening-balance gap for twelve further years.
                        #
                        # We follow the itemised record, which our figures
                        # match to the cent. Matching the summary instead would
                        # mean synthesising a second $40,000 row — disagreeing
                        # with the record we currently match exactly, and
                        # corrupting every donor and payee total on the site to
                        # repair one balance.
                        #
                        # Gated on known completeness. Where the survey has not
                        # measured a committee this stays None and the site
                        # says nothing, because a line difference looks the
                        # same whether ORESTAR over-reports or we under-hold.
                        "rows_complete": _rows_complete_here,
                        "summary_vs_itemised": (
                            None if not _rows_complete_here or orestar_c is None
                                    or orestar_e is None else
                            round((orestar_c - our_c) + ((orestar_or or 0) - our_or)
                                  - (orestar_e - our_e)
                                  - ((orestar_od or 0) - our_od), 2)),
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
        # _or_for_coh, not filer_or. The client recomputes cash on hand from this
        # timeline, so a frame that still carries the excluded exempt principal
        # re-adds on render exactly what the server just took out — #99, which
        # showed Ted Wheeler $726,880 against a stored $1,530.14.
        or_monthly = monthly_sum(_or_for_coh,         "other_receipts")
        od_monthly = monthly_sum(_od_for_coh,         "other_disbursements")
        # Balance adjustments were missing from the timeline entirely, so any
        # balance recomputed from it could not match ours no matter what else
        # was corrected — the information simply was not there.
        ba_monthly = monthly_sum(_ba_for_coh,         "balance_adjustments")
        count_monthly_filer = filer_all.groupby("month").size().rename("count")
        tl_df = pd.concat([c_monthly, i_monthly, li_monthly, ce_monthly, ik_monthly,
                           lo_monthly, or_monthly, od_monthly, ba_monthly,
                           count_monthly_filer], axis=1).fillna(0).sort_index()
        # The timeline must carry the SAME loan figures the balance was built
        # from, because the browser recomputes cash on hand from these rows:
        #
        #     netFlow = totalIn + totalLoansIn + totalOR
        #             - totalOut - totalLoansOut - totalOD
        #
        # so a correction applied only to yearly_nets is silently undone on
        # render. Ted Wheeler's stored balance was $1,530.14, matching ORESTAR
        # to the cent, while the page displayed $726,880 with a red discrepancy
        # warning — the client re-adding the loan principal the server had just
        # removed.
        #
        # Scaled proportionally within the year, so a month keeps its share of
        # whatever ORESTAR reports for that year. Where ORESTAR reports nothing
        # (2006 for 65 committees) every month goes to zero, which is the point.
        _loan_scale: dict[int, float] = {}
        if _sum_ts >= _LOAN_FIELD_TRUSTWORTHY_FROM:
            _their_years_tl = _name_to_yearly.get(name, {})
            _ours_tl = {}
            if not filer_contrib.empty and "year" in filer_contrib.columns:
                _lr_tl = filer_contrib[filer_contrib["sub_type"] == "Loan Received (Non-Exempt)"]
                if not _lr_tl.empty:
                    _ours_tl = _lr_tl.groupby("year")["amount"].sum().to_dict()
            for _y, _ours_amt in _ours_tl.items():
                _ty = _their_years_tl.get(str(int(_y)))
                if not _ty or _ty.get("loans_received") is None:
                    continue
                _ours_amt = float(_ours_amt)
                if abs(_ours_amt) <= 0.01:
                    continue
                _loan_scale[int(_y)] = float(_ty["loans_received"]) / _ours_amt

        def _scaled_loans(month_key, raw):
            try:
                _y = int(str(month_key)[:4])
            except (TypeError, ValueError):
                return raw
            return raw * _loan_scale.get(_y, 1.0)

        timeline = [
            {
                "month": m,
                # contributions ALREADY includes loans (and in-kind), and
                # expenditures already includes loan payments — that is what
                # _orestar_contrib and _EXPEND_TYPES contain. The loan portion
                # here is swapped for ORESTAR's reported figure so this field
                # carries the same loans the balance is built from.
                "contributions":       round(
                    float(row.get("contributions", 0))
                    - float(row.get("loans_received", 0))
                    + _scaled_loans(m, float(row.get("loans_received", 0))), 2),
                "inkind":              round(float(row.get("inkind",              0)), 2),
                "loans_received":      round(_scaled_loans(m, float(row.get("loans_received", 0))), 2),
                "expenditures":        round(float(row.get("cash_exp", 0)) + float(row.get("inkind_exp", 0)), 2),
                "loan_payments":       round(float(row.get("loan_payments",       0)), 2),
                "other_receipts":      round(float(row.get("other_receipts",      0)), 2),
                "other_disbursements": round(float(row.get("other_disbursements", 0)), 2),
                "balance_adjustments": round(float(row.get("balance_adjustments", 0)), 2),
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
            # ORESTAR's own year-to-year balances, where they do not chain.
            #
            # Year N's ending balance should be year N+1's beginning balance.
            # For 444 of 6,511 committees with more than one summary year it is
            # not, and the jumps have no transactions behind them: the silent
            # years were checked against ORESTAR directly and it reports ZERO
            # records for them, matching what we hold.
            #
            # Hood River County Democrats is the clearest case. Its 2009
            # summary ends at $336.93 and its 2010 summary begins at $475.85 —
            # $138.92 out of nowhere — and seven more such jumps follow. They
            # sum to $6,192.22, which is its entire discrepancy to the cent.
            # Across the bucket this holds for 171 of 178 committees.
            #
            # This is the same defect the summary_vs_itemised note describes,
            # only across years instead of within one, and it needs no
            # completeness evidence: it is ORESTAR's summary against ORESTAR's
            # summary. Nothing here can be fixed on our side — we roll the
            # balance forward from transactions and that arithmetic is sound.
            # Recording it is what lets the site say so.
            "orestar_chain_breaks": _chain_breaks(_name_to_yearly.get(name, {})),
            # Rows ORESTAR has withdrawn, kept in the dataset but out of the
            # balance. Surfaced so a reader who finds one in the transaction
            # list is told why it is not in the total, rather than concluding
            # the arithmetic is broken.
            "orestar_withdrawn": {
                "count": len(_withdrawn_here),
                "amount": round(float(
                    filer_all.loc[filer_all["tran_id"].astype(str).isin(_withdrawn_here),
                                  "amount"].sum()) if (_withdrawn_here and not filer_all.empty
                                                       and "tran_id" in filer_all.columns) else 0.0, 2),
            } if _withdrawn_here else {},
            # Exempt loan principal held out of cash because ORESTAR's own
            # statement records no receipts at all in those years. Carried per
            # year so the site can say WHY a figure omits a transaction the
            # committee plainly filed, rather than silently differing from a
            # number the reader can look up.
            "exempt_loans_excluded": _exempt_dropped,
            "yearly_discrepancies": yearly_discrepancies,
            # Candidate 2006 bases, on the pipeline's own definition, so the
            # rule ORESTAR actually applied can be identified by measurement
            # rather than guessed at a fifth time.
            "basis_2006": basis_2006,
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
    # Cash filed AFTER each committee's summary was captured, per (filer,
    # cutoff date). Built once from the merged frame rather than queried per
    # committee: this runs over 7,263 filers, and the alternative is 7,263
    # round trips to answer one question.
    #
    # Same definition as the balance itself — cash contributions and other
    # receipts in, cash expenditures and disbursements out, in-kind excluded
    # because it never moves cash. Loans are already inside the contribution
    # and expenditure sets, exactly as ORESTAR has them.
    _filer_post_summary: dict[str, dict] = {}
    _filer_last_filed: dict[str, object] = {}
    try:
        _cuts = {}
        for _r in _filer_detail_rows:
            _t = ((_r["detail"].get("orestar_account_summary") or {}).get("scrape_ts")) or 0
            if _t and _r.get("filer_id"):
                _cuts[str(_r["filer_id"])] = datetime.fromtimestamp(float(_t)).date()
        # The processing frame names this column "filer id", with a space — it
        # is the raw ORESTAR header. Only filer_detail and the database use
        # filer_id. Guarding on the wrong name meant this block was skipped in
        # full and silently: every committee reported $0.00 of post-summary
        # activity, the as-of delta equalled the live one everywhere, and no
        # warning was logged because nothing raised. A guard written to fail
        # safely instead made the feature do nothing at all.
        _fid_col = ("filer id" if "filer id" in df.columns
                    else ("filer_id" if "filer_id" in df.columns else None))
        if _cuts and "filed_date" in df.columns and _fid_col:
            _pf = df[df[_fid_col].astype(str).str.strip().isin(_cuts.keys())].copy()
            _pf["_fd"] = pd.to_datetime(_pf["filed_date"], errors="coerce").dt.date
            _pf = _pf[_pf["_fd"].notna()]
            _pf["_cut"] = _pf[_fid_col].astype(str).str.strip().map(_cuts)
            # When did each committee last file anything at all?
            #
            # Taken here, before the cutoff filter, because the question is
            # about the SNAPSHOT rather than about activity after it: if a
            # committee filed on or after the day its summary was captured,
            # that summary provably cannot contain everything we hold, and the
            # comparison against it is stale by arithmetic rather than by
            # supposition.
            #
            # The post-summary figure above cannot answer this, because it cuts
            # strictly after the capture DATE and filed_date carries no time.
            # Bring Balance to Salem PAC filed $180,040 on 2026-08-24 and the
            # summary was captured 2026-08-24 12:37:45, so its post-summary
            # activity came out as $0.00 and the gap was recorded as genuine.
            # ORESTAR's live page now reports exactly our figure, to the cent.
            for _fid_v, _fd_v in (_pf.groupby(_pf[_fid_col].astype(str).str.strip())["_fd"]
                                     .max().items()):
                _filer_last_filed[str(_fid_v)] = _fd_v
            _pf = _pf[_pf["_fd"] > _pf["_cut"]]
            if not _pf.empty:
                _ink = _pf["sub_type"].isin(INKIND_SUBTYPES) if "sub_type" in _pf.columns else False
                _sign = pd.Series(0.0, index=_pf.index)
                _sign[(_pf["tran_type"] == "C") & (~_ink)] = 1.0
                _sign[_pf["tran_type"] == "OR"] = 1.0
                _sign[(_pf["tran_type"] == "E") & (~_ink)] = -1.0
                _sign[_pf["tran_type"] == "OD"] = -1.0
                _pf["_signed"] = _pf["amount"].astype(float) * _sign
                for (_fid, _cut), _amt in _pf.groupby(
                        [_pf[_fid_col].astype(str).str.strip(), "_cut"])["_signed"].sum().items():
                    _filer_post_summary.setdefault(_fid, {})[_cut] = float(_amt)
        if not _filer_post_summary:
            log.warning("post-summary activity is empty for all %d committees — "
                        "the admin comparison is falling back to live deltas "
                        "(filer id column: %s)", len(_cuts), _fid_col)
        else:
            log.info("post-summary activity computed for %d committees",
                     len(_filer_post_summary))
    except Exception as _e:
        log.warning("post-summary activity not computed (%s) — "
                    "the admin comparison falls back to the live delta", _e)

    _disc_rows = []
    for _row in _filer_detail_rows:
        _d = _row["detail"]
        _acct = _d.get("orestar_account_summary") or {}
        if _acct.get("ending_cash_balance") is None:
            continue                      # never checked — not a discrepancy
        _delta = round(_d.get("cash_on_hand", 0.0) - _acct["ending_cash_balance"], 2)

        # The same balance as of the moment ORESTAR's figure was captured.
        #
        # cash_on_hand covers every transaction we hold; ending_cash_balance is
        # a snapshot. Anything filed in between is a difference belonging to
        # neither side, and it reappears continuously: hours after a summary
        # sweep finished, Cyrus for Oregon was flagged for $45,771 that was
        # exactly its 16 rows filed since, and Hicks for Senate for $108,312 of
        # which $91,765 was the same thing.
        #
        # Both figures are kept rather than one replacing the other. Showing
        # only the as-of delta would hide the window; showing only the live one
        # buries real gaps in it.
        #
        # Bring Balance to Salem PAC was cited here as the example of a gap
        # with no post-summary activity that was "entirely genuine". It was
        # not. Its $180,040 is two rows filed 2026-08-24 against a summary
        # captured 2026-08-24 12:37:45; ORESTAR's live page now reports our
        # figure to the cent. Three more of that group were checked against
        # ORESTAR directly and every one matched exactly. Hence snapshot_stale
        # below — post_summary_activity cannot see a same-day filing, so it
        # said $0.00 and the comparison looked sound.
        _asof_delta = None
        _post_summary = None
        _cut = None
        _last_filed = None
        _ts = _acct.get("scrape_ts") or 0
        # scrape_ts is 0 on summaries captured before #87 recorded it. Treating
        # that as "captured in 1970" would exclude a committee's whole history
        # and report its entire balance as post-summary activity, so those keep
        # the live comparison only.
        if _ts:
            _cut = datetime.fromtimestamp(float(_ts)).date()
            _last_filed = _filer_last_filed.get(str(_row.get("filer_id")))
            _post = _filer_post_summary.get(str(_row.get("filer_id")), {}).get(_cut)
            if _post is None:
                _post = 0.0
            _post_summary = round(float(_post), 2)
            _asof_delta = round(_delta - _post_summary, 2)

        # Flag on the AS-OF difference where we have one: that is the real
        # disagreement. A committee whose entire delta is post-summary activity
        # is not in disagreement with ORESTAR, it is simply ahead of it.
        _judge = _asof_delta if _asof_delta is not None else _delta
        if abs(_judge) <= 0.01:
            continue
        _disc_rows.append({
            "slug": _row["slug"], "name": _row["name"], "filer_id": _row.get("filer_id"),
            "calculated": _d.get("cash_on_hand", 0.0),
            "orestar": _acct["ending_cash_balance"],
            "delta": _delta,
            # What the comparison is actually judged on: our balance restricted
            # to rows filed by the summary's capture, against that summary.
            "asof_delta": _asof_delta,
            "post_summary_activity": _post_summary,
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
            # The snapshot provably predates something we hold.
            #
            # Not a tolerance and not a guess: if the committee filed on or
            # after the day its summary was captured, that summary cannot
            # contain every row on our side, so the difference is at least
            # partly the capture time rather than a disagreement. Four such
            # committees were checked against ORESTAR's live page during this
            # investigation and all four matched our figure exactly.
            #
            # These rows are LABELLED, not dropped — they stay in the list with
            # their dollars visible. The resolution is a fresher summary, and a
            # reader who cannot see which comparisons are stale cannot tell
            # which ones to re-scrape.
            "snapshot_stale": bool(
                _ts and _last_filed is not None and _last_filed >= _cut),
            "last_filed": (_last_filed.isoformat()
                           if _ts and _last_filed is not None else None),
        })
    _disc_rows.sort(key=lambda r: -abs(r["asof_delta"] if r["asof_delta"] is not None else r["delta"]))
    _write_json("balance_discrepancies.json", {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "checked": sum(1 for r in _filer_detail_rows
                       if (r["detail"].get("orestar_account_summary") or {})
                          .get("ending_cash_balance") is not None),
        "flagged": len(_disc_rows),
        # How many of those comparisons are against a summary that provably
        # predates the committee's own filings, and what they are worth. Kept
        # in the payload so the Admin tab can say what the flagged total is
        # actually made of instead of presenting one undifferentiated number.
        "snapshot_stale": sum(1 for r in _disc_rows if r.get("snapshot_stale")),
        "snapshot_stale_amount": round(sum(
            abs(r["asof_delta"] if r["asof_delta"] is not None else r["delta"])
            for r in _disc_rows if r.get("snapshot_stale")), 2),
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

    # Carry forward what this generator does not build.
    #
    # legislative_map has TWO producers. generate_activity_snapshot builds it
    # from the filing roster; refresh_legislative_map rebuilds the same key and
    # adds district_history, the per-district prior-cycle results the race map
    # renders under "Previous cycles".
    #
    # Rewriting the whole key here silently deleted that history on every daily
    # refresh. racemap.js reads it, refresh_legislative_map writes it, and
    # nothing errored — the panel simply showed nothing for all but the few
    # hours between a candidate-filings run and the next refresh. The feature
    # has never worked in the normal course of a day.
    #
    # Preserving a key the current generator cannot produce is the fix: the
    # daily refresh has no candidate-filing or election-results data in hand,
    # so it is in no position to replace it.
    try:
        _prev_snap = supabase_sync.get_dashboard_cache("activity_snapshot") or {}
        _prev_hist = ((_prev_snap.get("legislative_map") or {}).get("district_history"))
        if _prev_hist and "district_history" not in (_snapshot.get("legislative_map") or {}):
            _snapshot.setdefault("legislative_map", {})["district_history"] = _prev_hist
            log.info("Preserved district_history for %d chamber(s) from the previous snapshot",
                     len(_prev_hist))
    except Exception as e:
        log.warning("Could not preserve district_history: %s", e)

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
