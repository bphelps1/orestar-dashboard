"""
db_admin.py — apply Supabase migrations and verify schema.

Reads credentials from .env via supabase_sync (never prints them).

Usage:
    python db_admin.py apply            # run migrations 004-006 in order
    python db_admin.py verify           # report tables / indexes / policies / roles
    python db_admin.py seed-aggregates  # upsert dashboard_cache + filer_detail from data/aggregated
"""
import json
import sys
from pathlib import Path

import supabase_sync as s

ROOT = Path(__file__).resolve().parent.parent
AGG_DIR = ROOT / "data" / "aggregated"
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
MIGRATIONS = [
    "004_transactions.sql",
    "005_aggregate_views.sql",
    "006_public_query_role.sql",
    "007_donors.sql",
]


def apply():
    conn = s._connect()
    conn.autocommit = True
    cur = conn.cursor()
    for name in MIGRATIONS:
        path = MIGRATIONS_DIR / name
        sql = path.read_text()
        print(f"→ applying {name} …", flush=True)
        cur.execute(sql)
        print(f"  ✓ {name}")
    conn.close()
    print("All migrations applied.")


def verify():
    conn = s._connect()
    cur = conn.cursor()

    def q(sql, args=None):
        cur.execute(sql, args or ())
        return cur.fetchall()

    print("== Tables ==")
    for (t,) in q("""
        select table_name from information_schema.tables
        where table_schema='public' and table_name in
          ('transactions','dashboard_cache','filer_detail')
        order by table_name"""):
        print("  ", t)

    print("== transactions indexes ==")
    for (i,) in q("select indexname from pg_indexes where tablename='transactions' order by indexname"):
        print("  ", i)

    print("== RLS policies (public read) ==")
    for tbl, pol in q("""
        select tablename, policyname from pg_policies
        where schemaname='public' and tablename in
          ('transactions','dashboard_cache','filer_detail')
        order by tablename"""):
        print(f"   {tbl}: {pol}")

    print("== query schema view ==")
    for (v,) in q("select table_name from information_schema.views where table_schema='query'"):
        print("  query.", v, sep="")

    print("== public_query role ==")
    rows = q("select rolname, rolcanlogin from pg_roles where rolname='public_query'")
    print("  ", rows[0] if rows else "MISSING")

    print("== row counts ==")
    for tbl in ("transactions", "dashboard_cache", "filer_detail"):
        (n,) = q(f"select count(*) from {tbl}")[0]
        print(f"   {tbl}: {n:,}")

    conn.close()


def seed_aggregates():
    """Upsert the dashboard aggregate blobs and per-filer detail from the
    committed data/aggregated JSON files (no scraping/regeneration needed)."""
    # dashboard_cache: every top-level *.json → keyed by filename stem
    for path in sorted(AGG_DIR.glob("*.json")):
        key = path.stem
        s.upsert_dashboard_cache(key, json.loads(path.read_text()))

    # filer_detail: one row per data/aggregated/filers/*.json
    slug_to_fid = {}
    idx_path = AGG_DIR / "filer_index.json"
    if idx_path.exists():
        for row in json.loads(idx_path.read_text()):
            slug_to_fid[row.get("slug")] = row.get("filer_id", "")

    rows = []
    for path in sorted((AGG_DIR / "filers").glob("*.json")):
        detail = json.loads(path.read_text())
        slug = detail.get("slug") or path.stem
        rows.append({
            "slug": slug,
            "name": detail.get("name"),
            "filer_id": slug_to_fid.get(slug, ""),
            "detail": detail,
        })
    print(f"→ seeding {len(rows)} filer_detail rows …", flush=True)
    # Upsert in batches so one call isn't enormous.
    for i in range(0, len(rows), 500):
        s.bulk_upsert_filer_detail(rows[i:i + 500])
    print("Aggregate seed complete.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    {"apply": apply, "verify": verify, "seed-aggregates": seed_aggregates}.get(cmd, verify)()
