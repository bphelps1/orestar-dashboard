/**
 * donors.js — Admin Donor Dedup/Merge Workflow
 *
 * Loads review_queue.json (fuzzy-matched pairs from process.py),
 * displays them for admin review, and stores decisions in Supabase
 * (donor_review_decisions table) and entity_map.json.
 */

"use strict";

const DATA = "../data/aggregated";
const _cbv = Date.now();

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function fetchJSON(path) {
  const sep = path.includes("?") ? "&" : "?";
  const resp = await fetch(`${path}${sep}_v=${_cbv}`);
  if (!resp.ok) throw new Error(`Failed to load ${path}: ${resp.status}`);
  return resp.json();
}

// ── State ───────────────────────────────────────────────────────────────
let reviewQueue = [];       // from review_queue.json
let decisions = new Map();  // pairKey → "merged"|"rejected"
let entityMap = {};         // from entity_map.json (canonical merges)

// ── Auth ───────────────────────────────────────────────────────────────
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

async function initApp() {
  // Load review queue
  try {
    reviewQueue = await fetchJSON("../data/review_queue.json");
  } catch (e) {
    console.warn("Could not load review_queue.json:", e.message);
    reviewQueue = [];
  }

  // Load existing decisions from Supabase
  try {
    const sb = await getSupabase();
    const { data } = await sb.from("donor_review_decisions").select("*");
    if (data) {
      data.forEach(d => decisions.set(d.pair_key, d.decision));
    }
  } catch (e) {
    console.warn("Could not load decisions from Supabase:", e.message);
  }

  // Load entity map
  try {
    entityMap = await fetchJSON("../data/entity_map.json");
  } catch (e) {
    console.warn("Could not load entity_map.json:", e.message);
    entityMap = {};
  }

  renderPendingPairs();
  renderMergeLog();
  initSearch();
}

// ── Pending Pairs ──────────────────────────────────────────────────────
function pairKey(a, b) {
  return [a, b].sort().join("|||").toLowerCase();
}

function renderPendingPairs(filter = "") {
  const listEl = document.getElementById("pending-list");
  const emptyEl = document.getElementById("pending-empty");
  const countEl = document.getElementById("review-count");

  // Filter out already-decided pairs
  let pending = reviewQueue.filter(item => {
    const key = pairKey(item.a, item.b);
    return !decisions.has(key);
  });

  // Apply text filter
  if (filter) {
    const q = filter.toLowerCase();
    pending = pending.filter(item =>
      item.a.toLowerCase().includes(q) || item.b.toLowerCase().includes(q)
    );
  }

  // Sort by score descending (highest confidence first)
  pending.sort((a, b) => b.score - a.score);

  countEl.textContent = `${pending.length} pairs pending`;

  if (!pending.length) {
    listEl.innerHTML = "";
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;

  // Show first 100 to avoid DOM overload
  const shown = pending.slice(0, 100);

  listEl.innerHTML = shown.map((item, i) => {
    const key = pairKey(item.a, item.b);
    return `
      <div class="cluster-card" data-pair-key="${esc(key)}" data-pair-idx="${i}">
        <div class="pair-score">Score: ${item.score}</div>
        <div class="cluster-names">
          <div class="pair-name">
            <label><input type="radio" name="keep-${i}" value="a" checked /> <span>${esc(item.a)}</span></label>
          </div>
          <div class="pair-name">
            <label><input type="radio" name="keep-${i}" value="b" /> <span>${esc(item.b)}</span></label>
          </div>
        </div>
        <div class="cluster-actions">
          <button class="btn-primary btn-merge">Merge (keep selected)</button>
          <button class="btn-small btn-reject">Not duplicates</button>
        </div>
      </div>
    `;
  }).join("");

  // Event delegation — avoids inline onclick with quotes in donor names
  listEl.querySelectorAll(".btn-merge").forEach(btn => {
    const card = btn.closest(".cluster-card");
    btn.addEventListener("click", () => mergePair(+card.dataset.pairIdx, card.dataset.pairKey));
  });
  listEl.querySelectorAll(".btn-reject").forEach(btn => {
    const card = btn.closest(".cluster-card");
    btn.addEventListener("click", () => rejectPair(card.dataset.pairKey));
  });

  if (pending.length > 100) {
    listEl.insertAdjacentHTML("beforeend",
      `<p class="empty-msg">Showing 100 of ${pending.length} pairs. Use filter to narrow.</p>`
    );
  }
}

async function mergePair(idx, key) {
  const pending = reviewQueue.filter(item => !decisions.has(pairKey(item.a, item.b)));
  pending.sort((a, b) => b.score - a.score);
  const item = pending[idx];
  if (!item) return;

  const card = document.querySelector(`[data-pair-key="${key}"]`);
  const keepChoice = card.querySelector(`input[name="keep-${idx}"]:checked`).value;
  const keepName = keepChoice === "a" ? item.a : item.b;
  const mergeName = keepChoice === "a" ? item.b : item.a;

  // Store decision
  decisions.set(key, "merged");

  // Save to Supabase
  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").upsert({
      pair_key: key,
      decision: "merged",
      kept_name: keepName,
      merged_name: mergeName,
      score: item.score,
      decided_at: new Date().toISOString(),
    }, { onConflict: "pair_key" });
  } catch (e) {
    console.warn("Could not save to Supabase:", e.message);
  }

  renderPendingPairs(document.getElementById("review-search").value);
  renderMergeLog();
}

