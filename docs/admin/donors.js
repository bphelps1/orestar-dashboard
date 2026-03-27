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
      <div class="cluster-card" data-pair-key="${esc(key)}">
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
          <button class="btn-primary" onclick="mergePair(${i}, '${esc(key)}')">Merge (keep selected)</button>
          <button class="btn-small" onclick="rejectPair('${esc(key)}')">Not duplicates</button>
        </div>
      </div>
    `;
  }).join("");

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
      <div class="merge-entry">
        <div class="merge-info">
          <div class="merge-names">"${esc(item.a)}" ↔ "${esc(item.b)}" (score: ${item.score})</div>
        </div>
        <button class="btn-small" onclick="undoDecision('${esc(key)}')">Undo</button>
      </div>
    `;
  }).join("");
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
