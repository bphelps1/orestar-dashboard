#!/usr/bin/env python3
"""
refresh_leadership.py — Refresh Oregon legislative leadership roles.

Scrapes the Oregon Legislature website for current leadership positions
and updates the Supabase leadership_roles table.

Can be run:
  - Via GitHub Actions monthly cron (uses SUPABASE_URL / SUPABASE_SERVICE_KEY env vars)
  - Manually from the command line

Usage:
  python scraper/refresh_leadership.py
"""

import json
import logging
import os
import sys
from datetime import date, datetime

import requests

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Oregon legislative leadership roles to track
LEADERSHIP_TITLES = [
    "Speaker of the House",
    "Senate President",
    "Senate President Pro Tem",
    "House Speaker Pro Tem",
    "Senate Majority Leader",
    "Senate Minority Leader",
    "House Majority Leader",
    "House Minority Leader",
    "Senate Majority Whip",
    "Senate Minority Whip",
    "House Majority Whip",
    "House Minority Whip",
]

# Known committee chair keywords
CHAIR_KEYWORDS = ["Chair", "Co-Chair", "Vice-Chair"]


def get_supabase_client():
    """Get Supabase client from environment variables."""
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")

    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables required")
        sys.exit(1)

    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        log.error("supabase package not installed. Run: pip install supabase")
        sys.exit(1)


def scrape_leadership():
    """
    Scrape current Oregon legislative leadership.

    Returns a list of dicts with keys:
      filer_name, role_title, chamber, party, district, source
    """
    roles = []

    # Try the Oregon Legislature API / website
    # This is a placeholder — the actual scraping logic depends on
    # what endpoints/pages are available. For now, return empty
    # and rely on manual admin entry until the scrape source is confirmed.
    log.info("Leadership scraping not yet configured — using manual data only")
    log.info("Add leadership roles via the admin interface or directly in Supabase")

    return roles


def update_supabase(sb, roles):
    """Update leadership_roles table in Supabase."""
    today = date.today().isoformat()
    updated = 0

    for role in roles:
        # Check if this role already exists and is current
        existing = (
            sb.table("leadership_roles")
            .select("id")
            .eq("filer_name", role["filer_name"])
            .eq("role_title", role["role_title"])
            .is_("end_date", "null")
            .execute()
        )

        if existing.data:
            # Role already exists and is current — update it
            sb.table("leadership_roles").update({
                "updated_at": datetime.utcnow().isoformat(),
                "party": role.get("party"),
                "district": role.get("district"),
            }).eq("id", existing.data[0]["id"]).execute()
        else:
            # New role — insert
            sb.table("leadership_roles").insert({
                "filer_name": role["filer_name"],
                "role_title": role["role_title"],
                "chamber": role.get("chamber"),
                "party": role.get("party"),
                "district": role.get("district"),
                "effective_date": today,
                "source": role.get("source", "scraped"),
            }).execute()
            updated += 1

    # Log the refresh
    sb.table("leadership_refresh_log").insert({
        "source": "github_action",
        "roles_updated": updated,
        "notes": f"Processed {len(roles)} roles, {updated} new",
    }).execute()

    log.info("Leadership refresh complete: %d roles processed, %d new", len(roles), updated)
    return updated


def main():
    log.info("Starting leadership role refresh…")
    sb = get_supabase_client()
    roles = scrape_leadership()

    if roles:
        update_supabase(sb, roles)
    else:
        # Even with no scraped roles, log the refresh attempt
        sb.table("leadership_refresh_log").insert({
            "source": "github_action",
            "roles_updated": 0,
            "notes": "No roles scraped — manual data only",
        }).execute()
        log.info("No roles to update. Manual entry available via admin interface.")


if __name__ == "__main__":
    main()