async function rejectPair(key) {
  decisions.set(key, "rejected");

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").upsert({
      pair_key: key,
      decision: "rejected",
      decided_at: new Date().toISOString(),
    }, { onConflict: "pair_key" });
  } catch (e) {
    console.warn("Could not save to Supabase:", e.message);
  }

  renderPendingPairs(document.getElementById("review-search").value);
}

// ── Merge Log ──────────────────────────────────────────────────────────
function renderMergeLog() {
  const logEl = document.getElementById("merge-log");
  const emptyEl = document.getElementById("log-empty");

  const merged = [];
  for (const [key, decision] of decisions) {
    if (decision === "merged") {
      // Find the original item
      const item = reviewQueue.find(r => pairKey(r.a, r.b) === key);
      if (item) merged.push(item);
    }
  }

  if (!merged.length) {
    logEl.innerHTML = "";
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;

  logEl.innerHTML = merged.map(item => {
    const key = pairKey(item.a, item.b);
    return `
      <div class="merge-entry" data-pair-key="${esc(key)}">
        <div class="merge-info">
          <div class="merge-names">"${esc(item.a)}" ↔ "${esc(item.b)}" (score: ${item.score})</div>
        </div>
        <button class="btn-small btn-undo">Undo</button>
      </div>
    `;
  }).join("");

  logEl.querySelectorAll(".btn-undo").forEach(btn => {
    const entry = btn.closest(".merge-entry");
    btn.addEventListener("click", () => undoDecision(entry.dataset.pairKey));
  });
}

async function undoDecision(key) {
  decisions.delete(key);

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").delete().eq("pair_key", key);
  } catch (e) {
    console.warn("Could not delete from Supabase:", e.message);
  }

  renderPendingPairs(document.getElementById("review-search").value);
  renderMergeLog();
}

// ── Search filter ──────────────────────────────────────────────────────
function initSearch() {
  const input = document.getElementById("review-search");
  if (!input) return;
  input.addEventListener("input", () => {
    renderPendingPairs(input.value.trim());
  });
}

// ── Tab switching ─────────────────────────────────────────────────────
document.querySelectorAll(".admin-tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".admin-tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".admin-tab").forEach(t => t.hidden = true);
    btn.classList.add("active");
    document.getElementById(btn.dataset.adminTab).hidden = false;
    const tab = btn.dataset.adminTab;
    if (tab === "tab-active-merges" || tab === "tab-manual-merge" || tab === "tab-rejected") {
      loadMergeManager();
    }
  });
});

// ── Merge Manager ─────────────────────────────────────────────────────
let allDonorNames = new Set();
let supabaseDecisionRows = [];   // full rows from donor_review_decisions

function buildDonorNameSet() {
  // Collect all unique donor names from the review queue
  reviewQueue.forEach(item => {
    allDonorNames.add(item.a);
    allDonorNames.add(item.b);
  });
  // Also add names from entity_map
  for (const [raw, canonical] of Object.entries(entityMap)) {
    allDonorNames.add(raw);
    allDonorNames.add(canonical);
  }
}

