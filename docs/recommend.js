/**
 * recommend.js — Donor Recommendation Engine
 *
 * Transparent, explainable, rule-based v1.
 *
 * Flow:
 *  1. User authenticates via Supabase
 *  2. User searches for a candidate committee
 *  3. Engine finds comparable fundraisers (same office/party/chamber)
 *  4. Finds donors who gave to comparable filers
 *  5. Scores each donor on explainable factors
 *  6. Outputs recommendations with target ask and explanation
 */

"use strict";

const DATA = "data/aggregated";
const _cbv = Date.now();

// ── Utility helpers (shared with main app) ─────────────────────────────────
function fmt$(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}
function fmtNum(n) { return Number(n).toLocaleString("en-US"); }
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
async function fetchJSON(path) {
  const sep = path.includes("?") ? "&" : "?";
  const resp = await fetch(`${path}${sep}_v=${_cbv}`);
  if (!resp.ok) throw new Error(`Failed to load ${path}: ${resp.status}`);
  return resp.json();
}

// ── Cycle helpers ──────────────────────────────────────────────────────────
// Two-calendar-year cycle: 2026 cycle = Jan 1 2025 through Dec 31 2026
function cycleYears(cycle) {
  return [cycle - 1, cycle];
}
function currentCycle() {
  const yr = new Date().getFullYear();
  return yr % 2 === 0 ? yr : yr + 1;
}
function cycleDateRange(cycle) {
  const [y1, y2] = cycleYears(cycle);
  return { start: `${y1}-01`, end: `${y2}-12` };
}

// ── Data cache ─────────────────────────────────────────────────────────────
let filerIndex = null;
let filerFuse = null;
const filerCache = {};
let leadershipRoles = {};   // filer_id or slug → role info (from Supabase)
let adminTags = {};         // entity_id → tags (from Supabase)

// ── Auth ───────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  const session = await requireAuth();
  if (session) {
    document.getElementById("user-info").textContent = session.user.email;
    await initApp();
  }

  document.getElementById("login-form-el").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = document.getElementById("login-error");
    errEl.hidden = true;
    try {
      await signIn(
        document.getElementById("login-email").value,
        document.getElementById("login-password").value,
      );
      window.location.reload();
    } catch (err) {
      errEl.textContent = err.message || "Sign-in failed";
      errEl.hidden = false;
    }
  });

  document.getElementById("sign-out-btn").addEventListener("click", signOut);
});

// ── App initialization ─────────────────────────────────────────────────────
async function initApp() {
  showStatus("Loading filer data…", "loading");

  filerIndex = await DL.getBlob("filer_index");
  filerFuse = new Fuse(filerIndex, {
    keys: [
      { name: "name",            weight: 2 },
      { name: "candidate_name",  weight: 1.5 },
      { name: "office_district", weight: 1 },
      { name: "filer_id",        weight: 0.5 },
    ],
    threshold: 0.3,
  });

  // Try loading leadership roles and admin tags from Supabase
  try {
    const sb = await getSupabase();
    const { data: roles } = await sb.from("leadership_roles").select("*").is("end_date", null);
    if (roles) {
      roles.forEach(r => {
        const key = r.filer_id || r.filer_name.toLowerCase();
        leadershipRoles[key] = r;
      });
    }
    const { data: tags } = await sb.from("admin_tags").select("*");
    if (tags) {
      tags.forEach(t => {
        if (!adminTags[t.entity_id]) adminTags[t.entity_id] = [];
        adminTags[t.entity_id].push(t);
      });
    }
  } catch (e) {
    console.warn("Could not load Supabase data (tables may not exist yet):", e.message);
  }

  hideStatus();
  initSearch();
  initCycleSelector();
}

// ── Filer search ───────────────────────────────────────────────────────────
function initSearch() {
  const input = document.getElementById("filer-search");
  const dropdown = document.getElementById("filer-results");
  let selectedFiler = null;

  input.addEventListener("input", () => {
    const q = input.value.trim();
    if (!q) { dropdown.hidden = true; return; }
    // Digits → exact/prefix filer-ID lookup ("4792" → Friends of Tina Kotek)
    let results;
    if (/^\d+$/.test(q)) {
      results = filerIndex
        .filter(f => String(f.filer_id).startsWith(q))
        .sort((a, b) => (String(a.filer_id) === q ? -1 : 0) - (String(b.filer_id) === q ? -1 : 0))
        .slice(0, 15);
    }
    if (!results || !results.length) {
      results = filerFuse.search(q).slice(0, 15).map(r => r.item);
    }
    if (!results.length) { dropdown.hidden = true; return; }
    dropdown.innerHTML = results.map((f, i) => {
      const metaParts = [fmt$(f.total_in) + " raised"];
      if (f.candidate_name) metaParts.push(f.candidate_name);
      if (f.party) metaParts.push(f.party);
      if (f.office_district || f.office) metaParts.push(f.office_district || f.office);
      else if (f.committee_type) metaParts.push(f.committee_type);
      if (f.election) metaParts.push(f.election);
      return `<li data-idx="${i}">
        <span>${esc(f.name)}</span>
        <span class="filer-meta">${metaParts.join(" · ")}</span>
      </li>`;
    }).join("");
    dropdown.hidden = false;
    dropdown._items = results;
  });

  dropdown.addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    const idx = parseInt(li.dataset.idx);
    const filer = dropdown._items[idx];
    if (filer) selectFiler(filer);
  });

  input.addEventListener("blur", () => setTimeout(() => dropdown.hidden = true, 150));

  window._selectFiler = selectFiler;
  window._getSelectedFiler = () => selectedFiler;

  async function selectFiler(filer) {
    selectedFiler = filer;
    input.value = filer.name;
    dropdown.hidden = true;

    showStatus("Loading filer details…", "loading");
    const profile = await loadFilerProfile(filer.slug);
    hideStatus();

    const info = document.getElementById("selected-filer-info");
    info.hidden = false;
    const detectedOffice = getOffice(filer);
    const detectedParty = getParty(filer);
    const officeBadge = filer.office_district || filer.office || (detectedOffice || "");
    const partyBadge = filer.party || (detectedParty === "D" ? "Democrat" : detectedParty === "R" ? "Republican" : "");
    const badges = [officeBadge, partyBadge].filter(Boolean).join(" · ");
    info.innerHTML = `
      <h3>${esc(filer.name)}</h3>
      ${badges ? `<div class="filer-badges">${esc(badges)}</div>` : ""}
      <div class="filer-stats">
        <div><span class="filer-stat-label">Cash Contributions</span><br><span class="filer-stat-value">${fmt$(profile.total_in)}</span></div>
        <div><span class="filer-stat-label">Total Expenditures</span><br><span class="filer-stat-value">${fmt$(profile.total_out)}</span></div>
        <div><span class="filer-stat-label">Cash on Hand</span><br><span class="filer-stat-value">${fmt$(profile.cash_on_hand)}</span></div>
        <div><span class="filer-stat-label">Transactions</span><br><span class="filer-stat-value">${fmtNum(profile.tran_count)}</span></div>
      </div>
    `;

    document.getElementById("cycle-section").hidden = false;
    document.getElementById("results-section").hidden = true;
  }
}

function initCycleSelector() {
  const sel = document.getElementById("cycle-select");
  const cur = currentCycle();
  for (let c = cur; c >= 2008; c -= 2) {
    sel.insertAdjacentHTML("beforeend", `<option value="${c}"${c === cur ? " selected" : ""}>${c - 1}–${c}</option>`);
  }

  document.getElementById("run-btn").addEventListener("click", runRecommendations);
}

async function loadFilerProfile(slug) {
  if (!filerCache[slug]) {
    filerCache[slug] = DL.getFilerDetail(slug);
  }
  return filerCache[slug];
}

