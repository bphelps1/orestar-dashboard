"""
refresh_donor_aggregates.py — rebuild the donor-facing blobs by resolved entity.

The Donors tab read `top_donors` / `by_contributor_type`, which process.py
builds from the pandas dataframe keyed by *raw* contributor name. The dataframe
has no `donor_id`, so donor resolution never reached them: /donors showed
"Philip Knight" with 18 aliases merged while the Donors tab still listed the
spellings separately. This rebuilds both from Postgres so the merged entity is
what users see everywhere.

Two different methods, deliberately:

  • top_donors — recomputed exactly from `transactions` grouped by `donor_id`.
    This is the headline leaderboard, and re-ranking has to consider every
    transaction: an entity whose spellings each sat below the old top-1000
    cutoff should still appear once merged.

  • by_contributor_type — the existing per-type / per-month lists are re-keyed
    onto entities and re-summed. Those lists encode contributor-type rules
    (out-of-state splits, month buckets) that live in process.py; re-deriving
    them in SQL would duplicate that logic for no gain.

Usage:  python scraper/refresh_donor_aggregates.py
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync as s

TOP_N = 1000
# Contributions are CASH ONLY app-wide; in-kind is a separate, distinct metric
# and is never folded into a contribution total. summary.total_contributions,
# filer total_in and donors.total_given all use this same rule, so the Donors
# tab, /donors and the Overview headline reconcile.
CASH_CONTRIB = ("t.tran_type = 'C' and "
                "coalesce(t.sub_type,'') <> 'In-Kind Contribution'")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def build_top_donors(cur) -> dict:
    """{all_time: [{name,total,donor_id}], by_year: {year: [...]}} by entity."""
    log.info("Rebuilding top_donors from transactions grouped by donor_id…")
    cur.execute(f"""
        select t.donor_id, max(d.display_name), round(sum(t.amount)::numeric, 2)
        from transactions t
        join donors d on d.donor_id = t.donor_id
        where {CASH_CONTRIB}
        group by t.donor_id
        order by 3 desc
        limit %s
    """, (TOP_N,))
    all_time = [{"name": n, "total": float(v), "donor_id": did}
                for did, n, v in cur.fetchall()]

    cur.execute(f"""
        select yr, donor_id, name, total from (
          select extract(year from t.tran_date)::int as yr,
                 t.donor_id,
                 max(d.display_name) as name,
                 round(sum(t.amount)::numeric, 2) as total,
                 row_number() over (
                   partition by extract(year from t.tran_date)::int
                   order by sum(t.amount) desc) as rn
          from transactions t
          join donors d on d.donor_id = t.donor_id
          where {CASH_CONTRIB} and t.tran_date is not null
          group by 1, 2
        ) x where rn <= %s order by yr, total desc
    """, (TOP_N,))
    by_year: dict[str, list] = defaultdict(list)
    for yr, did, name, total in cur.fetchall():
        by_year[str(yr)].append({"name": name, "total": float(total), "donor_id": did})

    log.info("  %d entities all-time, %d years", len(all_time), len(by_year))
    return {"all_time": all_time, "by_year": dict(by_year)}


def canonical_to_entity(cur) -> dict:
    """contributor_payee_canonical -> (display_name, donor_id).

    The blobs are keyed by the canonical name, not the raw one, so the mapping
    is taken from `transactions` rather than `donor_aliases`.
    """
    cur.execute("""
        select t.contributor_payee_canonical, t.donor_id, max(d.display_name)
        from transactions t
        join donors d on d.donor_id = t.donor_id
        where coalesce(t.contributor_payee_canonical,'') <> ''
        group by 1, 2
    """)
    out = {}
    for canon, did, disp in cur.fetchall():
        # A canonical name maps to one entity in practice; if it somehow spans
        # two, keep the first — the merge still collapses the common case.
        out.setdefault(canon, (disp, did))
    return out


def remap_donor_list(rows: list, mapping: dict) -> list:
    """Merge a [{name,total}] list onto entities, re-sum, re-sort."""
    merged: dict[str, dict] = {}
    for r in rows or []:
        name = r.get("name", "")
        disp, did = mapping.get(name, (name, None))
        cur = merged.setdefault(disp, {"name": disp, "total": 0.0, "donor_id": did})
        cur["total"] = round(cur["total"] + (r.get("total") or 0), 2)
    return sorted(merged.values(), key=lambda x: -x["total"])


def remap_by_contributor_type(blob: dict, mapping: dict) -> dict:
    """Re-key every nested top_donors list onto entities, preserving structure."""
    def fix_types(type_rows):
        for tr in type_rows or []:
            if "top_donors" in tr:
                keep = len(tr["top_donors"])
                tr["top_donors"] = remap_donor_list(tr["top_donors"], mapping)[:keep]
        return type_rows

    out = dict(blob)
    if "all_time" in out:
        out["all_time"] = fix_types(out["all_time"])
    for section in ("by_year", "by_month"):
        if isinstance(out.get(section), dict):
            out[section] = {k: fix_types(v) for k, v in out[section].items()}
    return out


def _upsert(conn, cur, key: str, data) -> None:
    """Write through the connection we already hold.

    supabase_sync.upsert_dashboard_cache opens its own connection, which the
    pooler may have dropped while the long aggregation queries were running.
    """
    cur.execute(
        "insert into dashboard_cache (key, data, updated_at) "
        "values (%s, %s::jsonb, now()) "
        "on conflict (key) do update set data = excluded.data, updated_at = now()",
        (key, json.dumps(data, default=str)),
    )
    conn.commit()
    log.info("  wrote dashboard_cache['%s']", key)


def main() -> int:
    conn = s._connect()
    cur = conn.cursor()

    cur.execute("select count(*) from donors")
    if not cur.fetchone()[0]:
        log.error("donors table is empty — run resolve_donors.py first")
        return 1

    top = build_top_donors(cur)
    _upsert(conn, cur, "top_donors", top)

    log.info("Re-keying by_contributor_type onto entities…")
    mapping = canonical_to_entity(cur)
    log.info("  %d canonical names -> %d entities",
             len(mapping), len({v[1] for v in mapping.values()}))
    cur.execute("select data from dashboard_cache where key='by_contributor_type'")
    row = cur.fetchone()
    if row:
        _upsert(conn, cur, "by_contributor_type",
                remap_by_contributor_type(row[0], mapping))
    else:
        log.warning("by_contributor_type not in dashboard_cache — skipped")

    conn.close()
    log.info("Donor aggregates refreshed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