async function loadMergeManager() {
  // Build donor name set for autocomplete (if not already built)
  if (!allDonorNames.size) buildDonorNameSet();

  // Reload full decision rows from Supabase for detailed info
  try {
    const sb = await getSupabase();
    const { data } = await sb.from("donor_review_decisions").select("*");
    supabaseDecisionRows = data || [];
  } catch (e) {
    console.warn("Could not load decisions for merge manager:", e.message);
  }

  // Combine active merges from both sources
  const merges = [];

  // 1. entity_map.json merges
  for (const [raw, canonical] of Object.entries(entityMap)) {
    if (raw !== canonical) {
      merges.push({
        pairKey: pairKey(raw, canonical),
        rawName: raw,
        canonicalName: canonical,
        source: "entity_map",
        score: null,
      });
    }
  }

  // 2. Supabase admin merges
  supabaseDecisionRows.forEach(row => {
    if (row.decision === "merged") {
      merges.push({
        pairKey: row.pair_key,
        rawName: row.merged_name || row.pair_key,
        canonicalName: row.kept_name || "—",
        source: "admin",
        score: row.score,
      });
    }
  });

  renderActiveMerges(merges, "");
  renderRejectedPairs();
  initMergeSearch(merges);
  initMergeCreator();
}

// ── Active Merges ─────────────────────────────────────────────────────
let _cachedMerges = [];

function initMergeSearch(merges) {
  _cachedMerges = merges;
  const input = document.getElementById("merge-search");
  if (!input) return;
  input.addEventListener("input", () => {
    renderActiveMerges(_cachedMerges, input.value.trim());
  });
}

