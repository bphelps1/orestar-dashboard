#!/usr/bin/env python3
"""
fetch_filer_metadata.py — Scrape filer metadata (party, office, type) from ORESTAR.

Uses Playwright (headed browser) to navigate ORESTAR's "Committees/Filers by Name"
search, looking up each filer by committee ID and extracting:
  - Committee type (from page heading: "Statement of Organization for ___")
  - Office sought (from "Election/Office:" field on candidate pages)
  - Party affiliation (from "Party Affiliation:" field on candidate pages)
  - PAC type / nature (from "PAC Type:" and "Nature of Committee" on PAC pages)
  - Candidate name (from "Candidate Information" section)

The ORESTAR detail page has two main layouts:
  1. Candidate Committee: has "Candidate Information" section with office/party
  2. PAC/Party/Measure Committee: has "Nature of Committee" section, no party/office

Output: data/filer_metadata.json
  { "filer_id": { "committee_type": str, "office": str, "party": str,
                   "pac_type": str, "nature": str, "candidate_name": str,
                   "committee_name": str, "ts": float }, ... }

Usage:
    python scraper/fetch_filer_metadata.py [--filer-ids 19763 12345] [--max-filers 500]
    python scraper/fetch_filer_metadata.py --force   # re-scrape all
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

# Fraction of a batch that may fail before the run is treated as blocked
# rather than merely unlucky. Matches fetch_earliest_balances.py.
BATCH_FAILURE_ABORT = 0.5

log = logging.getLogger(__name__)

BASE_URL = "https://secure.sos.state.or.us/orestar"
SEARCH_URL = f"{BASE_URL}/GotoSearchByName.do"
DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_PATH = DATA_DIR / "filer_metadata.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Wait time after initial page load (F5 bot defense)
PAGE_RENDER_WAIT = 7
# Shorter wait for subsequent pages (session already established)
NAV_WAIT = 2
# Polite delay between filers
FILER_DELAY = 0.5


def get_all_filer_ids() -> list[str]:
    """Load all filer IDs from the aggregated filer index."""
    idx_path = DATA_DIR / "aggregated" / "filer_index.json"
    if not idx_path.exists():
        log.error("filer_index.json not found at %s", idx_path)
        return []
    with open(idx_path) as f:
        index = json.load(f)
    return [str(row["filer_id"]) for row in index if row.get("filer_id")]


def _scrape_filer_metadata(page, filer_id: str, first_load: bool) -> dict | None:
    """Search ORESTAR by committee ID and extract metadata from the detail page.

    The form at GotoSearchByName.do has:
      - input[name="committeeId"] for the ID field
      - input[type="submit"][value="Submit"] for the search button

    Searching by committee ID goes directly to the SOO (Statement of Organization)
    detail page for that filer.
    """
    page.goto(SEARCH_URL, timeout=60_000)
    time.sleep(PAGE_RENDER_WAIT if first_load else NAV_WAIT)

    if "secure.sos.state.or.us/orestar" not in page.url:
        log.warning("Redirected away from ORESTAR: %s", page.url)
        return None

    # Fill committee ID and submit
    id_field = page.locator('input[name="committeeId"]')
    if id_field.count() == 0:
        log.warning("Cannot find committeeId input field")
        return None

    id_field.fill(filer_id)
    page.wait_for_timeout(200)

    # First try WITHOUT "Include Discontinued Committees" checkbox
    disc_checkbox = page.locator('input[name="discontinuedSOO"][type="checkbox"]')
    if disc_checkbox.count() > 0 and disc_checkbox.first.is_checked():
        disc_checkbox.first.uncheck()
        page.wait_for_timeout(200)

    submit_btn = page.locator('input[type="submit"][value="Submit"]')
    if submit_btn.count() == 0:
        log.warning("No Submit button found")
        return None

    submit_btn.first.click()
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        log.warning("Timeout waiting for filer detail page for %s", filer_id)
        return None
    time.sleep(NAV_WAIT)

    text = page.inner_text("body")

    # If 0 results found, retry with discontinued checkbox checked
    if "0 found for the above search criteria" in text:
        log.debug("No active result for %s, retrying with discontinued", filer_id)
        page.goto(SEARCH_URL, timeout=60_000)
        time.sleep(NAV_WAIT)
        id_field = page.locator('input[name="committeeId"]')
        id_field.fill(filer_id)
        page.wait_for_timeout(200)
        disc_checkbox = page.locator('input[name="discontinuedSOO"][type="checkbox"]')
        if disc_checkbox.count() > 0 and not disc_checkbox.first.is_checked():
            disc_checkbox.first.check()
            page.wait_for_timeout(200)
        page.locator('input[type="submit"][value="Submit"]').first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        time.sleep(NAV_WAIT)
        text = page.inner_text("body")

    return _parse_detail_text(text, filer_id)


def _parse_detail_text(text: str, filer_id: str) -> dict | None:
    """Parse the ORESTAR SOO detail page text to extract metadata.

    Candidate Committee pages have:
      - Heading: "Statement of Organization for Candidate Committee"
      - "Name: <committee name>  ID: <id>"
      - "Election/Office: <election>\n<office, district>"
      - "Party Affiliation: <party>"
      - Candidate name in "Candidate Information" section

    PAC pages have:
      - Heading: "Statement of Organization for Political Action Committee"
      - "PAC Type: <type>"
      - "Nature of Committee" section with free-text description

    Party Committee pages:
      - "Statement of Organization for Political Party Committee"

    Measure Committee pages:
      - "Statement of Organization for Measure Committee"
    """
    result = {
        "committee_type": "",
        "election": "",
        "office": "",
        "party": "",
        "pac_type": "",
        "nature": "",
        "candidate_name": "",
        "committee_name": "",
    }

    # 1. Committee type from heading
    m = re.search(r"Statement of Organization for\s+(.+?)(?:\n|Committee Information)", text)
    if m:
        result["committee_type"] = m.group(1).strip()

    # 2. Committee name
    m = re.search(r"Name:\s*(.+?)(?:\s*ID:|$)", text, re.MULTILINE)
    if m:
        result["committee_name"] = m.group(1).strip()

    # 3. Party affiliation (candidate committees)
    m = re.search(r"Party Affiliation:\s*(.+)", text)
    if m:
        result["party"] = m.group(1).strip()

    # 4. Office sought — on candidate pages, appears as:
    #    "Election/Office:  2026 Primary Election\nState Representative, 25th District"
    #    Note: inner_text() uses \t for table cell boundaries, so stop at \t too
    m = re.search(r"Election/Office:\s*(.+?)(?:\t|\nParty Affiliation|\nCandidate Address)", text, re.DOTALL)
    if m:
        office_block = m.group(1).strip()
        # The first line is election, second line is office/district
        lines = [l.strip() for l in office_block.split("\n") if l.strip()]
        if len(lines) >= 2:
            result["election"] = lines[0]  # "2026 Primary Election"
            result["office"] = lines[1]    # "State Representative, 25th District"
        elif lines:
            result["office"] = lines[0]

    # 5. PAC type
    m = re.search(r"PAC Type:\s*(.+)", text)
    if m:
        result["pac_type"] = m.group(1).strip()

    # 6. Nature of Committee (PAC free-text)
    m = re.search(r"Nature of Committee\s*\n(.+?)(?:\nThe committee|\nAdditional Committee)", text, re.DOTALL)
    if m:
        result["nature"] = m.group(1).strip()

    # 7. Candidate name — from "Candidate Information" section
    #    "Name:  Benjamin W Bowman" right after "Candidate Information"
    m = re.search(r"Candidate Information\s*\n.*?Name:\s*(.+?)(?:\s*\n|$)", text)
    if m:
        result["candidate_name"] = m.group(1).strip()

    # Validate: must have at least committee_type
    if not result["committee_type"]:
        # Check if we're on a "0 found" results page
        if "0 found for the above search criteria" in text:
            log.debug("Filer %s not found on ORESTAR (0 results)", filer_id)
            result["committee_type"] = "Not Found"
            return result
        if "No results found" in text or "Committee not found" in text:
            log.debug("Filer %s not found on ORESTAR", filer_id)
            result["committee_type"] = "Not Found"
            return result
        # Try to detect type from text patterns
        if "Candidate Committee" in text:
            result["committee_type"] = "Candidate Committee"
        elif "Political Action Committee" in text:
            result["committee_type"] = "Political Action Committee"
        elif "Political Party Committee" in text:
            result["committee_type"] = "Political Party Committee"
        elif "Measure Committee" in text:
            result["committee_type"] = "Measure Committee"
        elif "Petition Committee" in text:
            result["committee_type"] = "Petition Committee"

    if result["committee_type"] or result["party"] or result["office"]:
        return result

    return None


def main():
    parser = argparse.ArgumentParser(description="Scrape filer metadata from ORESTAR")
    parser.add_argument("--filer-ids", nargs="+", help="Specific filer IDs to scrape")
    parser.add_argument("--max-filers", type=int, default=0, help="Max filers per run (0=all)")
    parser.add_argument("--force", action="store_true", help="Re-scrape even if cached")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load existing cache
    cache: dict[str, dict] = {}
    if CACHE_PATH.exists() and not args.force:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
        log.info("Loaded %d cached filer metadata entries", len(cache))

    # Determine which filers to scrape
    if args.filer_ids:
        filer_ids = args.filer_ids
    else:
        filer_ids = get_all_filer_ids()

    # Filter out already-cached (unless --force)
    # Re-scrape entries that are: Not Found, empty type, or candidate
    # committees missing the 'election' field (added after initial scrape)
    if not args.force:
        def _needs_scrape(fid):
            if fid not in cache:
                return True
            entry = cache[fid]
            if entry.get("committee_type") in ("Not Found", ""):
                return True
            # Candidate committees need the election field
            if entry.get("committee_type") == "Candidate Committee" and not entry.get("election"):
                return True
            return False
        filer_ids = [fid for fid in filer_ids if _needs_scrape(fid)]

    all_remaining = len(filer_ids)
    log.info("Filers to scrape: %d (of which %d already cached)", all_remaining, len(cache))

    if args.max_filers > 0:
        filer_ids = filer_ids[:args.max_filers]
    log.info("Will scrape %d filers this run", len(filer_ids))

    if not filer_ids:
        log.info("Nothing to scrape — all filers already cached")
        remaining_path = DATA_DIR / "filer_metadata_remaining.txt"
        remaining_path.write_text("0")
        return

    # Launch Playwright
    from playwright.sync_api import sync_playwright

    scraped = 0
    errors = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(user_agent=USER_AGENT, no_viewport=True)
        page = context.new_page()

        for i, fid in enumerate(filer_ids):
            log.info("[%d/%d] Scraping filer %s...", i + 1, len(filer_ids), fid)
            try:
                result = _scrape_filer_metadata(page, fid, first_load=(i == 0))
                if result:
                    result["ts"] = time.time()
                    cache[fid] = result
                    scraped += 1
                    log.info("  → type=%s office=%s party=%s",
                             result.get("committee_type", ""),
                             result.get("office", ""),
                             result.get("party", ""))
                else:
                    errors += 1
                    log.warning("  → No metadata found for filer %s", fid)
            except Exception as e:
                errors += 1
                log.error("  → Error scraping filer %s: %s", fid, e)

            # Save cache periodically (every 50 filers)
            if (i + 1) % 50 == 0:
                _save_cache(cache)
                log.info("  [checkpoint] Saved %d entries to cache", len(cache))

            time.sleep(FILER_DELAY)

        browser.close()

    _save_cache(cache)
    log.info("Done. Scraped %d, errors %d, total cached %d", scraped, errors, len(cache))

    # Write remaining count for workflow retrigger
    remaining = all_remaining - len(filer_ids)
    remaining_path = DATA_DIR / "filer_metadata_remaining.txt"
    remaining_path.write_text(str(remaining))
    log.info("Remaining filers: %d", remaining)

    # A batch that mostly failed is not a batch that ran.
    #
    # On 3 August this scraped 33 filers, failed 79, and exited 0 — the run
    # showed a green tick and the only trace was one summary line buried in the
    # log. The same guard already protects the balances scraper; this one was
    # missed, which is precisely why nobody would have noticed 70% of a batch
    # going missing.
    #
    # Errors here mean committee type, office and party did not refresh, and
    # those feed the candidate-to-committee matching that decides who appears
    # on the race map. Failing quietly there is expensive.
    attempted = scraped + errors
    if attempted and errors / attempted >= BATCH_FAILURE_ABORT:
        remaining_path.write_text("0")          # do not retrigger into a wall
        log.error("%d of %d filers failed (%.0f%%). Stopping rather than "
                  "retriggering — check whether ORESTAR is refusing this scraper.",
                  errors, attempted, 100 * errors / attempted)
        sys.exit(1)


def _save_cache(cache: dict):
    """Write cache to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


if __name__ == "__main__":
    main()