// ── Status helpers ─────────────────────────────────────────────────────────
function showStatus(msg, type) {
  const el = document.getElementById("status-msg");
  el.textContent = msg;
  el.className = `status-msg ${type}`;
  el.hidden = false;
}
function hideStatus() {
  document.getElementById("status-msg").hidden = true;
}

// ═══════════════════════════════════════════════════════════════════════════
// RECOMMENDATION ENGINE
// ═══════════════════════════════════════════════════════════════════════════

// Donors to always exclude from recommendations (aggregated/non-individual entries)
const EXCLUDED_DONORS = new Set([
  "miscellaneous cash contributions $100 and under",
  "miscellaneous contributors",
  "miscellaneous cash contributions",
  "misc cash contributions $100 and under",
  "aggregate contributions $100 or less",
]);
function isDonorExcluded(name) {
  return EXCLUDED_DONORS.has(name.toLowerCase().trim());
}

async function runRecommendations() {
  const filer = window._getSelectedFiler();
  if (!filer) return;

  const cycle = parseInt(document.getElementById("cycle-select").value);
  const years = cycleYears(cycle).map(String);

  document.getElementById("run-btn").disabled = true;
  showStatus("Finding comparable fundraisers…", "loading");

  try {
    // 1. Load the target filer's profile
    const targetProfile = await loadFilerProfile(filer.slug);

    // 2. Find comparable filers
    const comparables = await findComparables(targetProfile, filer, cycle);
    showStatus(`Loading donor data for ${comparables.length} comparable filers…`, "loading");

    // 3. Load profiles for all comparables
    const compProfiles = await Promise.all(
      comparables.map(c => loadFilerProfile(c.slug))
    );

    // 4. Build repeat donor targets (existing donors to THIS filer)
    showStatus("Analyzing repeat donors…", "loading");
    targetProfile._leadershipTier = filer.leadership_tier || 0;
    const { targets: repeatTargets, notRecommended: repeatNotRec } = buildRepeatDonorTargets(targetProfile, comparables, compProfiles, years, cycle, seatCompetitiveness(filer));

    // 5. Build new donor scoring
    showStatus("Scoring new donors…", "loading");
    const { prospects: recommendations, notRecommended: prospectNotRec } = scoreDonors(targetProfile, comparables, compProfiles, years, cycle);
    const allNotRecommended = [...repeatNotRec, ...prospectNotRec];

    // 6. Display results
    displayResults(recommendations, repeatTargets, targetProfile, comparables, cycle, allNotRecommended);

  } catch (err) {
    showStatus(`Error: ${err.message}`, "error");
    console.error(err);
  } finally {
    document.getElementById("run-btn").disabled = false;
  }
}

// ── Office hierarchy for asymmetric comparability ─────────────────────────
// Legislative donors flow UP to statewide, but not down.
const LEGISLATIVE_OFFICES = new Set(["state_rep", "state_senate"]);
const STATEWIDE_OFFICES = new Set(["governor", "sos", "ag", "treasurer"]);

/**
 * Check if fOffice is comparable to targetOffice.
 * - state_rep ↔ state_senate: always comparable (bidirectional)
 * - legislative → statewide: comparable (donors flow up)
 * - statewide → legislative: NOT comparable (donors don't flow down)
 * - same office: always comparable
 */
function isOfficeComparable(targetOffice, fOffice) {
  if (!targetOffice || !fOffice) return false;
  if (targetOffice === fOffice) return true;
  // state_rep ↔ state_senate: bidirectional
  if (LEGISLATIVE_OFFICES.has(targetOffice) && LEGISLATIVE_OFFICES.has(fOffice)) return true;
  // Legislative → statewide: filer is legislative, target is statewide
  if (STATEWIDE_OFFICES.has(targetOffice) && LEGISLATIVE_OFFICES.has(fOffice)) return true;
  // Statewide ↔ statewide: comparable
  if (STATEWIDE_OFFICES.has(targetOffice) && STATEWIDE_OFFICES.has(fOffice)) return true;
  return false;
}

// ── Leadership tier definitions ───────────────────────────────────────────
// Tier 1: Speaker of the House, President of the Senate
// Tier 2: Majority Leaders, Ways & Means Co-Chairs
// Tier 3: Chairs, Pro Tems, Whips, other leadership positions
// Tier 0: Not in leadership
//
// Leadership tiers influence comparable-filer matching (affinity scoring),
// NOT individual donor ask amounts — comparable uplift handles that naturally.

// ── Step 2: Find comparable fundraisers ────────────────────────────────────
// ── Seat competitiveness ─────────────────────────────────────────────────────
//
// How close a seat is shapes what donors give: a swing-seat candidate can ask
// for more than someone in a safe seat, and a safe seat's donor history is a
// poor template for a competitive one. Margins come from race_margins
// (general elections only — primaries are too variable to describe a seat).
//
// Only the CURRENT district era is used. Oregon redraws maps two years after
// each census (2012, 2022), so a pre-2022 margin describes a different
// electorate under the same district number.

const MARGIN_BANDS = [
  { band: "competitive", max: 10,       mult: 1.25, label: "competitive (<10 pt margin)" },
  { band: "lean",        max: 20,       mult: 1.05, label: "lean (10–20 pt margin)" },
  { band: "safe",        max: Infinity, mult: 0.85, label: "safe (>20 pt margin)" },
];
const UNOPPOSED = { band: "unopposed", mult: 0.75, label: "unopposed last cycle" };

let raceMarginIndex = null;   // "Office|District" -> {margin_pts, band, mult, label, year}

function bandFor(marginPts, unopposed) {
  if (unopposed) return UNOPPOSED;
  if (marginPts == null) return null;
  return MARGIN_BANDS.find(b => marginPts < b.max) || MARGIN_BANDS[MARGIN_BANDS.length - 1];
}

async function loadRaceMargins() {
  if (raceMarginIndex) return raceMarginIndex;
  raceMarginIndex = new Map();
  try {
    const sb = await getSupabase();
    const { data, error } = await sb
      .from("race_margins")
      .select("year, office_normalized, district, margin_pts, unopposed, era")
      .eq("era", "2022-")                       // current maps only
      .order("year", { ascending: false });
    if (error) throw new Error(error.message);
    for (const r of data || []) {
      const key = `${r.office_normalized}|${r.district || ""}`;
      if (raceMarginIndex.has(key)) continue;   // newest year wins
      const b = bandFor(r.margin_pts, r.unopposed);
      if (b) raceMarginIndex.set(key, { ...b, margin_pts: r.margin_pts, year: r.year });
    }
    console.log(`[recommend] loaded competitiveness for ${raceMarginIndex.size} seats`);
  } catch (e) {
    console.warn("[recommend] race margins unavailable — scaling disabled:", e.message);
  }
  return raceMarginIndex;
}

/** Competitiveness for a filer, from its office_district ("State Representative, 15th District"). */
function seatCompetitiveness(filer) {
  if (!raceMarginIndex || !filer) return null;
  const od = (filer.office_district || "").trim();
  if (!od) return null;
  const i = od.indexOf(",");
  if (i === -1) return null;
  return raceMarginIndex.get(`${od.slice(0, i).trim()}|${od.slice(i + 1).trim()}`) || null;
}

