#!/usr/bin/env python3
"""Generate activity_snapshot.json from existing aggregated data.

Reads filer_index.json, per-filer detail files, and recent_transactions.json
to compute recent fundraising metrics grouped by office tier, growth rates,
and top donors. Designed to run quickly from already-aggregated data.

Can be run standalone or called from process.py.
"""

import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

ROOT = Path(__file__).parent.parent
AGG_DIR = ROOT / "data" / "aggregated"
FILERS_DIR = AGG_DIR / "filers"

STATEWIDE_OFFICES = {
    "Governor", "Secretary of State", "Attorney General",
    "State Treasurer", "Superintendent of Public Instruction",
    "Commissioner of the Bureau of Labor and Industries",
}
LEGISLATIVE_OFFICES = {"State Senator", "State Representative"}


def office_tier(office: str) -> str | None:
    if not office:
        return None
    if office in STATEWIDE_OFFICES:
        return "statewide"
    if office in LEGISLATIVE_OFFICES:
        return "legislative"
    return "local"


def months_in_range(start_ym: str, end_ym: str) -> list[str]:
    """Return list of YYYY-MM strings from start to end inclusive."""
    sy, sm = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    result = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


def sum_timeline_months(timeline: list[dict], target_months: set[str]) -> float:
    """Sum contributions from timeline entries matching target months."""
    total = 0.0
    for entry in timeline:
        if entry.get("month") in target_months:
            total += entry.get("contributions", 0)
    return total


def aggregate_top_donors_from_filers(
    filer_details: list[dict],
    target_months: set[str],
) -> list[dict]:
    """Aggregate top donors across all filer detail files for given months.

    Uses by_contributor_type_by_month data from per-filer detail files,
    which captures the full month's data (not limited to recent transactions).
    Returns top 10 donors sorted by total amount.
    """
    donor_totals: dict[str, float] = defaultdict(float)
    donor_filers: dict[str, set] = defaultdict(set)
    donor_filer_amounts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    donor_filer_slugs: dict[str, dict[str, str]] = {}  # donor_name → {filer_name: slug}

    for detail in filer_details:
        filer_name = detail.get("name", "")
        filer_slug = detail.get("slug", "")
        by_month = detail.get("by_contributor_type_by_month", {})
        for month, type_rows in by_month.items():
            if month not in target_months:
                continue
            for tr in type_rows:
                for d in tr.get("top_donors", []):
                    name = d.get("name", "")
                    if not name or name.startswith("Miscellaneous Cash"):
                        continue
                    amt = d.get("total", 0)
                    donor_totals[name] += amt
                    donor_filers[name].add(filer_name)
                    donor_filer_amounts[name][filer_name] += amt
                    if name not in donor_filer_slugs:
                        donor_filer_slugs[name] = {}
                    donor_filer_slugs[name][filer_name] = filer_slug

    # Sort by total, take top 10
    ranked = sorted(donor_totals.items(), key=lambda x: -x[1])[:10]
    return [
        {
            "name": name,
            "total": round(total, 2),
            "committees": len(donor_filers[name]),
            "details": sorted(
                [{"filer": fn, "slug": donor_filer_slugs[name].get(fn, ""), "amount": round(amt, 2)}
                 for fn, amt in donor_filer_amounts[name].items()],
                key=lambda x: -x["amount"]
            )[:10],
        }
        for name, total in ranked
    ]


