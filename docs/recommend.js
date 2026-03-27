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

  filerIndex = await fetchJSON(`${DATA}/filer_index.json`);
  filerFuse = new Fuse(filerIndex, {
    keys: ["name"],
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
    const results = filerFuse.search(q).slice(0, 15).map(r => r.item);
    if (!results.length) { dropdown.hidden = true; return; }
    dropdown.innerHTML = results.map((f, i) => {
      const metaParts = [fmt$(f.total_in) + " raised"];
      if (f.party) metaParts.push(f.party);
      if (f.office) metaParts.push(f.office);
      else if (f.committee_type) metaParts.push(f.committee_type);
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
    filerCache[slug] = fetchJSON(`${DATA}/filers/${slug}.json`);
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

    // 4. Build donor scoring
    showStatus("Scoring donors…", "loading");
    const recommendations = scoreDonors(targetProfile, comparables, compProfiles, years, cycle);

    // 5. Display results
    displayResults(recommendations, targetProfile, comparables, cycle);

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

// ── Step 2: Find comparable fundraisers ────────────────────────────────────
async function findComparables(targetProfile, targetFiler, cycle) {
  // Use scraped ORESTAR metadata (party, office) from the filer index,
  // falling back to name-based heuristics if metadata not yet scraped.
  const officeType = getOffice(targetFiler);
  const party = getParty(targetFiler);
  const chamber = getChamber(targetFiler);

  console.log(`[recommend] Target: office=${officeType}, party=${party}, chamber=${chamber}`);

  // Check if this filer has leadership tags
  const targetLeadership = leadershipRoles[targetFiler.filer_id] ||
    leadershipRoles[targetFiler.slug] || null;

  const scored = [];

  for (const f of filerIndex) {
    if (f.slug === targetFiler.slug) continue;

    // Must have some fundraising activity
    if (f.total_in < 100) continue;

    const fOffice = getOffice(f);
    const fParty = getParty(f);
    const fChamber = getChamber(f);

    // Party filter: if target has a known party, SKIP filers from other parties.
    // PACs/committees without party affiliation are allowed through.
    if (party && fParty && fParty !== party) continue;

    let similarity = 0;

    // Office comparability (asymmetric: legislative → statewide but not reverse)
    if (officeType && fOffice) {
      if (officeType === fOffice) {
        similarity += 40;  // Exact same office
      } else if (isOfficeComparable(officeType, fOffice)) {
        // state_rep ↔ state_senate or legislative → statewide
        similarity += 30;  // Strong but slightly less than exact match
      }
      // else: not comparable, gets 0 office points
    }

    // Same party gets a bonus (already filtered opposite parties above)
    if (party && fParty === party) similarity += 15;
    // Similar fundraising magnitude (within 3x)
    const ratio = Math.min(f.total_in, targetFiler.total_in) /
                  Math.max(f.total_in, targetFiler.total_in, 1);
    similarity += ratio * 15;

    // Penalize leadership committees when target is not leadership
    const fLeadership = leadershipRoles[f.filer_id] || leadershipRoles[f.slug] || null;
    if (fLeadership && !targetLeadership) {
      similarity -= 20;  // Leadership PACs are not typical peers
    }

    // Check admin tags for exclusions
    const fTags = adminTags[f.slug] || [];
    if (fTags.some(t => t.tag === "exclude")) continue;
    if (fTags.some(t => t.tag === "prolific") && !targetLeadership) {
      similarity -= 10;
    }

    if (similarity > 20) {
      scored.push({ ...f, similarity, officeType: fOffice, party: fParty, chamber: fChamber });
    }
  }

  // Sort by similarity descending, take top 50 (increased from 30 to
  // accommodate the broader legislative pool)
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

// ── Step 4: Score donors ──────────────────────────────────────────────────
function scoreDonors(targetProfile, comparables, compProfiles, years, cycle) {
  // Build: donor name → { compFilers: [{name, amount}], totalToComps, distinctComps }
  const donorMap = new Map(); // lowered name → data

  compProfiles.forEach((profile, idx) => {
    const comp = comparables[idx];
    const donors = mergeDonorsByYear(profile.top_donors_by_year || {}, years);

    donors.forEach(d => {
      const key = d.name.toLowerCase();
      if (!donorMap.has(key)) {
        donorMap.set(key, {
          name: d.name,
          compGifts: [],          // {filer, amount, similarity}
          totalToComps: 0,
          distinctComps: 0,
        });
      }
      const entry = donorMap.get(key);
      entry.compGifts.push({
        filer: comp.name,
        amount: d.total,
        similarity: comp.similarity,
      });
      entry.totalToComps += d.total;
      entry.distinctComps = entry.compGifts.length;
    });
  });

  // Get what each donor already gave to the target filer this cycle
  const targetDonors = mergeDonorsByYear(targetProfile.top_donors_by_year || {}, years);
  const targetDonorMap = new Map(targetDonors.map(d => [d.name.toLowerCase(), d.total]));

  // Score each donor
  const results = [];

  for (const [key, donor] of donorMap) {
    const alreadyGiven = targetDonorMap.get(key) || 0;

    // Compute target ask from comparable gifts
    const compAmounts = donor.compGifts.map(g => g.amount).sort((a, b) => a - b);
    const median = percentile(compAmounts, 0.5);
    const p75 = percentile(compAmounts, 0.75);

    // Target = upper-median (between median and 75th) capped by donor's own max
    const maxGift = Math.max(...compAmounts);
    const targetAsk = Math.min(Math.round(((median + p75) / 2) * 100) / 100, maxGift);
    const remainingAsk = Math.max(0, targetAsk - alreadyGiven);

    // Comparable giving range
    const compMin = Math.min(...compAmounts);
    const compMax = maxGift;

    // ── Explainable score components ──────────────────────────────────
    let score = 0;
    const factors = [];

    // Factor 1: Number of distinct comparable filers supported (0-35 pts)
    // STRONG preference for donors who gave to MULTIPLE candidates.
    // 1 filer = 3 pts, 2 = 10, 3 = 18, 4 = 24, 5+ = 30-35
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

    // Factor 5: Recency — reward current-cycle giving, penalize stale donors (−10 to +20 pts)
    // We check if donor gave to comparables during the selected cycle years.
    // Since mergeDonorsByYear already filters to cycle years, donors in the
    // list are cycle-relevant. But we also check all-time giving years to
    // detect one-time donors from years ago.
    const cycleYrs = cycleYears(cycle).map(String);
    const allYearGifts = _getAllYearGifts(donor.name, compProfiles, comparables);
    const mostRecentYear = allYearGifts.length ? Math.max(...allYearGifts) : 0;
    const currentYear = new Date().getFullYear();
    const yearsAgo = mostRecentYear ? (currentYear - mostRecentYear) : 99;

    if (yearsAgo <= 1) {
      // Gave this year or last year — strong recency
      score += 20;
      factors.push(`Active donor (last gave ${mostRecentYear})`);
    } else if (yearsAgo <= 3) {
      // Gave within recent cycle
      score += 10;
    } else if (yearsAgo > 5) {
      // Stale donor — penalize
      score -= 10;
      factors.push(`Last gave ${mostRecentYear} (${yearsAgo} years ago)`);
    }

    // Penalty: one-time donors who gave to only 1 filer AND only 1 year
    const distinctYears = new Set(allYearGifts).size;
    if (donor.distinctComps === 1 && distinctYears <= 1) {
      score -= 5;
      factors.push(`One-time donor (1 candidate, 1 year)`);
    }

    // Normalize to 0-100
    score = Math.max(0, Math.min(Math.round(score), 100));

    // Tier assignment
    const tier = score >= 60 ? "A" : score >= 35 ? "B" : "C";

    // Build explanation summary
    const topComps = donor.compGifts
      .sort((a, b) => b.amount - a.amount)
      .slice(0, 5);
    const whySummary = buildWhySummary(donor, alreadyGiven, targetAsk, topComps);

    results.push({
      donor: donor.name,
      score,
      tier,
      already_given: alreadyGiven,
      target_ask: targetAsk,
      remaining_ask: remainingAsk,
      comp_min: compMin,
      comp_max: compMax,
      comp_range: `${fmt$(compMin)}–${fmt$(compMax)}`,
      distinct_comps: donor.distinctComps,
      total_to_comps: donor.totalToComps,
      comp_gifts: donor.compGifts,
      why_summary: whySummary,
      factors,
    });
  }

  // Sort by score descending
  results.sort((a, b) => b.score - a.score);
  return results;
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

// ── Step 5: Display results ───────────────────────────────────────────────
function displayResults(recommendations, targetProfile, comparables, cycle) {
  hideStatus();

  const section = document.getElementById("results-section");
  section.hidden = false;

  document.getElementById("results-title").textContent =
    `Recommendations for ${targetProfile.name} (${cycle - 1}–${cycle} Cycle)`;

  // Summary cards
  const totalRemainingAsk = recommendations.reduce((s, r) => s + r.remaining_ask, 0);
  const tierACnt = recommendations.filter(r => r.tier === "A").length;
  const summaryEl = document.getElementById("results-summary");
  summaryEl.innerHTML = `
    <div class="summary-card"><span class="sc-label">Total Donors</span><br><span class="sc-value">${fmtNum(recommendations.length)}</span></div>
    <div class="summary-card"><span class="sc-label">Tier A Donors</span><br><span class="sc-value">${fmtNum(tierACnt)}</span></div>
    <div class="summary-card"><span class="sc-label">Comparable Filers</span><br><span class="sc-value">${fmtNum(comparables.length)}</span></div>
    <div class="summary-card"><span class="sc-label">Total Remaining Ask</span><br><span class="sc-value">${fmt$(totalRemainingAsk)}</span></div>
  `;

  // Store for filtering/sorting/export
  window._recommendations = recommendations;
  window._targetProfile = targetProfile;
  window._comparables = comparables;
  window._cycle = cycle;

  // Render table
  renderRecTable(recommendations);

  // Wire up filters
  const searchEl = document.getElementById("results-search");
  const tierEl = document.getElementById("tier-filter");
  searchEl.value = "";
  tierEl.value = "";

  searchEl.addEventListener("input", () => renderFiltered());
  tierEl.addEventListener("change", () => renderFiltered());

  // Wire up sort
  document.querySelectorAll("#rec-table th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      const curDir = th.classList.contains("sort-asc") ? "asc" : th.classList.contains("sort-desc") ? "desc" : null;
      document.querySelectorAll("#rec-table th.sortable").forEach(t => t.classList.remove("sort-asc", "sort-desc"));
      const newDir = curDir === "desc" ? "asc" : "desc";
      th.classList.add("sort-" + newDir);
      window._sortCol = col;
      window._sortDir = newDir;
      renderFiltered();
    });
  });

  // Wire up export
  document.getElementById("export-csv").addEventListener("click", () => exportData("csv"));
  document.getElementById("export-xlsx").addEventListener("click", () => exportData("xlsx"));
}

function renderFiltered() {
  const q = (document.getElementById("results-search").value || "").trim().toLowerCase();
  const tier = document.getElementById("tier-filter").value;
  let rows = window._recommendations || [];

  if (q) rows = rows.filter(r => r.donor.toLowerCase().includes(q));
  if (tier) rows = rows.filter(r => r.tier === tier);

  const col = window._sortCol || "score";
  const dir = window._sortDir || "desc";
  rows = [...rows].sort((a, b) => {
    let va = a[col], vb = b[col];
    if (typeof va === "string") { va = va.toLowerCase(); vb = (vb || "").toLowerCase(); }
    if (va < vb) return dir === "asc" ? -1 : 1;
    if (va > vb) return dir === "asc" ? 1 : -1;
    return 0;
  });

  renderRecTable(rows);
}

function renderRecTable(rows) {
  const tbody = document.getElementById("rec-tbody");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#718096;padding:24px">No recommendations found.</td></tr>';
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
      <td><span class="tier-badge tier-${r.tier}">${r.tier}</span></td>
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

  const r = (window._recommendations || [])[idx];
  if (!r) return;

  btn.textContent = "Hide details ▾";

  const tr = btn.closest("tr");
  const detailRow = document.createElement("tr");
  detailRow.className = "detail-row";
  detailRow.dataset.for = idx;

  const topGifts = r.comp_gifts.sort((a, b) => b.amount - a.amount).slice(0, 10);

  detailRow.innerHTML = `<td colspan="9"><div class="detail-content">
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
function exportData(format) {
  const recs = window._recommendations || [];
  const target = window._targetProfile;
  const cycle = window._cycle;

  const exportRows = recs.map(r => ({
    "Donor Name": r.donor,
    "Searched Filer": target ? target.name : "",
    "Cycle": cycle ? `${cycle - 1}-${cycle}` : "",
    "Score": r.score,
    "Tier": r.tier,
    "Already Given This Cycle": r.already_given,
    "Comparable Giving Min": r.comp_min,
    "Comparable Giving Max": r.comp_max,
    "Target Ask": r.target_ask,
    "Remaining Ask": r.remaining_ask,
    "Comparable Filers Referenced": r.comp_gifts.map(g => g.filer).join("; "),
    "Reason for Recommendation": r.why_summary,
    "Distinct Comparable Filers": r.distinct_comps,
    "Total to Comparables": r.total_to_comps,
  }));

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

    downloadFile(csv, `recommendations_${target.slug}_${cycle}.csv`, "text/csv");

  } else if (format === "xlsx") {
    if (typeof XLSX === "undefined") {
      alert("Excel export library not loaded. Please try CSV instead.");
      return;
    }
    const ws = XLSX.utils.json_to_sheet(exportRows);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Recommendations");
    XLSX.writeFile(wb, `recommendations_${target.slug}_${cycle}.xlsx`);
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