async function findComparables(targetProfile, targetFiler, cycle) {
  await loadRaceMargins();
  const targetSeat = seatCompetitiveness(targetFiler);
  if (targetSeat) {
    console.log(`[recommend] target seat: ${targetSeat.label} (${targetSeat.year})`);
  }
  const officeType = getOffice(targetFiler);
  const party = getParty(targetFiler);
  const chamber = getChamber(targetFiler);
  const targetTier = targetFiler.leadership_tier || 0;
  const isTargetLeadership = targetTier > 0;

  console.log(`[recommend] Target: office=${officeType}, party=${party}, chamber=${chamber}, leadership_tier=${targetTier}`);

  const scored = [];

  for (const f of filerIndex) {
    if (f.slug === targetFiler.slug) continue;

    // Must have some fundraising activity
    if (f.total_in < 100) continue;

    const fOffice = getOffice(f);
    const fParty = getParty(f);
    const fChamber = getChamber(f);
    const fTier = f.leadership_tier || 0;

    // Party filter: if target has a known party, SKIP filers from other parties.
    // PACs/committees without party affiliation are allowed through.
    if (party && fParty && fParty !== party) continue;

    let similarity = 0;

    // Office comparability (asymmetric: legislative → statewide but not reverse)
    if (officeType && fOffice) {
      if (officeType === fOffice) {
        similarity += 40;  // Exact same office
      } else if (isOfficeComparable(officeType, fOffice)) {
        similarity += 30;  // state_rep ↔ state_senate or legislative → statewide
      }
    }

    // Same party gets a bonus (already filtered opposite parties above)
    if (party && fParty === party) similarity += 15;

    // Similar fundraising magnitude
    const ratio = Math.min(f.total_in, targetFiler.total_in) /
                  Math.max(f.total_in, targetFiler.total_in, 1);
    similarity += ratio * 15;

    // Leadership-to-leadership affinity: heavy preference
    if (isTargetLeadership && fTier > 0) {
      // Both are leadership — strong bonus
      similarity += 25;
      // Extra bonus if same tier or adjacent tier
      if (fTier === targetTier) similarity += 10;
      else if (Math.abs(fTier - targetTier) === 1) similarity += 5;
    } else if (isTargetLeadership && fTier === 0) {
      // Target is leadership, comparable is not — no penalty.
      // Non-leadership filers with bigger donations are a strong signal
      // of donor capacity and should be included in comparables.
    } else if (!isTargetLeadership && fTier > 0) {
      // Target is NOT leadership but comparable IS — bigger penalty
      similarity -= 15;
    }

    // Seat competitiveness: prefer comparables from similarly-contested seats.
    // Without this, a safe-seat donor history can set the template for a swing
    // seat (and vice versa), which is exactly what skews the suggested ask.
    if (targetSeat) {
      const fSeat = seatCompetitiveness(f);
      if (fSeat) {
        if (fSeat.band === targetSeat.band) similarity += 12;
        else if ((fSeat.band === "competitive" && targetSeat.band === "safe") ||
                 (fSeat.band === "safe" && targetSeat.band === "competitive") ||
                 fSeat.band === "unopposed" || targetSeat.band === "unopposed") {
          similarity -= 8;                       // opposite ends of the scale
        }
      }
    }

    // Check admin tags for exclusions
    const fTags = adminTags[f.slug] || [];
    if (fTags.some(t => t.tag === "exclude")) continue;
    if (fTags.some(t => t.tag === "prolific") && !isTargetLeadership) {
      similarity -= 10;
    }

    if (similarity > 20) {
      scored.push({ ...f, similarity, officeType: fOffice, party: fParty, chamber: fChamber });
    }
  }

  // Sort by similarity descending, take top 50
  scored.sort((a, b) => b.similarity - a.similarity);
  return scored.slice(0, 50);
}

// ── Metadata helpers: use scraped ORESTAR data, fall back to name heuristics ─

/**
 * Normalize a scraped office string to a canonical office type.
 * ORESTAR gives us e.g. "State Representative" or "State Senator".
 */
function normalizeOffice(office) {
  if (!office) return null;
  const o = office.toLowerCase().trim();
  if (o.startsWith("state representative")) return "state_rep";
  if (o.startsWith("state senator") || o.startsWith("state senate")) return "state_senate";
  if (o === "governor") return "governor";
  if (o.includes("secretary of state")) return "sos";
  if (o.includes("attorney general")) return "ag";
  if (o.includes("treasurer")) return "treasurer";
  if (o.includes("commissioner")) return "commissioner";
  if (o.includes("county")) return "county";
  if (o.includes("city council") || o.includes("mayor")) return "city";
  if (o.includes("school") || o.includes("education")) return "school";
  if (o.includes("judge") || o.includes("justice")) return "judicial";
  return o; // Return as-is if no normalization matched
}

function getOffice(filer) {
  // 1. Scraped ORESTAR metadata (preferred)
  if (filer.office) return normalizeOffice(filer.office);
  // 2. Name-based fallback
  return detectOfficeFromName(filer.name || "");
}

function getParty(filer) {
  // 1. Scraped ORESTAR metadata (preferred)
  if (filer.party) {
    const p = filer.party.toLowerCase();
    if (p.startsWith("democrat")) return "D";
    if (p.startsWith("republican")) return "R";
    if (p.startsWith("independent") || p.startsWith("nonaffiliated")) return "I";
    return filer.party.charAt(0).toUpperCase();
  }
  // 2. Admin tags
  const tags = adminTags[filer.slug] || [];
  const partyTag = tags.find(t => t.tag === "party");
  if (partyTag) return partyTag.value;
  // 3. PAC nature may hint at party (e.g. "Supporting House Democratic Candidates")
  if (filer.nature) {
    const n = filer.nature.toLowerCase();
    if (n.includes("democrat")) return "D";
    if (n.includes("republican")) return "R";
  }
  // 4. Name-based fallback (rare)
  const name = (filer.name || "").toLowerCase();
  if (/\bdemocrat\b/.test(name)) return "D";
  if (/\brepublican\b/.test(name)) return "R";
  return null;
}

function getChamber(filer) {
  // Use office metadata first
  const office = getOffice(filer);
  if (office === "state_rep") return "house";
  if (office === "state_senate") return "senate";
  // Fallback to name
  const name = (filer.name || "").toLowerCase();
  if (/\b(house)\b/.test(name)) return "house";
  if (/\b(senate)\b/.test(name)) return "senate";
  return null;
}

// Legacy name-based detection (fallback when metadata not scraped)
function detectOfficeFromName(name) {
  const n = name.toLowerCase();
  if (/\b(state representative|state rep)\b/.test(n)) return "state_rep";
  if (/\b(state senator|state senate)\b/.test(n)) return "state_senate";
  if (/\bgovernor\b/.test(n)) return "governor";
  if (/\b(secretary of state)\b/.test(n)) return "sos";
  if (/\b(attorney general)\b/.test(n)) return "ag";
  if (/\b(treasurer)\b/.test(n)) return "treasurer";
  if (/\b(commissioner)\b/.test(n)) return "commissioner";
  if (/\b(county)\b/.test(n)) return "county";
  if (/\b(city council|mayor|city)\b/.test(n)) return "city";
  if (/\b(school|education)\b/.test(n)) return "school";
  if (/\b(judge|justice)\b/.test(n)) return "judicial";
  return null;
}

/**
 * Get all years a donor gave to any comparable filer (across ALL years, not just cycle).
 * Returns array of year numbers, e.g. [2020, 2022, 2024].
 */
function _getAllYearGifts(donorName, compProfiles, comparables) {
  const key = donorName.toLowerCase();
  const years = [];
  for (const profile of compProfiles) {
    const byYear = profile.top_donors_by_year || {};
    for (const [yr, donors] of Object.entries(byYear)) {
      if (donors.some(d => d.name.toLowerCase() === key)) {
        years.push(parseInt(yr));
      }
    }
  }
  return years;
}