function renderActiveMerges(merges, searchQuery) {
  const container = document.getElementById("merge-list");

  let filtered = merges;
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    filtered = merges.filter(m =>
      m.rawName.toLowerCase().includes(q) ||
      m.canonicalName.toLowerCase().includes(q)
    );
  }

  if (!filtered.length) {
    container.innerHTML = `<p class="empty-msg">No active merges found.</p>`;
    return;
  }

  const rows = filtered.map(m => {
    const sourceBadge = m.source === "entity_map"
      ? `<span class="merge-source-badge badge-gray">entity_map</span>`
      : `<span class="merge-source-badge badge-blue">admin</span>`;
    const scoreCell = m.score != null ? m.score : "—";
    const editBtn = m.source === "admin"
      ? `<button class="btn-small btn-edit">Edit</button>`
      : "";
    const splitBtn = `<button class="btn-small btn-split">Split</button>`;

    return `<tr data-pair-key="${esc(m.pairKey)}" data-source="${esc(m.source)}">
      <td>${esc(m.rawName)}</td>
      <td class="merge-arrow-cell">→</td>
      <td>${esc(m.canonicalName)}</td>
      <td>${sourceBadge}</td>
      <td>${scoreCell}</td>
      <td class="merge-actions">${editBtn} ${splitBtn}</td>
    </tr>`;
  }).join("");

  container.innerHTML = `
    <table class="merge-table">
      <thead>
        <tr>
          <th>Raw Name</th>
          <th></th>
          <th>Canonical Name</th>
          <th>Source</th>
          <th>Score</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  container.querySelectorAll(".btn-edit").forEach(btn => {
    const row = btn.closest("tr");
    btn.addEventListener("click", () => editMerge(row.dataset.pairKey));
  });
  container.querySelectorAll(".btn-split").forEach(btn => {
    const row = btn.closest("tr");
    btn.addEventListener("click", () => splitMerge(row.dataset.pairKey, row.dataset.source));
  });
}

// ── Edit Merge ────────────────────────────────────────────────────────
async function editMerge(pk) {
  const row = supabaseDecisionRows.find(r => r.pair_key === pk && r.decision === "merged");
  if (!row) return;

  const newName = prompt("Edit canonical name:", row.kept_name);
  if (newName === null || newName.trim() === "" || newName.trim() === row.kept_name) return;

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").upsert({
      pair_key: pk,
      decision: "merged",
      kept_name: newName.trim(),
      merged_name: row.merged_name,
      score: row.score,
      decided_at: new Date().toISOString(),
    }, { onConflict: "pair_key" });

    // Update local state
    decisions.set(pk, "merged");
  } catch (e) {
    console.warn("Could not update merge:", e.message);
    alert("Error updating merge: " + e.message);
    return;
  }

  await loadMergeManager();
}

// ── Split Merge ───────────────────────────────────────────────────────
async function splitMerge(pk, source) {
  if (source === "entity_map") {
    alert("This merge is in entity_map.json \u2014 edit the file directly to remove it.");
    return;
  }

  if (!confirm("Split this merge? The pair will return to pending review.")) return;

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").delete().eq("pair_key", pk);
    decisions.delete(pk);
  } catch (e) {
    console.warn("Could not delete decision:", e.message);
    alert("Error splitting merge: " + e.message);
    return;
  }

  await loadMergeManager();
  renderPendingPairs(document.getElementById("review-search").value);
  renderMergeLog();
}

// ── Manual Merge Creator ──────────────────────────────────────────────
let _mergeCreatorInitialized = false;

function initMergeCreator() {
  if (_mergeCreatorInitialized) return;
  _mergeCreatorInitialized = true;

  const inputA = document.getElementById("merge-donor-a");
  const inputB = document.getElementById("merge-donor-b");
  const suggestA = document.getElementById("merge-suggest-a");
  const suggestB = document.getElementById("merge-suggest-b");
  const createBtn = document.getElementById("merge-create-btn");

  function updateCreateBtn() {
    createBtn.disabled = !(inputA.value.trim() && inputB.value.trim());
  }

  function setupAutocomplete(input, suggestEl) {
    input.addEventListener("input", () => {
      updateCreateBtn();
      const q = input.value.trim().toLowerCase();
      if (q.length < 2) { suggestEl.hidden = true; return; }

      const matches = [];
      for (const name of allDonorNames) {
        if (name.toLowerCase().includes(q)) {
          matches.push(name);
          if (matches.length >= 10) break;
        }
      }

      if (!matches.length) { suggestEl.hidden = true; return; }

      suggestEl.innerHTML = matches.map(m =>
        `<div class="merge-suggest-item" data-name="${esc(m)}">${esc(m)}</div>`
      ).join("");
      suggestEl.hidden = false;
    });

    suggestEl.addEventListener("click", (e) => {
      const item = e.target.closest(".merge-suggest-item");
      if (!item) return;
      input.value = item.dataset.name;
      suggestEl.hidden = true;
      updateCreateBtn();
    });

    // Hide suggestions on blur (with delay so click can register)
    input.addEventListener("blur", () => {
      setTimeout(() => { suggestEl.hidden = true; }, 200);
    });
  }

  setupAutocomplete(inputA, suggestA);
  setupAutocomplete(inputB, suggestB);

  createBtn.addEventListener("click", createManualMerge);
}

async function createManualMerge() {
  const inputA = document.getElementById("merge-donor-a");
  const inputB = document.getElementById("merge-donor-b");
  const donorA = inputA.value.trim();
  const canonicalName = inputB.value.trim();

  if (!donorA || !canonicalName) return;

  const pk = pairKey(donorA, canonicalName);

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").upsert({
      pair_key: pk,
      decision: "merged",
      kept_name: canonicalName,
      merged_name: donorA,
      score: 100,
      decided_at: new Date().toISOString(),
    }, { onConflict: "pair_key" });

    decisions.set(pk, "merged");
  } catch (e) {
    console.warn("Could not create manual merge:", e.message);
    alert("Error creating merge: " + e.message);
    return;
  }

  // Clear inputs
  inputA.value = "";
  inputB.value = "";
  document.getElementById("merge-create-btn").disabled = true;

  await loadMergeManager();
}

// ── Rejected Pairs ────────────────────────────────────────────────────
function renderRejectedPairs() {
  const container = document.getElementById("rejected-list");

  const rejected = supabaseDecisionRows.filter(r => r.decision === "rejected");

  if (!rejected.length) {
    container.innerHTML = `<p class="empty-msg">No rejected pairs.</p>`;
    return;
  }

  container.innerHTML = rejected.map(row => {
    const names = row.pair_key.split("|||");
    const display = names.length === 2
      ? `${esc(names[0])} / ${esc(names[1])}`
      : esc(row.pair_key);
    return `
      <div class="merge-entry" data-pair-key="${esc(row.pair_key)}">
        <div class="merge-info">
          <div class="merge-names">${display}</div>
        </div>
        <button class="btn-small btn-reconsider">Reconsider</button>
      </div>
    `;
  }).join("");

  container.querySelectorAll(".btn-reconsider").forEach(btn => {
    const entry = btn.closest(".merge-entry");
    btn.addEventListener("click", () => reconsiderPair(entry.dataset.pairKey));
  });
}

async function reconsiderPair(pk) {
  if (!confirm("Remove this rejection? The pair will return to pending review.")) return;

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").delete().eq("pair_key", pk);
    decisions.delete(pk);
  } catch (e) {
    console.warn("Could not delete rejection:", e.message);
    alert("Error: " + e.message);
    return;
  }

  renderRejectedPairs();
  renderPendingPairs(document.getElementById("review-search").value);
}