def build_races(filer_metrics: list[dict], index: list[dict]) -> list[dict]:
    """Group candidates by office_district to find contested races.

    ONLY includes candidates whose ORESTAR 'election' field (scraped from
    the Statement of Organization 'Election/Office' field) contains a 2026
    election. Candidates without this field or from previous cycles are
    excluded entirely — no fallbacks or guessing.
    """
    import re as _re
    metric_by_slug = {fm["slug"]: fm for fm in filer_metrics}

    # Show races for the current year and next year. This covers:
    # - Even years (2026): statewide + legislative elections
    # - Odd years (2027): local/municipal elections
    # The window automatically advances as time passes.
    now = datetime.now()
    valid_election_years = {now.year, now.year + 1}

    races = defaultdict(list)
    for row in index:
        if row.get("committee_type") != "Candidate Committee":
            continue
        od = row.get("office_district", "")
        if not od:
            continue

        # STRICT: only include candidates with an upcoming election
        # field from ORESTAR (e.g., "2026 Primary Election")
        election = row.get("election", "")
        if not election:
            continue  # No election data scraped yet — skip
        yr_match = _re.match(r"(\d{4})", election)
        if not yr_match:
            continue
        election_year = int(yr_match.group(1))
        if election_year not in valid_election_years:
            continue

        slug = row.get("slug", "")
        fm = metric_by_slug.get(slug, {})
        raised = fm.get("raised_cycle", 0)
        races[od].append({
            "slug": slug,
            "name": row.get("name", ""),
            "party": row.get("party", ""),
            "candidate_name": row.get("candidate_name", ""),
            "raised_cycle": round(raised, 2),
            "cash_on_hand": row.get("cash_on_hand", 0),
        })

    contested = []
    for office, candidates in races.items():
        if len(candidates) < 2:
            continue
        candidates.sort(key=lambda c: -c["raised_cycle"])
        total = sum(c["raised_cycle"] for c in candidates)
        # Determine if statewide
        base_office = office.split(",")[0].strip()
        is_statewide = base_office in STATEWIDE_OFFICES
        contested.append({
            "office": office,
            "total_raised": round(total, 2),
            "is_statewide": is_statewide,
            "candidates": candidates,  # All candidates (frontend shows top 5 + expand)
        })

    # Sort: statewide first (by total raised), then non-statewide (by total raised)
    statewide = sorted([r for r in contested if r["is_statewide"]], key=lambda r: -r["total_raised"])
    other = sorted([r for r in contested if not r["is_statewide"]], key=lambda r: -r["total_raised"])[:5]
    return statewide + other