// ── Step 4a: Donor Targets ────────────────────────────────────────────────
// For ANY donor who has ever given to THIS candidate, compute a fundraising
// target: ~5% increase from their last giving, adjusted upward if they gave
// more to comparable candidates.
function buildRepeatDonorTargets(targetProfile, comparables, compProfiles, years, cycle, targetSeat) {
  const byYear = targetProfile.top_donors_by_year || {};
  const allYears = Object.keys(byYear).map(Number).sort((a, b) => a - b);
  const cycleStart = cycle - 1;

  // Group years into 2-year election cycles
  function yearToCycle(yr) { return yr % 2 === 0 ? yr : yr + 1; }

  // Build per-donor cycle history: donor → { cycles: {cycle: amount}, totalGifts }
  const donorHistory = new Map();
  for (const [yrStr, donors] of Object.entries(byYear)) {
    const yr = parseInt(yrStr);
    const cy = yearToCycle(yr);
    for (const d of donors) {
      const key = d.name.toLowerCase();
      if (!donorHistory.has(key)) {
        donorHistory.set(key, { name: d.name, cycles: {}, totalGifts: 0 });
      }
      const entry = donorHistory.get(key);
      entry.cycles[cy] = (entry.cycles[cy] || 0) + d.total;
      entry.totalGifts++;
    }
  }

  // Get comparable giving for upside adjustment — track per-filer details
  // compDonorDetails: lowered name → [{ filer, maxCycleAmt }]
  const compDonorDetails = new Map();
  for (let i = 0; i < compProfiles.length; i++) {
    const profile = compProfiles[i];
    const comp = comparables[i];
    const compByYear = profile.top_donors_by_year || {};
    const compDonorCycles = new Map(); // donor → {cycle: amount}
    for (const [yrStr, donors] of Object.entries(compByYear)) {
      const yr = parseInt(yrStr);
      const cy = yearToCycle(yr);
      for (const d of donors) {
        const key = d.name.toLowerCase();
        if (!compDonorCycles.has(key)) compDonorCycles.set(key, {});
        const cyMap = compDonorCycles.get(key);
        cyMap[cy] = (cyMap[cy] || 0) + d.total;
      }
    }
    // Store per-filer max cycle amount for each donor
    const compIsLeadership = (comp.leadership_tier || 0) > 0;
    const compTier = comp.leadership_tier || 0;
    for (const [key, cyMap] of compDonorCycles) {
      const maxCy = Math.max(...Object.values(cyMap));
      if (!compDonorDetails.has(key)) compDonorDetails.set(key, []);
      compDonorDetails.get(key).push({ filer: comp.name, amount: maxCy, isLeadership: compIsLeadership, leadershipTier: compTier });
    }
  }

  // Build set of candidate committee names for exclusion (not PACs or other types)
  const candidateFilerNames = new Set();
  if (filerIndex) {
    for (const f of filerIndex) {
      if (f.committee_type === "Candidate Committee") {
        candidateFilerNames.add(f.name.toLowerCase());
      }
    }
  }

  const results = [];
  const notRecommended = [];

  for (const [key, donor] of donorHistory) {
    // Skip aggregated/non-individual entries
    if (isDonorExcluded(donor.name)) continue;

    // Skip candidate committees (e.g. "Kate Lieber for State Senate (20136)")
    const nameNoId = donor.name.replace(/\s*\(\d+\)\s*$/, "").toLowerCase();
    if (candidateFilerNames.has(key) || candidateFilerNames.has(nameNoId)) continue;

    const cycleNums = Object.keys(donor.cycles).map(Number).sort((a, b) => a - b);

    // Single-cycle donors must also have given to comparable candidates
    if (cycleNums.length < 2 && !compDonorDetails.has(key)) continue;

    const currentCycleAmt = donor.cycles[cycle] || 0;
    const prevCycles = cycleNums.filter(c => c < cycle);

    const prevCycle = cycle - 2; // Most recent previous cycle
    const gaveInPrevCycle = donor.cycles[prevCycle] != null && donor.cycles[prevCycle] > 0;

    // Determine last giving amount for target calculation
    let lastCycle, lastCycleAmt;
    if (prevCycles.length) {
      lastCycle = prevCycles[prevCycles.length - 1];
      lastCycleAmt = donor.cycles[lastCycle];
    } else {
      // Only gave in current cycle — use current giving as base
      lastCycle = cycle;
      lastCycleAmt = currentCycleAmt;
    }

    // Base target: 5% increase from last cycle (or current if no prior)
    let target = Math.round(lastCycleAmt * 1.05 * 100) / 100;

    // Same-tier leadership floor: if a same-tier leader received more from this
    // donor, use that as the starting point (not just a blend).
    const compGifts = compDonorDetails.get(key) || [];
    const targetTier = targetProfile._leadershipTier || 0;
    if (targetTier > 0) {
      const sameTierGifts = compGifts
        .filter(g => g.leadershipTier === targetTier)
        .sort((a, b) => b.amount - a.amount);
      if (sameTierGifts.length > 0) {
        // Outlier check within same-tier
        const stRef = (sameTierGifts.length >= 2 && sameTierGifts[0].amount > sameTierGifts[1].amount * 1.5)
          ? sameTierGifts[1].amount
          : sameTierGifts[0].amount;
        if (stRef > target) {
          target = Math.round(stRef * 100) / 100;
        }
      }
    }

    // Comparable upside adjustment: if they gave MORE to a comparable,
    // blend toward that amount — but scale weight INVERSELY with the gap.
    // A small gap (1.5x) means the comp amount is realistic for this donor;
    // a huge gap (10x+) means the relationship isn't there and we should
    // stay close to historical giving.
    // Discount single-filer outliers: if the max is >1.5x the second-highest,
    // it's an outlier — use the second-highest as the reference instead.
    const sortedAmts = compGifts.map(g => g.amount).sort((a, b) => b - a);
    const compRef = (sortedAmts.length >= 2 && sortedAmts[0] > sortedAmts[1] * 1.5)
      ? sortedAmts[1]
      : (sortedAmts[0] || 0);
    const refGift = compGifts.find(g => g.amount === compRef);
    const maxFromNonLeadership = refGift && !refGift.isLeadership;
    const hasUplift = compRef > target;
    let compWeight = 0;
    if (hasUplift) {
      // Scale blend weight INVERSELY with the gap ratio:
      // Small gap (< 2x) → 35% weight (realistic uplift)
      // Medium gap (2-4x) → 20% weight (stretch goal)
      // Large gap (4-8x) → 10% weight (aspirational, stay close to history)
      // Huge gap (8x+) → 5% weight (almost entirely history-based)
      const gapRatio = compRef / Math.max(target, 1);
      if (gapRatio >= 8) compWeight = 0.05;
      else if (gapRatio >= 4) compWeight = 0.10;
      else if (gapRatio >= 2) compWeight = 0.20;
      else compWeight = 0.35;
      // Non-leadership max gets +5% weight (stronger signal of donor capacity)
      if (maxFromNonLeadership) compWeight = Math.min(compWeight + 0.05, 0.40);
      target = Math.round((target * (1 - compWeight) + compRef * compWeight) * 100) / 100;
    }

    const remaining = Math.max(0, Math.round((target - currentCycleAmt) * 100) / 100);

    if (!gaveInPrevCycle && prevCycles.length > 0) {
      // Lapsed donor — didn't give in most recent previous cycle
      notRecommended.push({
        donor: donor.name,
        type: "Previous Donor",
        lastGave: `${prevCycles[prevCycles.length - 1] - 1}–${prevCycles[prevCycles.length - 1]}`,
        amount: lastCycleAmt,
        whyNotIncluded: `Did not give in most recent previous cycle (${prevCycle - 1}–${prevCycle})`,
      });
      continue; // Skip adding to results
    }

    // Build cycle history summary
    const historyParts = prevCycles.map(c => `${c - 1}–${c}: ${fmt$(donor.cycles[c])}`);
    const avgPrev = prevCycles.length
      ? prevCycles.reduce((s, c) => s + donor.cycles[c], 0) / prevCycles.length
      : currentCycleAmt;

    const factors = [];
    if (prevCycles.length) {
      factors.push(`${prevCycles.length} previous cycle${prevCycles.length > 1 ? "s" : ""}: ${historyParts.join(", ")}`);
      factors.push(`Last cycle (${lastCycle - 1}–${lastCycle}): ${fmt$(lastCycleAmt)} → target: ${fmt$(target)} (+5%${hasUplift ? " + comparable uplift" : ""})`);
    } else {
      factors.push(`Current cycle donor: ${fmt$(currentCycleAmt)} given so far`);
      factors.push(`Base target: ${fmt$(target)} (+5%${hasUplift ? " + comparable uplift" : ""})`);
    }

    // Show comparable uplift details
    if (hasUplift) {
      const upliftGifts = compGifts
        .filter(g => g.amount > lastCycleAmt * 1.05)
        .sort((a, b) => b.amount - a.amount);
      const pct = Math.round(compWeight * 100);
      const nlTag = maxFromNonLeadership ? " — non-leadership benchmark" : "";
      const outlierNote = (compRef < sortedAmts[0]) ? ` — top gift ${fmt$(sortedAmts[0])} discounted as outlier` : "";
      factors.push(`Comparable uplift (${pct}% weight, ref: ${fmt$(compRef)}${nlTag}${outlierNote}):`);
      upliftGifts.forEach(g => {
        const tag = g.isLeadership ? "" : " ★";
        factors.push(`  • ${g.filer}: ${fmt$(g.amount)}${tag}`);
      });
    } else if (compGifts.length > 0) {
      // Show top comparable gifts even without uplift for context
      const topGifts = [...compGifts].sort((a, b) => b.amount - a.amount).slice(0, 3);
      factors.push(`Top comparable gifts: ${topGifts.map(g => `${g.filer} (${fmt$(g.amount)})`).join(", ")}`);
    }

    if (currentCycleAmt > 0 && prevCycles.length > 0) {
      factors.push(`Already given this cycle: ${fmt$(currentCycleAmt)}`);
    }

    // Confidence based on consistency
    const consistency = prevCycles.length >= 3 ? "high" : prevCycles.length >= 1 ? "medium" : "low";

    results.push({
      donor: donor.name,
      prev_cycles: prevCycles.length,
      last_cycle_amt: lastCycleAmt,
      avg_prev: Math.round(avgPrev * 100) / 100,
      comp_max: compRef,
      target,
      current_cycle_amt: currentCycleAmt,
      remaining,
      consistency,
      history: historyParts,
      factors,
    });
  }

  // Filter out donors with target below $500
  const filtered = results.filter(r => r.target >= 500).sort((a, b) => b.target - a.target);
  return { targets: filtered, notRecommended };
}

