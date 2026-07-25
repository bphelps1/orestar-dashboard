"""
fetch_candidates.py — scrape ORESTAR's Candidate Filing Search for the ballot roster.

Why this exists: committees self-report "Election/Office" on their Statement of
Organization and update it inconsistently, so a committee can sit in a current
race while still claiming a past election (Emerson Levy's committee still says
"2024 General Election" while she is nominated for HD 53 in 2026). Driving the
Races map off that field silently drops candidates. The candidate *filing*
record is authoritative for who is actually on the ballot.

Notes on the site:
  • CFSearchPage.do is behind F5 bot defense. Plain HTTP and HEADLESS Chromium
    are both blocked ("Please Contact Us"); a HEADED browser works. CI runs this
    under `xvfb-run`, exactly like fetch.py.
  • cfElection / cfOfficeGrp are AJAX-populated, so selections must be made in
    order (year -> election -> office) with waits between.
  • The named submit button is replaced during those re-renders, so we submit
    the form directly.
  • One query per chamber returns every district — no need to iterate districts.

Usage:
    python scraper/fetch_candidates.py                # current year, auto-advance
    python scraper/fetch_candidates.py --year 2026
    python scraper/fetch_candidates.py --election-id 1451   # force (e.g. primary)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "candidate_filings.json"

SEARCH_URL = "https://secure.sos.state.or.us/orestar/CFSearchPage.do"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PAGE_RENDER_WAIT = 7_000     # ms — F5 challenge + form JS
AJAX_WAIT = 3_000            # ms — dependent dropdown population
RESULTS_WAIT = 10_000        # ms — search submit

CHAMBERS = {"SR": "house", "SS": "senate"}
FILING_NOMINATED = "NOM"
DISTRICT_PAT = re.compile(r"(\d+)\w*\s+District", re.I)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _open(playwright):
    """Headed browser — headless is blocked by F5."""
    browser = playwright.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    page = browser.new_context(user_agent=USER_AGENT, no_viewport=True,
                               accept_downloads=True).new_page()
    page.goto(SEARCH_URL, timeout=90_000)
    page.wait_for_timeout(PAGE_RENDER_WAIT)
    if page.locator("select[name=cfyearActive]").count() == 0:
        raise RuntimeError(
            f"Filing search form did not load (title={page.title()!r}). "
            "F5 likely blocked the browser — headless mode is always blocked; "
            "CI must run this under xvfb-run."
        )
    return browser, page


def elections_for_year(page, year: str) -> list[tuple[str, str]]:
    """[(election_id, label)] for a year, newest-looking first (General before Primary)."""
    page.select_option("select[name=cfyearActive]", year)
    page.wait_for_timeout(AJAX_WAIT)
    opts = page.evaluate(
        """() => [...document.querySelector('select[name=cfElection]').options]
             .map(o => [o.value, o.text.trim()]).filter(([v]) => v)"""
    )
    # General before Primary so auto-advance prefers the later election
    opts.sort(key=lambda kv: (0 if "general" in kv[1].lower() else 1, kv[1]))
    return [(v, t) for v, t in opts]


def _download_export(page) -> list[list[str]]:
    """Click Export and parse the workbook — returns rows as lists of strings.

    The results page caps the HTML table at 50 rows per page; the export has no
    such cap, so this is the only complete source.
    """
    import pandas as pd

    link = page.locator("a:has-text('Export')").first
    if link.count() == 0:
        log.warning("No Export link on the results page")
        return []
    import tempfile
    with page.expect_download(timeout=60_000) as dl_info:
        link.click()
    # Playwright's temp file has no extension, so pandas can't infer an engine.
    # ORESTAR serves legacy BIFF .xls here (OLE2), which needs xlrd.
    dest = Path(tempfile.gettempdir()) / "orestar_cf_export.xls"
    dl_info.value.save_as(dest)

    try:
        df = pd.read_excel(dest, dtype=str)
    except Exception as e:
        log.debug("read_excel failed (%s) — trying HTML table", e)
        try:
            tables = pd.read_html(dest)
        except Exception:
            tables = []
        if not tables:
            log.warning("Could not parse the export workbook")
            return []
        df = tables[0].astype(str)

    df = df.fillna("")
    # Find the header row (export sometimes carries title rows above it)
    cols = [str(c).strip().lower() for c in df.columns]
    if "ballot name" not in " ".join(cols):
        for i in range(min(6, len(df))):
            if any("ballot name" in str(v).strip().lower() for v in df.iloc[i]):
                df.columns = [str(v).strip() for v in df.iloc[i]]
                df = df.iloc[i + 1:]
                break
    return df


def scrape_chamber(page, year: str, election_id: str, office: str) -> list[dict]:
    """Ballot candidates for one chamber, for a given election.

    Deliberately does NOT filter by filing type at the query level. "Nominated"
    only exists after a primary is decided — 2026 Primary filings are all method
    "Fee" — so filtering on NOM would return an empty roster for the whole
    pre-primary phase. Instead we take everything and let the caller prefer
    nominees when they exist.
    """
    page.goto(SEARCH_URL, timeout=90_000)          # fresh form per query
    page.wait_for_timeout(PAGE_RENDER_WAIT)
    page.select_option("select[name=cfyearActive]", year)
    page.wait_for_timeout(AJAX_WAIT)
    page.select_option("select[name=cfElection]", election_id)
    page.wait_for_timeout(AJAX_WAIT)
    page.select_option("select[name=cfOffice]", office)
    page.wait_for_timeout(AJAX_WAIT)
    # The named submit button is swapped out by the AJAX re-renders.
    page.evaluate("() => document.forms[0].submit()")
    page.wait_for_timeout(RESULTS_WAIT)

    # The HTML table caps at "Maximum of 50 records ... in a page", which
    # silently truncated the roster (Emerson Levy and 8 others were lost). The
    # Excel export returns the complete set, the same way fetch.py pulls
    # transactions.
    df = _download_export(page)
    if df is None or not len(df):
        return []

    def col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_name = col("Cand Ballot Name Txt", "Ballot Name")
    c_off = col("Candidate Office", "Office")
    c_type = col("Filetype Descr", "Filing Method")
    c_party = col("Party Descr", "Party")
    c_filed = col("Filed Date")
    c_qlf = col("Qlf Ind", "Qualified")
    if not (c_name and c_off):
        log.warning("Export missing expected columns: %s", list(df.columns)[:8])
        return []

    # A candidate can be cross-nominated (Emerson Levy appears as both
    # Nominated/Democrat and Minor Party/Independent). Collapse to one entry
    # per person per district, keeping every party they appear under.
    merged: dict[tuple, dict] = {}
    for _, r in df.iterrows():
        ballot = str(r[c_name]).strip()
        office_txt = str(r[c_off]).strip()
        ftype = str(r[c_type]).strip() if c_type else ""
        if not ballot or not office_txt:
            continue
        if ftype.lower() == "write in":     # not on the printed ballot
            continue
        m = DISTRICT_PAT.search(office_txt)
        if not m:
            log.warning("No district parsed from %r (%s) — skipped", office_txt, ballot)
            continue
        key = (ballot.lower(), int(m.group(1)))
        party = str(r[c_party]).strip() if c_party else ""
        if key in merged:
            if party and party not in merged[key]["parties"]:
                merged[key]["parties"].append(party)
            continue
        merged[key] = {
            "ballot_name": ballot,
            "party": party,
            "parties": [party] if party else [],
            "chamber": CHAMBERS[office],
            "district": int(m.group(1)),
            "office_district": office_txt,   # matches filer_index.office_district
            "election": str(r[col("Election Txt") or c_off]).strip(),
            "filing_method": ftype,
            "filing_date": str(r[c_filed]).strip() if c_filed else "",
            "qualified": str(r[c_qlf]).strip() if c_qlf else "",
        }
    return list(merged.values())


def scrape(year: str | None = None, election_id: str | None = None) -> dict:
    year = year or str(datetime.now().year)
    with sync_playwright() as pw:
        browser, page = _open(pw)
        try:
            elections = elections_for_year(page, year)
            if not elections:
                raise RuntimeError(f"No elections listed for {year}")
            log.info("Elections for %s: %s", year, [t for _, t in elections])

            # Auto-advance: prefer the General; fall back to the Primary if it
            # has no nominees yet (nominations post after the primary).
            tries = ([(election_id, next((t for v, t in elections if v == election_id),
                                         election_id))]
                     if election_id else elections)

            for eid, label in tries:
                log.info("Trying election %s (%s)", label, eid)
                candidates, per_chamber = [], {}
                for office in CHAMBERS:
                    rows = scrape_chamber(page, year, eid, office)
                    # Everything filed for this election is on its ballot:
                    # "Nominated" (major-party nominees) AND "Minor Party".
                    # Filtering to Nominated alone would drop minor-party
                    # candidates; write-ins are already excluded in parsing.
                    per_chamber[office] = len(rows)
                    candidates.extend(rows)
                    types = Counter(r["filing_method"] for r in rows)
                    log.info("  %s: %d candidates %s", office, len(rows), dict(types))
                if candidates:
                    return {
                        "election": label,
                        "election_id": eid,
                        "year": year,
                        "scraped": datetime.now(timezone.utc)
                                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "counts": per_chamber,
                        "candidates": candidates,
                    }
                log.warning("No nominated candidates for %s — trying next election", label)
            raise RuntimeError(
                f"No nominated candidates found for any {year} election. "
                "Refusing to write an empty roster (it would blank the Races map)."
            )
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year")
    ap.add_argument("--election-id", help="force a specific election (skips auto-advance)")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    data = scrape(args.year, args.election_id)

    # Guard: a chamber returning zero is a scrape failure, not an empty ballot.
    missing = [o for o, n in data["counts"].items() if n == 0]
    if missing:
        log.error("Chamber(s) returned zero candidates: %s — not writing output", missing)
        return 1

    Path(args.out).write_text(json.dumps(data, indent=2))
    log.info("Wrote %s — %s, %d candidates (%s)", args.out, data["election"],
             len(data["candidates"]), data["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