def generate(agg_dir: Path = AGG_DIR, filers_dir: Path = FILERS_DIR) -> dict:
    """Generate the activity snapshot data structure."""
    with open(agg_dir / "filer_index.json") as f:
        index = json.load(f)

    # Determine time periods
    now = datetime.now()
    current_ym = now.strftime("%Y-%m")

    # Last 3 months (current + 2 prior)
    recent_months = []
    y, m = now.year, now.month
    for _ in range(3):
        recent_months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    recent_3m = set(recent_months)

    # Prior 3 months (the 3 months before that)
    prior_months = []
    for _ in range(3):
        prior_months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m < 1:
            m = 12
            y -= 1
    prior_3m = set(prior_months)

    # Last 1 month
    recent_1m = {current_ym}
    y1, m1 = now.year, now.month - 1
    if m1 < 1:
        m1 = 12
        y1 -= 1
    recent_1m.add(f"{y1:04d}-{m1:02d}")

    # Prior 1 month (for 30d growth)
    prior_1m_months = set()
    for _ in range(2):
        m1 -= 1
        if m1 < 1:
            m1 = 12
            y1 -= 1
        prior_1m_months.add(f"{y1:04d}-{m1:02d}")

    # Cycle: Jan 2025 onward (post-2024 election)
    cycle_start = "2025-01"
    cycle_months = set(months_in_range(cycle_start, current_ym))

    # Prior cycle equivalent (same number of months before cycle start)
    cycle_len = len(cycle_months)
    prior_cycle_months = []
    y_pc, m_pc = 2025, 0
    if m_pc < 1:
        m_pc = 12
        y_pc -= 1
    for _ in range(cycle_len):
        prior_cycle_months.append(f"{y_pc:04d}-{m_pc:02d}")
        m_pc -= 1
        if m_pc < 1:
            m_pc = 12
            y_pc -= 1
    prior_cycle = set(prior_cycle_months)

    # Filter to candidate committees with offices (for Raising/Momentum lanes)
    candidates = []
    for row in index:
        if row.get("committee_type") != "Candidate Committee":
            continue
        tier = office_tier(row.get("office", ""))
        if not tier:
            continue
        candidates.append((row, tier))

    print(f"Processing {len(candidates)} candidate committees...")

    # Load ALL filer detail files for donor aggregation (Biggest Donors
    # lane should capture donations to PACs, party committees, etc.)
    all_filer_slugs = [row["slug"] for row in index]
    all_filer_details = []
    print(f"Loading {len(all_filer_slugs)} filer detail files for donor aggregation...")
    for slug in all_filer_slugs:
        detail_path = filers_dir / f"{slug}.json"
        if not detail_path.exists():
            continue
        with open(detail_path) as f:
            all_filer_details.append(json.load(f))

    # Build candidate-specific detail lookup for timeline metrics
    candidate_details = {}  # slug -> detail
    for detail in all_filer_details:
        candidate_details[detail.get("slug", "")] = detail

    # Collect metrics per candidate filer
    filer_metrics = []
    for row, tier in candidates:
        slug = row["slug"]
        detail = candidate_details.get(slug)
        if not detail:
            continue

        timeline = detail.get("timeline", [])

        # Compute period totals
        raised_1m = sum_timeline_months(timeline, recent_1m)
        raised_prior_1m = sum_timeline_months(timeline, prior_1m_months)
        raised_3m = sum_timeline_months(timeline, recent_3m)
        raised_prior_3m = sum_timeline_months(timeline, prior_3m)
        raised_cycle = sum_timeline_months(timeline, cycle_months)
        raised_prior_cycle = sum_timeline_months(timeline, prior_cycle)

        # Growth rates
        def growth(current, prior):
            if prior <= 0:
                return 999 if current > 0 else 0
            return round((current - prior) / prior * 100)

        growth_1m = growth(raised_1m, raised_prior_1m)
        growth_3m = growth(raised_3m, raised_prior_3m)
        growth_cycle = growth(raised_cycle, raised_prior_cycle)

        filer_metrics.append({
            "slug": row["slug"],
            "name": row["name"],
            "office": row.get("office", ""),
            "party": row.get("party", ""),
            "tier": tier,
            "raised_1m": round(raised_1m, 2),
            "raised_3m": round(raised_3m, 2),
            "raised_cycle": round(raised_cycle, 2),
            "growth_1m": growth_1m,
            "growth_3m": growth_3m,
            "growth_cycle": growth_cycle,
            "cash_on_hand": row.get("cash_on_hand", 0),
            "total_in": row.get("total_in", 0),
        })

    # Collect metrics for non-candidate committees (PACs, party committees, etc.)
    committee_metrics = []
    for row in index:
        if row.get("committee_type") == "Candidate Committee":
            continue
        slug = row["slug"]
        detail = candidate_details.get(slug)
        if not detail:
            continue
        timeline = detail.get("timeline", [])
        raised_1m = sum_timeline_months(timeline, recent_1m)
        raised_prior_1m = sum_timeline_months(timeline, prior_1m_months)
        raised_3m = sum_timeline_months(timeline, recent_3m)
        raised_prior_3m = sum_timeline_months(timeline, prior_3m)
        raised_cycle = sum_timeline_months(timeline, cycle_months)
        raised_prior_cycle = sum_timeline_months(timeline, prior_cycle)
        if raised_1m <= 0 and raised_3m <= 0 and raised_cycle <= 0:
            continue

        def _cgrowth(current, prior):
            if prior <= 0:
                return 999 if current > 0 else 0
            return round((current - prior) / prior * 100)

        committee_metrics.append({
            "slug": slug,
            "name": row["name"],
            "committee_type": row.get("committee_type", ""),
            "raised_1m": round(raised_1m, 2),
            "raised_3m": round(raised_3m, 2),
            "raised_cycle": round(raised_cycle, 2),
            "growth_1m": _cgrowth(raised_1m, raised_prior_1m),
            "growth_3m": _cgrowth(raised_3m, raised_prior_3m),
            "growth_cycle": _cgrowth(raised_cycle, raised_prior_cycle),
        })

    # Aggregate top donors from per-filer monthly data (full, not limited)
    print(f"Aggregating top donors from {len(all_filer_details)} filer detail files...")
    top_donors_30d = aggregate_top_donors_from_filers(all_filer_details, recent_1m)
    top_donors_90d = aggregate_top_donors_from_filers(all_filer_details, recent_3m)
    top_donors_cycle = aggregate_top_donors_from_filers(all_filer_details, cycle_months)

    # Build output per period
    def build_period(metric_key, growth_key, top_donors, min_raised=5000):
        by_tier = {"statewide": [], "legislative": [], "local": [], "committees": []}
        growth_candidates = []

        for fm in filer_metrics:
            raised = fm[metric_key]
            if raised <= 0:
                continue

            by_tier[fm["tier"]].append({
                "slug": fm["slug"],
                "name": fm["name"],
                "office": fm["office"],
                "party": fm["party"],
                "raised": raised,
                "cash_on_hand": fm["cash_on_hand"],
            })

            # Growth: include all actively raising candidates
            if raised >= min_raised:
                growth_candidates.append({
                    "slug": fm["slug"],
                    "name": fm["name"],
                    "office": fm["office"],
                    "party": fm["party"],
                    "tier": fm["tier"],
                    "raised": raised,
                    "growth_pct": fm[growth_key],
                    "is_new": fm[growth_key] == 999,
                })

        # Add non-candidate committees (raising + growth)
        for cm in committee_metrics:
            raised = cm[metric_key]
            if raised <= 0:
                continue
            by_tier["committees"].append({
                "slug": cm["slug"],
                "name": cm["name"],
                "office": cm.get("committee_type", ""),
                "party": "",
                "raised": raised,
            })
            # Committee growth
            if raised >= min_raised:
                growth_candidates.append({
                    "slug": cm["slug"],
                    "name": cm["name"],
                    "office": cm.get("committee_type", ""),
                    "party": "",
                    "tier": "committees",
                    "raised": raised,
                    "growth_pct": cm[growth_key],
                    "is_new": cm[growth_key] == 999,
                })

        # Sort each tier by raised amount, take top entries
        for tier in by_tier:
            by_tier[tier].sort(key=lambda x: -x["raised"])
            by_tier[tier] = by_tier[tier][:3]

        # Top growth: only existing filers with positive growth (no new entrants)
        # Sorted by growth percentage descending, flat list (no tiers)
        top_growth_list = sorted(
            [c for c in growth_candidates if not c.get("is_new") and c["growth_pct"] > 0],
            key=lambda x: -x["growth_pct"],
        )[:12]

        return {
            "by_office_tier": by_tier,
            "top_growth": top_growth_list,
            "top_donors": top_donors,
        }

    snapshot = {
        "generated": now.isoformat(),
        "periods": {
            "30d": {
                "label": "Last 30 Days",
                "months": sorted(recent_1m),
                **build_period("raised_1m", "growth_1m", top_donors_30d, min_raised=500),
            },
            "90d": {
                "label": "Last 90 Days",
                "months": sorted(recent_3m),
                **build_period("raised_3m", "growth_3m", top_donors_90d, min_raised=1000),
            },
            "cycle": {
                "label": "This Cycle",
                "months": sorted(cycle_months),
                **build_period("raised_cycle", "growth_cycle", top_donors_cycle, min_raised=5000),
            },
        },
        "meta": {
            "total_candidates": len(candidates),
            "statewide_count": sum(1 for _, t in candidates if t == "statewide"),
            "legislative_count": sum(1 for _, t in candidates if t == "legislative"),
            "local_count": sum(1 for _, t in candidates if t == "local"),
        },
    }

    snapshot["races"] = build_races(filer_metrics, index)

    return snapshot


def main():
    snapshot = generate()
    out_path = AGG_DIR / "activity_snapshot.json"
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Wrote {out_path}")
    print(f"  Candidates: {snapshot['meta']['total_candidates']}")
    print(f"  Statewide: {snapshot['meta']['statewide_count']}")
    print(f"  Legislative: {snapshot['meta']['legislative_count']}")
    print(f"  Local: {snapshot['meta']['local_count']}")

    for period_key, period_data in snapshot["periods"].items():
        print(f"\n  {period_data['label']}:")
        for tier in ["statewide", "legislative", "local"]:
            entries = period_data["by_office_tier"][tier]
            if entries:
                top = entries[0]
                print(f"    {tier}: {top['name']} — ${top['raised']:,.0f}")
        growth = period_data["top_growth"]
        if growth:
            top = growth[0]
            print(f"    Top growth: {top['name']} — +{top['growth_pct']}%")
        donors = period_data["top_donors"]
        if donors:
            top = donors[0]
            print(f"    Top donor: {top['name']} — ${top['total']:,.0f} ({top['committees']} committees)")


if __name__ == "__main__":
    main()
