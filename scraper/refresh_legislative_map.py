"""
refresh_legislative_map.py — rebuild the Races map from the candidate roster.

Reads the ballot roster (data/candidate_filings.json) and rebuilds only the
`legislative_map` key of the activity_snapshot blob in dashboard_cache.

Deliberately sources committees and cycle totals from the LIVE database
(dashboard_cache.filer_index + filer_detail) rather than data/aggregated/*.
Those local files lag the database, and generating the map from a stale copy
silently dropped 27 candidates from the map once already.

Usage:
    python scraper/refresh_legislative_map.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import supabase_sync as s
from generate_activity_snapshot import build_legislative_map_from_filings

ROOT = Path(__file__).parent.parent
FILINGS = ROOT / "data" / "candidate_filings.json"
CYCLE_START = "2025-01"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def main() -> int:
    if not FILINGS.exists():
        log.error("No %s — run fetch_candidates.py first", FILINGS)
        return 1
    filings = json.loads(FILINGS.read_text())
    if not filings.get("candidates"):
        log.error("Roster is empty — refusing to blank the Races map")
        return 1

    conn = s._connect()
    cur = conn.cursor()

    cur.execute("select data from dashboard_cache where key='filer_index'")
    row = cur.fetchone()
    if not row:
        log.error("filer_index not in dashboard_cache")
        return 1
    index = row[0]

    # cycle totals straight from the live per-filer timelines
    slugs = [r["slug"] for r in index
             if r.get("committee_type") == "Candidate Committee" and r.get("slug")]
    cur.execute("select slug, detail->'timeline' from filer_detail where slug = any(%s)",
                (slugs,))
    metrics = [
        {"slug": slug,
         "raised_cycle": round(sum(e.get("contributions", 0) for e in (tl or [])
                                   if (e.get("month") or "") >= CYCLE_START), 2)}
        for slug, tl in cur.fetchall()
    ]
    log.info("Loaded %d candidate committees, %d timelines", len(slugs), len(metrics))

    lm = build_legislative_map_from_filings(filings, metrics, index)

    cur.execute("select data from dashboard_cache where key='activity_snapshot'")
    snap_row = cur.fetchone()
    if not snap_row:
        log.error("activity_snapshot not in dashboard_cache")
        return 1
    snapshot = snap_row[0]
    snapshot["legislative_map"] = lm
    cur.execute(
        "update dashboard_cache set data = %s::jsonb, updated_at = now() "
        "where key = 'activity_snapshot'",
        (json.dumps(snapshot, default=str),),
    )
    conn.commit()
    conn.close()

    log.info("Races map refreshed — %s | %d districts (house) + %d (senate) | %s",
             lm.get("election"), len(lm["house"]), len(lm["senate"]), lm["match_stats"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