// ── Step 4b: Score new donors ─────────────────────────────────────────────
function scoreDonors(targetProfile, comparables, compProfiles, years, cycle) {
  // Build a set of leadership filer slugs for quick lookup
  const leadershipSlugs = new Set(
    comparables.filter(c => c.leadership_tier > 0).map(c => c.slug)
  );

  // Build: donor name → { compGifts, totalToComps, distinctComps, leadershipComps }
  const donorMap = new Map(); // lowered name → data

  compProfiles.forEach((profile, idx) => {
    const comp = comparables[idx];
    const donors = mergeDonorsByYear(profile.top_donors_by_year || {}, years);

    donors.forEach(d => {
      const key = d.name.toLowerCase();
      if (!donorMap.has(key)) {
        donorMap.set(key, {
          name: d.name,
          compGifts: [],
          totalToComps: 0,
          distinctComps: 0,
          leadershipComps: 0,  // how many leadership members this donor gave to
        });
      }
      const entry = donorMap.get(key);
      entry.compGifts.push({
        filer: comp.name,
        amount: d.total,
        similarity: comp.similarity,
        isLeadership: (comp.leadership_tier || 0) > 0,
      });
      entry.totalToComps += d.total;
      entry.distinctComps = entry.compGifts.length;
      if ((comp.leadership_tier || 0) > 0) entry.leadershipComps++;
    });
  });

  // Get what each donor already gave to the target filer this cycle
  const targetDonors = mergeDonorsByYear(targetProfile.top_donors_by_year || {}, years);
  const targetDonorMap = new Map(targetDonors.map(d => [d.name.toLowerCase(), d.total]));

  // Build set of ALL donors who have ever given to this filer (any year)
  const allTargetDonors = new Set();
  for (const donors of Object.values(targetProfile.top_donors_by_year || {})) {
    for (const d of donors) allTargetDonors.add(d.name.toLowerCase());
  }

  // Build set of candidate committee names for exclusion (not PACs or other types)
  const candidateFilerNames = new Set();
  if (filerIndex) {
    for (const f of filerIndex) {
      if (f.committee_type === "Candidate Committee") {
        candidateFilerNames.add(f.name.toLowerCase());
      }
    }
  }

  // Score each donor
  const results = [];
  const notRecommended = [];

  for (const [key, donor] of donorMap) {
    // Skip aggregated/non-individual entries
    if (isDonorExcluded(donor.name)) continue;

    // Skip candidate committees (e.g. "Kate Lieber for State Senate (20136)")
    const nameNoId = donor.name.replace(/\s*\(\d+\)\s*$/, "").toLowerCase();
    if (candidateFilerNames.has(key) || candidateFilerNames.has(nameNoId)) continue;

    // Skip donors who have ever given to this filer — they belong in Donor Targets
    if (allTargetDonors.has(key)) continue;

    const alreadyGiven = targetDonorMap.get(key) || 0;

    // ── EXCLUSION: donors who gave to only 1 person or donated ≤2 times ──
    const allYearGifts = _getAllYearGifts(donor.name, compProfiles, comparables);
    const distinctYears = new Set(allYearGifts).size;
    const totalDonationInstances = allYearGifts.length; // times across all years × filers

    if (donor.distinctComps <= 1) {
      notRecommended.push({
        donor: donor.name,
        type: "Donor Prospect",
        lastGave: "",  // Could compute but not critical
        amount: donor.totalToComps,
        whyNotIncluded: "Gave only to one comparable filer across all cycles",
      });
      continue;
    }

    // Compute target ask from comparable gifts
    const compAmounts = donor.compGifts.map(g => g.amount).sort((a, b) => a - b);
    const median = percentile(compAmounts, 0.5);
    const p75 = percentile(compAmounts, 0.75);

    // Target = upper-median (between median and 75th) capped by donor's own max
    const maxGift = Math.max(...compAmounts);
    let targetAsk = Math.min(Math.round(((median + p75) / 2) * 100) / 100, maxGift);

    // Scale by how contested the seat is: closer races draw larger gifts, so a
    // safe-seat baseline understates a swing seat (and vice versa). Bounded
    // 0.75×–1.25× so it tilts the ask without inventing a number, and the
    // reason is surfaced in the explanation below rather than left implicit.
    const seat = targetSeat;
    if (seat) {
      targetAsk = Math.min(Math.round(targetAsk * seat.mult * 100) / 100, maxGift);
    }

    const remainingAsk = Math.max(0, targetAsk - alreadyGiven);

    // Comparable giving range
    const compMin = Math.min(...compAmounts);
    const compMax = maxGift;

    // ── Explainable score components ──────────────────────────────────
    let score = 0;
    const factors = [];

    // Say so when the seat moved the ask, so a scaled number is legible
    // rather than looking arbitrary.
    if (seat && seat.mult !== 1) {
      const dir = seat.mult > 1 ? "raised" : "lowered";
      factors.push(
        `Ask ${dir} ${Math.round(Math.abs(seat.mult - 1) * 100)}% — seat is ${seat.label}`
        + (seat.year ? ` (${seat.year})` : "")
      );
    }

    // Factor 1: Number of distinct comparable filers supported (0-35 pts)
    // STRONG preference for donors who gave to MULTIPLE candidates.
    const distinctPts = donor.distinctComps === 1
      ? 3
      : Math.min(donor.distinctComps * 7, 35);
    score += distinctPts;
    if (donor.distinctComps >= 3) {
      factors.push(`Gave to ${donor.distinctComps} similar candidates`);
    } else if (donor.distinctComps === 1) {
      factors.push(`Only gave to 1 comparable candidate`);
    }

    // Factor 2: Total amount to comparable filers (0-15 pts)
    const totalPts = Math.min(donor.totalToComps / 500, 15);
    score += totalPts;

    // Factor 3: Similarity-weighted giving (0-15 pts)
    const simWeighted = donor.compGifts.reduce((s, g) => s + g.amount * (g.similarity / 100), 0);
    const simPts = Math.min(simWeighted / 300, 15);
    score += simPts;

    // Factor 4: Gap between comparable giving and current giving (0-15 pts)
    const gap = targetAsk - alreadyGiven;
    const gapPts = gap > 0 ? Math.min(gap / 200, 15) : 0;
    score += gapPts;
    if (alreadyGiven > 0 && gap > 0) {
      factors.push(`Gave ${fmt$(alreadyGiven)} but target is ${fmt$(targetAsk)}`);
    } else if (alreadyGiven === 0) {
      factors.push(`Has not given to this filer yet`);
    }

    // Factor 5: Recency — reward current-cycle giving, penalize stale donors
    const mostRecentYear = allYearGifts.length ? Math.max(...allYearGifts) : 0;
    const currentYear = new Date().getFullYear();
    const yearsAgo = mostRecentYear ? (currentYear - mostRecentYear) : 99;

    if (yearsAgo <= 1) {
      score += 20;
      factors.push(`Active donor (last gave ${mostRecentYear})`);
    } else if (yearsAgo <= 3) {
      score += 10;
    } else if (yearsAgo > 5) {
      score -= 10;
      factors.push(`Last gave ${mostRecentYear} (${yearsAgo} years ago)`);
    }

    // Factor 6: Leadership donor bonus — gave to multiple leadership members
    if (donor.leadershipComps >= 2) {
      score += Math.min(donor.leadershipComps * 5, 20);
      factors.push(`Gave to ${donor.leadershipComps} leadership members`);
    }

    // Penalty: one-time donors (1 candidate, 1 year)
    if (donor.distinctComps === 1 && distinctYears <= 1) {
      score -= 5;
      factors.push(`One-time donor (1 candidate, 1 year)`);
    }

    // Compute distinct election cycles (2-year periods) from year-level data
    const giftCycles = new Set(allYearGifts.map(yr => yr % 2 === 0 ? yr : yr + 1));
    const numCycles = giftCycles.size;

    // Heavy penalty: single-cycle donors (all giving in one 2-year cycle)
    if (numCycles <= 1) {
      score -= 25;
      factors.push(`Single-cycle donor — all comparable giving in one election cycle`);
    }

    // Note how many cycles the donor has given in
    if (numCycles > 0) {
      const sortedCycles = [...giftCycles].sort((a, b) => a - b);
      const cycleLabels = sortedCycles.map(c => `${c - 1}–${c}`).join(", ");
      factors.push(`Active in ${numCycles} election cycle${numCycles !== 1 ? "s" : ""}: ${cycleLabels}`);
    }

    // Normalize to 0-100
    score = Math.max(0, Math.min(Math.round(score), 100));

    // Build explanation summary
    const topComps = donor.compGifts
      .sort((a, b) => b.amount - a.amount)
      .slice(0, 5);
    const whySummary = buildWhySummary(donor, alreadyGiven, targetAsk, topComps);

    results.push({
      donor: donor.name,
      score,
      already_given: alreadyGiven,
      target_ask: targetAsk,
      remaining_ask: remainingAsk,
      comp_min: compMin,
      comp_max: compMax,
      comp_range: `${fmt$(compMin)}–${fmt$(compMax)}`,
      distinct_comps: donor.distinctComps,
      total_to_comps: donor.totalToComps,
      leadership_comps: donor.leadershipComps,
      comp_gifts: donor.compGifts,
      why_summary: whySummary,
      factors,
    });
  }

  // Filter out prospects with target ask below $1,000
  const filtered = results.filter(r => r.target_ask >= 1000).sort((a, b) => b.score - a.score);
  return { prospects: filtered, notRecommended };
}

