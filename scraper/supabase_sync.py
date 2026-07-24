"""
supabase_sync.py — push processed data into Supabase Postgres.

The dashboard reads its data from Postgres (not static files), so after
process.py computes everything it syncs three things here:

  • transactions        — the full queryable table (source of truth for the
                          Explore / SQL / download surface)
  • dashboard_cache     — the curated aggregate blobs (summary, timeline, …)
  • filer_detail        — one jsonb row per filer slug

All functions no-op when SUPABASE_DB_URL is not set, so local runs without
credentials still produce the CSV/JSON files as before.

Env:
  SUPABASE_DB_URL            postgres connection string (service/postgres role)
  SUPABASE_URL               https://<project>.supabase.co   (for Storage upload)
  SUPABASE_SERVICE_ROLE_KEY  service role key                (for Storage upload)
  SUPABASE_STORAGE_BUCKET    bucket name for the full-CSV download (default: 'exports')
"""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# ── df/CSV column name → transactions table column name ─────────────────────
COLUMN_MAP = {
    "tran_id": "tran_id",
    "original id": "original_id",
    "tran_date": "tran_date",
    "tran status": "tran_status",
    "filer": "filer",
    "contributor_payee": "contributor_payee",
    "sub_type": "sub_type",
    "payer of personal expenditure": "payer_of_personal_expenditure",
    "amount": "amount",
    "aggregate amount": "aggregate_amount",
    "contributor/payee committee id": "contributor_payee_committee_id",
    "filer id": "filer_id",
    "attest by name": "attest_by_name",
    "attest date": "attest_date",
    "review by name": "review_by_name",
    "review date": "review_date",
    "due date": "due_date",
    "occptn ltr date": "occptn_ltr_date",
    "pymt sched txt": "pymt_sched_txt",
    "purpose": "purpose",
    "intrst rate": "intrst_rate",
    "check nbr": "check_nbr",
    "tran stsfd ind": "tran_stsfd_ind",
    "filed by name": "filed_by_name",
    "filed_date": "filed_date",
    "addr book agent name": "addr_book_agent_name",
    "book type": "book_type",
    "book_type": "book_type",
    "title txt": "title_txt",
    "occupation": "occupation",
    "employer": "employer",
    "emp city": "emp_city",
    "emp state": "emp_state",
    "employ ind": "employ_ind",
    "self employ ind": "self_employ_ind",
    "addr line1": "addr_line1",
    "addr line2": "addr_line2",
    "city": "city",
    "state": "state",
    "zip": "zip",
    "zip plus four": "zip_plus_four",
    "county": "county",
    "country": "country",
    "foreign postal code": "foreign_postal_code",
    "purpose_codes": "purpose_codes",
    "exp date": "exp_date",
    "_source_file": "source_file",
    "tran_type": "tran_type",
    "contributor_type": "contributor_type",
    "office": "office",
    "party": "party",
    "contributor_payee_canonical": "contributor_payee_canonical",
    "filer_canonical": "filer_canonical",
    "contributor_type_label": "contributor_type_label",
}

DATE_COLS = {"tran_date", "attest_date", "review_date", "due_date",
             "occptn_ltr_date", "filed_date", "exp_date"}
NUMERIC_COLS = {"amount", "aggregate_amount"}
BIGINT_COLS = {"tran_id", "original_id"}

# Ordered list of target columns (table order not required, but stable helps).
TARGET_COLS = list(dict.fromkeys(COLUMN_MAP.values()))


# ── Connection ──────────────────────────────────────────────────────────────
def _load_dotenv() -> None:
    """Populate os.environ from a repo-root .env (local dev convenience).
    Never overrides values already set (e.g. GitHub Actions secrets)."""
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def sync_enabled() -> bool:
    _load_dotenv()
    return bool(os.environ.get("SUPABASE_DB_URL"))


def _parse_dsn(dsn: str) -> dict:
    """Parse postgres://user:password@host:port/dbname into psycopg2 kwargs.

    Done manually (not urllib) so passwords containing URL-special characters
    like '?', '#', '/', '@' work without the caller having to percent-encode."""
    body = dsn.split("://", 1)[1] if "://" in dsn else dsn
    userpass, _, hostpart = body.rpartition("@")          # host is after the last @
    user, _, password = userpass.partition(":")           # password is after the first :
    hostport, _, dbname = hostpart.partition("/")
    dbname = dbname.split("?", 1)[0] or "postgres"         # drop any ?query suffix
    host, _, port = hostport.rpartition(":")
    return {
        "host": host,
        "port": port or "5432",
        "user": user,
        "password": password,
        "dbname": dbname,
    }


