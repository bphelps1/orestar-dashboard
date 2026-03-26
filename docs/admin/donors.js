/**
 * donors.js — Admin Donor Dedup/Merge Workflow
 *
 * Reads pending duplicate clusters from Supabase, allows admin to:
 *  - Review clusters and evidence
 *  - Merge duplicates (pick canonical name)
 *  - Reject false positives
 *  - Undo previous merges
 *  - Manually search and merge donors
 */

"use strict";

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Auth ───────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  const session = await requireAuth();
  if (session) {
    document.getElementById("user-info").textContent = session.user.email;
    await loadPendingClusters();
    await loadMergeLog();
    initDonorSearch();
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

// ── Pending clusters ──────────────────────────────────────────────────────
async function loadPendingClusters() {
  const sb = await getSupabase();
  const { data: clusters, error } = await sb
    .from("donor_merge_pending")
    .select("*")
    .eq("status", "pending")
    .order("created_at", { ascending: false });

  const listEl = document.getElementById("pending-list");
  const emptyEl = document.getElementById("pending-empty");

  if (error || !clusters || !clusters.length) {
    listEl.innerHTML = "";
    emptyEl.hidden = false;
    return;
  }

  emptyEl.hidden = true;

  // For each cluster, load the donor names from donor_canonical
  const allIds = clusters.flatMap(c => c.donor_ids);
  const { data: donors } = await sb
    .from("donor_canonical")
    .select("id, canonical_name")
    .in("id", allIds);

  const donorNames = new Map((donors || []).map(d => [d.id, d.canonical_name]));

  listEl.innerHTML = clusters.map(cluster => {
    const names = cluster.donor_ids.map(id => ({
      id,
      name: donorNames.get(id) || `#${id}`,
    }));
    const evidence = cluster.evidence || {};

    return `
      <div class="cluster-card" data-cluster-id="${cluster.id}">
        <div class="cluster-key">Cluster: ${esc(cluster.cluster_key)}</div>
        <div class="cluster-names">
          ${names.map((n, i) => `
            <label class="cluster-name">
              <input type="radio" name="keep-${cluster.id}" value="${n.id}" ${i === 0 ? "checked" : ""} />
              <span>${esc(n.name)}</span>
            </label>
          `).join("")}
        </div>
        ${evidence.reason ? `<div class="cluster-evidence">${esc(evidence.reason)}</div>` : ""}
        <div class="cluster-actions">
          <button class="btn-primary" onclick="mergeCluster(${cluster.id})">Merge (keep selected)</button>
          <button class="btn-small" onclick="rejectCluster(${cluster.id})">Not duplicates</button>
        </div>
      </div>
    `;
  }).join("");
}

async function mergeCluster(clusterId) {
  const sb = await getSupabase();
  const card = document.querySelector(`[data-cluster-id="${clusterId}"]`);
  const keepId = parseInt(card.querySelector(`input[name="keep-${clusterId}"]:checked`).value);

  // Get cluster info
  const { data: cluster } = await sb
    .from("donor_merge_pending")
    .select("*")
    .eq("id", clusterId)
    .single();

  if (!cluster) return;

  const mergeIds = cluster.donor_ids.filter(id => id !== keepId);

  // For each merged donor: move aliases, log the merge, delete the donor
  for (const mergedId of mergeIds) {
    // Get merged donor info
    const { data: mergedDonor } = await sb
      .from("donor_canonical")
      .select("*")
      .eq("id", mergedId)
      .single();

    // Get aliases that will be moved
    const { data: aliases } = await sb
      .from("donor_alias")
      .select("*")
      .eq("canonical_id", mergedId);

    // Move aliases to the kept donor
    if (aliases && aliases.length) {
      await sb
        .from("donor_alias")
        .update({ canonical_id: keepId })
        .eq("canonical_id", mergedId);
    }

    // Add the merged donor's name as an alias of the kept donor
    await sb.from("donor_alias").upsert({
      canonical_id: keepId,
      alias_name: mergedDonor?.canonical_name || `Unknown #${mergedId}`,
      source: "admin",
    }, { onConflict: "alias_name_lower" });

    // Log the merge
    await sb.from("donor_merge_log").insert({
      kept_id: keepId,
      merged_id: mergedId,
      merged_name: mergedDonor?.canonical_name || "",
      merged_aliases: aliases,
      action: "merge",
    });

    // Delete the merged canonical entry
    await sb.from("donor_canonical").delete().eq("id", mergedId);
  }

  // Mark cluster as merged
  await sb
    .from("donor_merge_pending")
    .update({ status: "merged", reviewed_at: new Date().toISOString() })
    .eq("id", clusterId);

  // Refresh
  await loadPendingClusters();
  await loadMergeLog();
}

async function rejectCluster(clusterId) {
  const sb = await getSupabase();
  await sb
    .from("donor_merge_pending")
    .update({ status: "rejected", reviewed_at: new Date().toISOString() })
    .eq("id", clusterId);

  await loadPendingClusters();
}

// ── Merge log ─────────────────────────────────────────────────────────────
async function loadMergeLog() {
  const sb = await getSupabase();
  const { data: logs, error } = await sb
    .from("donor_merge_log")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(50);

  const logEl = document.getElementById("merge-log");
  const emptyEl = document.getElementById("log-empty");

  if (error || !logs || !logs.length) {
    logEl.innerHTML = "";
    emptyEl.hidden = false;
    return;
  }

  emptyEl.hidden = true;

  // Look up kept donor names
  const keptIds = [...new Set(logs.map(l => l.kept_id))];
  const { data: keptDonors } = await sb
    .from("donor_canonical")
    .select("id, canonical_name")
    .in("id", keptIds);

  const keptNames = new Map((keptDonors || []).map(d => [d.id, d.canonical_name]));

  logEl.innerHTML = logs.map(log => {
    const keptName = keptNames.get(log.kept_id) || `#${log.kept_id}`;
    const ts = new Date(log.created_at).toLocaleString();
    const isUndo = log.action === "undo";

    return `
      <div class="merge-entry">
        <div class="merge-info">
          <div class="merge-names">
            ${isUndo ? "Undone:" : ""} "${esc(log.merged_name)}" → "${esc(keptName)}"
          </div>
          <div class="merge-date">${ts}</div>
        </div>
        ${!isUndo ? `<button class="btn-small" onclick="undoMerge(${log.id}, ${log.kept_id}, ${log.merged_id})">Undo</button>` : ""}
      </div>
    `;
  }).join("");
}

async function undoMerge(logId, keptId, mergedId) {
  const sb = await getSupabase();

  // Get the original merge log entry
  const { data: log } = await sb
    .from("donor_merge_log")
    .select("*")
    .eq("id", logId)
    .single();

  if (!log) return;

  // Re-create the merged donor canonical entry
  const { data: restored } = await sb
    .from("donor_canonical")
    .insert({ canonical_name: log.merged_name })
    .select()
    .single();

  if (restored && log.merged_aliases && log.merged_aliases.length) {
    // Move aliases back
    for (const alias of log.merged_aliases) {
      await sb.from("donor_alias")
        .update({ canonical_id: restored.id })
        .eq("id", alias.id);
    }
  }

  // Remove the alias that was created during merge
  await sb.from("donor_alias")
    .delete()
    .eq("canonical_id", keptId)
    .eq("alias_name", log.merged_name);

  // Log the undo
  await sb.from("donor_merge_log").insert({
    kept_id: keptId,
    merged_id: mergedId,
    merged_name: log.merged_name,
    action: "undo",
  });

  await loadMergeLog();
}

// ── Manual donor search ───────────────────────────────────────────────────
function initDonorSearch() {
  const input = document.getElementById("donor-search");
  let debounce = null;

  input.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => searchDonors(input.value.trim()), 300);
  });
}

async function searchDonors(q) {
  const resultsEl = document.getElementById("donor-search-results");
  if (!q || q.length < 2) {
    resultsEl.innerHTML = "";
    return;
  }

  const sb = await getSupabase();
  const { data: donors } = await sb
    .from("donor_canonical")
    .select("id, canonical_name, employer, city, state")
    .ilike("canonical_name", `%${q}%`)
    .limit(20);

  if (!donors || !donors.length) {
    resultsEl.innerHTML = '<div class="empty-msg">No donors found.</div>';
    return;
  }

  resultsEl.innerHTML = donors.map(d => `
    <div class="donor-result-row">
      <div>
        <div class="donor-result-name">${esc(d.canonical_name)}</div>
        <div class="donor-result-meta">${[d.employer, d.city, d.state].filter(Boolean).join(" · ") || "—"}</div>
      </div>
      <div>ID: ${d.id}</div>
    </div>
  `).join("");
}

// Make functions available globally for onclick handlers
window.mergeCluster = mergeCluster;
window.rejectCluster = rejectCluster;
window.undoMerge = undoMerge;