function mergeDonorsByYear(byYear, years) {
  const totalMap = new Map();
  const nameMap = new Map();
  years.forEach(yr => {
    (byYear[yr] || []).forEach(d => {
      const key = d.name.toLowerCase();
      totalMap.set(key, (totalMap.get(key) || 0) + d.total);
      if (!nameMap.has(key)) nameMap.set(key, d.name);
    });
  });
  return [...totalMap.entries()]
    .map(([key, total]) => ({ name: nameMap.get(key), total: Math.round(total * 100) / 100 }))
    .sort((a, b) => b.total - a.total);
}

function percentile(sorted, p) {
  if (!sorted.length) return 0;
  const idx = Math.max(0, Math.ceil(sorted.length * p) - 1);
  return sorted[idx];
}

function buildWhySummary(donor, alreadyGiven, targetAsk, topComps) {
  const parts = [];
  if (topComps.length >= 2) {
    const names = topComps.slice(0, 3).map(g => g.filer);
    const amtRange = `${fmt$(Math.min(...topComps.map(g => g.amount)))}–${fmt$(Math.max(...topComps.map(g => g.amount)))}`;
    parts.push(`Gave ${amtRange} to ${names.length} similar filers (${names.join(", ")})`);
  } else if (topComps.length === 1) {
    parts.push(`Gave ${fmt$(topComps[0].amount)} to ${topComps[0].filer}`);
  }
  if (alreadyGiven > 0) {
    const gap = targetAsk - alreadyGiven;
    if (gap > 0) {
      parts.push(`Already gave ${fmt$(alreadyGiven)}; remaining ask: ${fmt$(gap)}`);
    } else {
      parts.push(`Already gave ${fmt$(alreadyGiven)} (at or above target)`);
    }
  } else {
    parts.push(`Has not contributed this cycle`);
  }
  return parts.join(". ") + ".";
}