def _connect():
    import psycopg2  # imported lazily so local runs without the dep still work
    _load_dotenv()
    params = _parse_dsn(os.environ["SUPABASE_DB_URL"])
    # TLS + TCP keepalives keep long bulk-load connections from being dropped.
    params.update(sslmode="require", keepalives=1, keepalives_idle=30,
                  keepalives_interval=10, keepalives_count=5)
    return psycopg2.connect(**params)


# ── DataFrame → COPY-ready CSV ───────────────────────────────────────────────
def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Rename to table columns and coerce dates/numerics so COPY into the typed
    transactions table succeeds. Empty strings become NULL via COPY(NULL '')."""
    present = {src: dst for src, dst in COLUMN_MAP.items() if src in df.columns}
    out = df[list(present.keys())].rename(columns=present).copy()

    # Collapse any duplicate target columns (e.g. both "book type" and
    # "book_type" present) keeping the first non-empty.
    out = out.loc[:, ~out.columns.duplicated()]

    for col in out.columns:
        if col in DATE_COLS:
            s = pd.to_datetime(out[col], errors="coerce")
            out[col] = s.dt.strftime("%Y-%m-%d").where(s.notna(), "")
        elif col in NUMERIC_COLS:
            s = pd.to_numeric(out[col], errors="coerce")
            out[col] = s.map(lambda x: "" if pd.isna(x) else repr(float(x)))
        elif col in BIGINT_COLS:
            s = pd.to_numeric(out[col], errors="coerce")
            out[col] = s.map(lambda x: "" if pd.isna(x) else str(int(x)))
        else:
            out[col] = out[col].fillna("").astype(str)

    return out


# COPY payload size per round-trip. Large default for clean networks (CI);
# override to a small value (e.g. 500) on flaky/inspected TLS links where big
# payloads get dropped: export SUPABASE_COPY_CHUNK=500
_load_dotenv()
COPY_CHUNK_ROWS = int(os.environ.get("SUPABASE_COPY_CHUNK", "25000"))


def _copy_frame(cur, table: str, frame: pd.DataFrame) -> None:
    """COPY a frame in row-chunks so no single COPY payload is huge."""
    cols = list(frame.columns)
    col_list = ", ".join(f'"{c}"' for c in cols)
    copy_sql = f"COPY {table} ({col_list}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"
    for start in range(0, len(frame), COPY_CHUNK_ROWS):
        buf = io.StringIO()
        frame.iloc[start:start + COPY_CHUNK_ROWS].to_csv(buf, index=False, header=True)
        buf.seek(0)
        cur.copy_expert(copy_sql, buf)


# ── Transactions ─────────────────────────────────────────────────────────────
def upsert_transactions(df: pd.DataFrame) -> None:
    """Insert-or-update the given rows (used for the daily changed window)."""
    if not sync_enabled() or df is None or df.empty:
        return
    frame = _prepare_frame(df)
    frame = frame[frame["tran_id"] != ""]  # rows without a PK can't be stored
    if frame.empty:
        return
    cols = list(frame.columns)
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != "tran_id")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE _txn_stage (LIKE transactions INCLUDING DEFAULTS) "
            "ON COMMIT DROP"
        )
        _copy_frame(cur, "_txn_stage", frame)
        col_list = ", ".join(f'"{c}"' for c in cols)
        cur.execute(
            f"INSERT INTO transactions ({col_list}) "
            f"SELECT {col_list} FROM _txn_stage "
            f"ON CONFLICT (tran_id) DO UPDATE SET {updates}"
        )
        conn.commit()
    log.info("Supabase: upserted %d transactions", len(frame))


def delete_transactions(tran_ids) -> None:
    """Remove rows no longer present (e.g. originals superseded by amendments)."""
    ids = [int(t) for t in tran_ids if str(t).strip().isdigit()]
    if not sync_enabled() or not ids:
        return
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM transactions WHERE tran_id = ANY(%s)", (ids,))
        conn.commit()
    log.info("Supabase: deleted %d superseded transactions", len(ids))


def _load_frame_chunked(frame: pd.DataFrame, attempts: int = 6) -> int:
    """COPY a shard into transactions committing every COPY_CHUNK_ROWS rows, so
    progress is durable and a dropped TLS link only costs one small chunk.
    Reuses one connection; reconnects only when a chunk fails."""
    import time
    import psycopg2
    cols = list(frame.columns)
    col_list = ", ".join(f'"{c}"' for c in cols)
    copy_sql = f"COPY transactions ({col_list}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"
    conn = _connect()
    loaded = 0
    try:
        for start in range(0, len(frame), COPY_CHUNK_ROWS):
            sub = frame.iloc[start:start + COPY_CHUNK_ROWS]
            for attempt in range(1, attempts + 1):
                try:
                    buf = io.StringIO()
                    sub.to_csv(buf, index=False, header=True)
                    buf.seek(0)
                    with conn.cursor() as cur:
                        cur.copy_expert(copy_sql, buf)
                    conn.commit()
                    loaded += len(sub)
                    break
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    try: conn.close()
                    except Exception: pass
                    if attempt == attempts:
                        raise
                    time.sleep(attempt * 2)
                    conn = _connect()
    finally:
        try: conn.close()
        except Exception: pass
    return loaded


def full_reload_transactions(shard_dir: Path) -> None:
    """One-time / periodic full load: truncate, then COPY every txn_*.csv.gz
    shard on its own connection so a dropped link only retries one shard."""
    if not sync_enabled():
        log.warning("SUPABASE_DB_URL not set — skipping full transaction reload")
        return
    shards = sorted(Path(shard_dir).glob("txn_*.csv.gz"))
    if not shards:
        log.warning("No transaction shards found in %s", shard_dir)
        return

    # Drop the trigram GIN indexes for the load: maintaining them per-row during
    # a 3M-row bulk COPY is the dominant cost. We rebuild them once at the end,
    # which is far faster. (Definitions mirror migration 004.)
    with _connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP INDEX IF EXISTS idx_txn_payee_trgm")
            cur.execute("DROP INDEX IF EXISTS idx_txn_filer_trgm")
            cur.execute("TRUNCATE transactions")
    log.info("Supabase: dropped trigram indexes, truncated, loading %d shards…", len(shards))

    total = 0
    for shard in shards:
        df = pd.read_csv(shard, compression="gzip", dtype=str)
        frame = _prepare_frame(df)
        frame = frame[frame["tran_id"] != ""]
        total += _load_frame_chunked(frame)
        log.info("Supabase: loaded %s (%d rows, %d total)", shard.name, len(frame), total)

    log.info("Supabase: rebuilding trigram indexes + analyzing…")
    with _connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_txn_payee_trgm "
                        "ON transactions USING gin (contributor_payee_canonical gin_trgm_ops)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_txn_filer_trgm "
                        "ON transactions USING gin (filer_canonical gin_trgm_ops)")
            cur.execute("ANALYZE transactions")
    log.info("Supabase: full reload complete — %d transactions", total)


# ── Dashboard aggregate blobs ────────────────────────────────────────────────
def upsert_dashboard_cache(key: str, data) -> None:
    if not sync_enabled():
        return
    payload = json.dumps(data, default=str)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO dashboard_cache (key, data, updated_at) "
            "VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            (key, payload),
        )
        conn.commit()
    log.info("Supabase: cached dashboard aggregate '%s'", key)


def bulk_upsert_filer_detail(rows: list[dict]) -> None:
    """rows: list of {slug, name, filer_id, detail(dict)}."""
    if not sync_enabled() or not rows:
        return
    from psycopg2.extras import execute_values
    values = [
        (r["slug"], r.get("name"), r.get("filer_id") or None, json.dumps(r["detail"], default=str))
        for r in rows
    ]
    with _connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO filer_detail (slug, name, filer_id, detail, updated_at) "
            "VALUES %s "
            "ON CONFLICT (slug) DO UPDATE SET "
            "name = EXCLUDED.name, filer_id = EXCLUDED.filer_id, "
            "detail = EXCLUDED.detail, updated_at = now()",
            values,
            template="(%s, %s, %s, %s::jsonb, now())",
            page_size=500,
        )
        conn.commit()
    log.info("Supabase: upserted %d filer_detail rows", len(rows))


# ── Full-dataset CSV upload to Storage (the "Download all" button) ───────────
def upload_full_csv(csv_gz_path: Path) -> None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    bucket = os.environ.get("SUPABASE_STORAGE_BUCKET", "exports")
    if not (url and key) or not Path(csv_gz_path).exists():
        return
    import requests
    object_path = "transactions.csv.gz"
    endpoint = f"{url}/storage/v1/object/{bucket}/{object_path}"
    with open(csv_gz_path, "rb") as f:
        resp = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/gzip",
                "x-upsert": "true",
            },
            data=f,
        )
    if resp.status_code in (200, 201):
        log.info("Supabase: uploaded full CSV to %s/%s", bucket, object_path)
    else:
        log.warning("Supabase: full CSV upload failed (%s): %s", resp.status_code, resp.text[:200])