// ── Step 6: Display results ───────────────────────────────────────────────
function displayResults(recommendations, repeatTargets, targetProfile, comparables, cycle, allNotRecommended) {
  hideStatus();

  const section = document.getElementById("results-section");
  section.hidden = false;

  document.getElementById("results-title").textContent =
    `Fundraising Plan for ${targetProfile.name} (${cycle - 1}–${cycle} Cycle)`;

  // Compute cycle contributions from timeline
  const { start, end } = cycleDateRange(cycle);
  const cycleContributions = (targetProfile.timeline || [])
    .filter(t => t.month >= start && t.month <= end)
    .reduce((s, t) => s + (t.contributions || 0), 0);

  // Summary cards
  const repeatRemaining = repeatTargets.reduce((s, r) => s + r.remaining, 0);
  const newRemaining = recommendations.reduce((s, r) => s + r.remaining_ask, 0);
  const summaryEl = document.getElementById("results-summary");
  summaryEl.innerHTML = `
    <div class="summary-card sc-muted"><span class="sc-label">Cycle Contributions <span class="sc-help" title="Total cash contributions received by this committee during the selected election cycle.">?</span></span><br><span class="sc-value">${fmt$(cycleContributions)}</span></div>
    <div class="summary-card"><span class="sc-label">Donor Target Total <span class="sc-help" title="Sum of recommended ask amounts for all existing donors who gave in the most recent previous cycle.">?</span></span><br><span class="sc-value">${fmt$(repeatRemaining)}</span></div>
    <div class="summary-card"><span class="sc-label">New Prospect Target <span class="sc-help" title="Sum of recommended ask amounts for new donors identified from comparable filer giving patterns.">?</span></span><br><span class="sc-value">${fmt$(newRemaining)}</span></div>
    <div class="summary-card"><span class="sc-label">Comparable Filers <span class="sc-help" title="Number of similar candidates used as benchmarks for donor targeting and prospect identification.">?</span></span><br><span class="sc-value">${fmtNum(comparables.length)}</span></div>
    <div class="summary-card"><span class="sc-label">Total Fundraising Target <span class="sc-help" title="Combined target from existing donor asks plus new prospect asks. Represents the total recommended fundraising goal.">?</span></span><br><span class="sc-value">${fmt$(repeatRemaining + newRemaining)}</span></div>
  `;

  // Update tab badges with counts
  document.querySelectorAll(".tab-btn[data-tab='tab-repeat'] .tab-badge").forEach(el => el.textContent = repeatTargets.length);
  document.querySelectorAll(".tab-btn[data-tab='tab-prospects'] .tab-badge").forEach(el => el.textContent = recommendations.length);
  document.querySelectorAll(".tab-btn[data-tab='tab-not-recommended'] .tab-badge").forEach(el => el.textContent = (allNotRecommended || []).length);

  // Wire up tabs
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(btn.dataset.tab).classList.add("active");
    });
  });

  // Store for filtering/sorting/export
  window._recommendations = recommendations;
  window._repeatTargets = repeatTargets;
  window._targetProfile = targetProfile;
  window._comparables = comparables;
  window._cycle = cycle;
  window._allNotRecommended = allNotRecommended || [];

  // Render repeat donors section
  renderRepeatDonors(repeatTargets);

  // Render new donor recommendations table
  renderRecTable(recommendations);

  // Wire up search bars
  const repeatSearchEl = document.getElementById("repeat-search");
  const recSearchEl = document.getElementById("rec-search");
  repeatSearchEl.value = "";
  recSearchEl.value = "";
  repeatSearchEl.addEventListener("input", () => renderFilteredRepeat());
  recSearchEl.addEventListener("input", () => renderFilteredRec());

  // Render Not Recommended table
  window._notRecData = allNotRecommended || [];
  window._notRecSortCol = "amount";
  window._notRecSortDir = "desc";
  renderFilteredNotRec();

  // Wire up search for Not Recommended
  const notRecSearch = document.getElementById("not-rec-search");
  if (notRecSearch) {
    notRecSearch.value = "";
    notRecSearch.addEventListener("input", () => renderFilteredNotRec());
  }

  // Wire up sort headers for both tables (remove old listeners by re-cloning)
  function wireSortHeaders(tableId, sortColKey, sortDirKey, renderFn) {
    document.querySelectorAll(`#${tableId} th.sortable`).forEach(th => {
      const clone = th.cloneNode(true);
      th.parentNode.replaceChild(clone, th);
      clone.addEventListener("click", () => {
        const col = clone.dataset.col;
        const curDir = clone.classList.contains("sort-asc") ? "asc" : clone.classList.contains("sort-desc") ? "desc" : null;
        document.querySelectorAll(`#${tableId} th.sortable`).forEach(t => t.classList.remove("sort-asc", "sort-desc"));
        const newDir = curDir === "desc" ? "asc" : "desc";
        clone.classList.add("sort-" + newDir);
        window[sortColKey] = col;
        window[sortDirKey] = newDir;
        renderFn();
      });
    });
  }

  wireSortHeaders("repeat-table", "_repeatSortCol", "_repeatSortDir", renderFilteredRepeat);
  wireSortHeaders("rec-table", "_sortCol", "_sortDir", renderFilteredRec);
  wireSortHeaders("not-rec-table", "_notRecSortCol", "_notRecSortDir", renderFilteredNotRec);

  // Wire up export (re-clone to avoid duplicate listeners)
  for (const id of ["export-csv", "export-xlsx", "export-repeat-csv", "export-repeat-xlsx", "export-full-csv", "export-full-xlsx"]) {
    const btn = document.getElementById(id);
    if (!btn) continue;
    const clone = btn.cloneNode(true);
    btn.parentNode.replaceChild(clone, btn);
    const fmt = id.includes("csv") ? "csv" : "xlsx";
    const scope = id.includes("repeat") ? "repeat" : id.includes("full") ? "full" : "new";
    clone.addEventListener("click", () => exportData(fmt, scope));
  }
}

function renderRepeatDonors(repeatTargets) {
  const container = document.getElementById("repeat-donors-section");
  if (!container) return;

  // Show/hide container based on whether original data exists
  const allRepeat = window._repeatTargets || [];
  if (!allRepeat.length) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  // Store the currently-rendered rows so detail toggles reference the right data
  window._renderedRepeat = repeatTargets;

  const tbody = document.getElementById("repeat-tbody");

  if (!repeatTargets.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#718096;padding:24px">No donors match the current filters.</td></tr>';
    return;
  }

  tbody.innerHTML = repeatTargets.map((r, i) => {
    const consistencyBadge = r.consistency === "high"
      ? '<span class="consistency-badge high">High</span>'
      : r.consistency === "medium"
      ? '<span class="consistency-badge medium">Med</span>'
      : '<span class="consistency-badge low">Low</span>';

    return `
    <tr class="repeat-row" data-idx="${i}">
      <td>${i + 1}</td>
      <td>${esc(r.donor)}</td>
      <td class="num">${r.prev_cycles}</td>
      <td class="num">${fmt$(r.last_cycle_amt)}</td>
      <td class="num"><strong>${fmt$(r.target)}</strong></td>
      <td class="num">${fmt$(r.current_cycle_amt)}</td>
      <td class="num">${fmt$(r.remaining)}</td>
      <td>${consistencyBadge}</td>
      <td>
        <div class="why-text">${r.history.join(", ")}</div>
        <button class="why-toggle repeat-detail-toggle" data-ridx="${i}">Show details ▸</button>
      </td>
    </tr>`;
  }).join("");

  // Wire up detail toggles
  tbody.querySelectorAll(".repeat-detail-toggle").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = parseInt(btn.dataset.ridx);
      toggleRepeatDetail(idx, btn);
    });
  });
}

function toggleRepeatDetail(idx, btn) {
  const existing = document.querySelector(`.repeat-detail-row[data-for="${idx}"]`);
  if (existing) {
    existing.remove();
    btn.textContent = "Show details ▸";
    return;
  }

  const r = (window._renderedRepeat || window._repeatTargets || [])[idx];
  if (!r) return;

  btn.textContent = "Hide details ▾";
  const tr = btn.closest("tr");
  const detailRow = document.createElement("tr");
  detailRow.className = "repeat-detail-row detail-row";
  detailRow.dataset.for = idx;

  detailRow.innerHTML = `<td colspan="9"><div class="detail-content">
    <h4>Target Calculation</h4>
    <ul style="margin:0 0 0 16px;font-size:0.83rem;color:#4a5568">
      ${r.factors.map(f => {
        const text = esc(f);
        if (text.startsWith("  •")) {
          return `<li style="margin-left:16px;list-style:none">${text.trim()}</li>`;
        }
        return `<li>${text}</li>`;
      }).join("")}
    </ul>
  </div></td>`;

  tr.after(detailRow);
}

// ── Search filtering ──────────────────────────────────────────────────────
function sortRows(rows, col, dir) {
  return [...rows].sort((a, b) => {
    let va = a[col], vb = b[col];
    if (typeof va === "string") { va = va.toLowerCase(); vb = (vb || "").toLowerCase(); }
    if (va < vb) return dir === "asc" ? -1 : 1;
    if (va > vb) return dir === "asc" ? 1 : -1;
    return 0;
  });
}

function renderFilteredRepeat() {
  const q = (document.getElementById("repeat-search").value || "").trim().toLowerCase();
  let rows = window._repeatTargets || [];
  if (q) rows = rows.filter(r => r.donor.toLowerCase().includes(q));

  const col = window._repeatSortCol || "target";
  const dir = window._repeatSortDir || "desc";
  rows = sortRows(rows, col, dir);

  renderRepeatDonors(rows);
}

function renderFilteredRec() {
  const q = (document.getElementById("rec-search").value || "").trim().toLowerCase();
  let rows = window._recommendations || [];
  if (q) rows = rows.filter(r => r.donor.toLowerCase().includes(q));

  const col = window._sortCol || "score";
  const dir = window._sortDir || "desc";
  rows = sortRows(rows, col, dir);

  renderRecTable(rows);
}

function renderFilteredNotRec() {
  const q = (document.getElementById("not-rec-search")?.value || "").trim().toLowerCase();
  let rows = window._notRecData || [];
  if (q) rows = rows.filter(r => r.donor.toLowerCase().includes(q) || r.type.toLowerCase().includes(q));

  const col = window._notRecSortCol || "amount";
  const dir = window._notRecSortDir || "desc";
  rows = sortRows(rows, col, dir);

  const tbody = document.getElementById("not-rec-tbody");
  if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#718096;padding:24px">No excluded donors.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((r, i) => `<tr>
    <td>${i + 1}</td>
    <td>${esc(r.donor)}</td>
    <td><span class="not-rec-type">${esc(r.type)}</span></td>
    <td>${esc(r.lastGave)}</td>
    <td class="num">${fmt$(r.amount)}</td>
    <td class="not-rec-reason">${esc(r.whyNotIncluded)}</td>
  </tr>`).join("");
}

function renderRecTable(rows) {
  // Store the currently-rendered rows so detail toggles reference the right data
  window._renderedRecs = rows;

  const tbody = document.getElementById("rec-tbody");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#718096;padding:24px">No recommendations found.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map((r, i) => `
    <tr class="rec-row" data-idx="${i}">
      <td>${i + 1}</td>
      <td>${esc(r.donor)}</td>
      <td class="num">
        <div class="score-bar">
          <div class="score-bar-track"><div class="score-bar-fill" style="width:${r.score}%"></div></div>
          <span>${r.score}</span>
        </div>
      </td>
      <td class="num">${fmt$(r.already_given)}</td>
      <td class="num">${fmt$(r.target_ask)}</td>
      <td class="num">${fmt$(r.remaining_ask)}</td>
      <td class="num" style="font-size:0.8rem">${r.comp_range}</td>
      <td>
        <div class="why-text">${esc(r.why_summary)}</div>
        <button class="why-toggle" data-donor-idx="${i}">Show details ▸</button>
      </td>
    </tr>
  `).join("");

  // Attach detail toggle listeners
  tbody.querySelectorAll(".why-toggle").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = parseInt(btn.dataset.donorIdx);
      toggleDetail(idx, btn);
    });
  });
}

function toggleDetail(idx, btn) {
  const existing = document.querySelector(`.detail-row[data-for="${idx}"]`);
  if (existing) {
    existing.remove();
    btn.textContent = "Show details ▸";
    return;
  }

  const r = (window._renderedRecs || window._recommendations || [])[idx];
  if (!r) return;

  btn.textContent = "Hide details ▾";

  const tr = btn.closest("tr");
  const detailRow = document.createElement("tr");
  detailRow.className = "detail-row";
  detailRow.dataset.for = idx;

  const topGifts = r.comp_gifts.sort((a, b) => b.amount - a.amount).slice(0, 10);

  detailRow.innerHTML = `<td colspan="8"><div class="detail-content">
    <h4>Comparable Filer Gifts (${r.distinct_comps} filers, ${fmt$(r.total_to_comps)} total)</h4>
    ${topGifts.map(g => `
      <div class="comp-filer-row">
        <span class="comp-filer-name">${esc(g.filer)}</span>
        <span class="comp-filer-amt">${fmt$(g.amount)}</span>
      </div>
    `).join("")}
    <h4>Scoring Factors</h4>
    <ul style="margin:0 0 0 16px;font-size:0.83rem;color:#4a5568">
      ${r.factors.map(f => `<li>${esc(f)}</li>`).join("")}
      <li>Distinct comparable filers: ${r.distinct_comps}</li>
      <li>Total to comparables: ${fmt$(r.total_to_comps)}</li>
    </ul>
  </div></td>`;

  tr.after(detailRow);
}

// ── Export ─────────────────────────────────────────────────────────────────
function exportData(format, scope = "new") {
  const recs = window._recommendations || [];
  const repeats = window._repeatTargets || [];
  const target = window._targetProfile;
  const cycle = window._cycle;
  const cycleLabel = cycle ? `${cycle - 1}-${cycle}` : "";

  const repeatRows = repeats.map(r => ({
    "Type": "Donor Target",
    "Donor Name": r.donor,
    "Filer": target ? target.name : "",
    "Cycle": cycleLabel,
    "Score": "",
    "Tier": r.consistency === "high" ? "High" : r.consistency === "medium" ? "Med" : "Low",
    "Previous Cycles": r.prev_cycles,
    "Last Cycle Amount": r.last_cycle_amt,
    "Comparable Max": r.comp_max || "",
    "Target Ask": r.target,
    "Already Given This Cycle": r.current_cycle_amt,
    "Remaining Ask": r.remaining,
    "History": r.history.join("; "),
    "Notes": r.factors.join("; "),
  }));

  const newRows = recs.map(r => ({
    "Type": "New Prospect",
    "Donor Name": r.donor,
    "Filer": target ? target.name : "",
    "Cycle": cycleLabel,
    "Score": r.score,
    "Previous Cycles": "",
    "Last Cycle Amount": "",
    "Comparable Max": r.comp_max,
    "Target Ask": r.target_ask,
    "Already Given This Cycle": r.already_given,
    "Remaining Ask": r.remaining_ask,
    "History": r.comp_gifts.map(g => g.filer).join("; "),
    "Notes": r.why_summary,
  }));

  let exportRows;
  let fileLabel;
  if (scope === "repeat") {
    exportRows = repeatRows;
    fileLabel = "donor_targets";
  } else if (scope === "new") {
    exportRows = newRows;
    fileLabel = "new_prospects";
  } else {
    // full: repeat donors first, then new prospects
    exportRows = [...repeatRows, ...newRows];
    fileLabel = "full_plan";
  }

  if (!exportRows.length) {
    alert("No data to export.");
    return;
  }

  if (format === "csv") {
    const headers = Object.keys(exportRows[0] || {});
    const csv = [
      headers.join(","),
      ...exportRows.map(row =>
        headers.map(h => {
          const v = String(row[h] ?? "");
          return v.includes(",") || v.includes('"') || v.includes("\n")
            ? `"${v.replace(/"/g, '""')}"`
            : v;
        }).join(",")
      ),
    ].join("\n");

    downloadFile(csv, `${fileLabel}_${target.slug}_${cycle}.csv`, "text/csv");

  } else if (format === "xlsx") {
    if (typeof XLSX === "undefined") {
      alert("Excel export library not loaded. Please try CSV instead.");
      return;
    }
    const wb = XLSX.utils.book_new();
    if (scope === "full") {
      // Separate sheets for repeat and new
      if (repeatRows.length) {
        const ws1 = XLSX.utils.json_to_sheet(repeatRows);
        XLSX.utils.book_append_sheet(wb, ws1, "Donor Targets");
      }
      if (newRows.length) {
        const ws2 = XLSX.utils.json_to_sheet(newRows);
        XLSX.utils.book_append_sheet(wb, ws2, "New Prospects");
      }
    } else {
      const ws = XLSX.utils.json_to_sheet(exportRows);
      XLSX.utils.book_append_sheet(wb, ws, scope === "repeat" ? "Donor Targets" : "New Prospects");
    }
    XLSX.writeFile(wb, `${fileLabel}_${target.slug}_${cycle}.xlsx`);
  }
}

function downloadFile(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
