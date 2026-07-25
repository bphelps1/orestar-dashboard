/**
 * donors.js — donor entity search + profile pages.
 *
 * Search: server-backed typeahead on the `donors` table (trgm-indexed ilike,
 * ordered by total_given) — 300k+ entities is far too many for client Fuse.
 * Profile (?d=<donor_id>): donor row + donor_profile() RPC (by-year + top
 * recipients, aggregated server-side) + paginated transactions + aliases.
 */
"use strict";

const PAGE = 25;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt$ = v => (v == null) ? "—" : Number(v).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const fmtN = v => (v == null) ? "—" : Number(v).toLocaleString("en-US");

let txnPage = 0;
let currentDonor = null;

// ── Search ──────────────────────────────────────────────────────────────────

let searchTimer = null;
let highlightIdx = -1;
let results = [];

async function runSearch(q) {
  const sb = await getSupabase();
  const { data, error } = await sb.from("donors")
    .select("donor_id, display_name, book_type, city, state, total_given, gift_count, filer_slug")
    .ilike("display_name", `%${q}%`)
    .order("total_given", { ascending: false })
    .limit(12);
  if (error) { console.warn(error.message); return; }
  results = data || [];
  const ul = $("dn-results");
  highlightIdx = -1;
  if (!results.length) {
    ul.innerHTML = `<li class="dn-result-meta">No donors match "${esc(q)}"</li>`;
  } else {
    ul.innerHTML = results.map((r, i) => `
      <li data-idx="${i}">
        <div class="dn-result-name">${esc(r.display_name)}</div>
        <div class="dn-result-meta">
          ${esc([r.book_type, [r.city, r.state].filter(Boolean).join(", ")].filter(Boolean).join(" · "))}
          · ${fmt$(r.total_given)} given · ${fmtN(r.gift_count)} gifts
        </div>
      </li>`).join("");
  }
  ul.hidden = false;
}

function pickResult(i) {
  const r = results[i];
  if (!r) return;
  $("dn-results").hidden = true;
  $("dn-search").value = r.display_name;
  history.pushState(null, "", `?d=${encodeURIComponent(r.donor_id)}`);
  loadProfile(r.donor_id);
}

function initSearch() {
  const input = $("dn-search");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (q.length < 2) { $("dn-results").hidden = true; return; }
    searchTimer = setTimeout(() => runSearch(q), 220);
  });
  input.addEventListener("keydown", e => {
    const ul = $("dn-results");
    if (ul.hidden) return;
    const items = ul.querySelectorAll("li[data-idx]");
    if (e.key === "ArrowDown") { e.preventDefault(); highlightIdx = Math.min(highlightIdx + 1, items.length - 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlightIdx = Math.max(highlightIdx - 1, 0); }
    else if (e.key === "Enter") { e.preventDefault(); if (highlightIdx >= 0) pickResult(highlightIdx); return; }
    else if (e.key === "Escape") { ul.hidden = true; return; }
    else return;
    items.forEach((li, i) => li.classList.toggle("selected", i === highlightIdx));
  });
  document.addEventListener("click", e => {
    if (!e.target.closest(".dn-search-box")) $("dn-results").hidden = true;
  });
  $("dn-results").addEventListener("mousedown", e => {
    const li = e.target.closest("li[data-idx]");
    if (li) pickResult(+li.dataset.idx);
  });
}

// ── Profile ─────────────────────────────────────────────────────────────────

async function loadProfile(donorId) {
  $("dn-error").hidden = true;
  $("dn-profile").hidden = true;
  $("dn-loading").hidden = false;
  try {
    const sb = await getSupabase();
    const [{ data: donor, error: e1 }, { data: prof, error: e2 }, { data: aliases }] = await Promise.all([
      sb.from("donors").select("*").eq("donor_id", donorId).single(),
      sb.rpc("donor_profile", { p_donor_id: donorId }),
      sb.from("donor_aliases").select("raw_name").eq("donor_id", donorId).limit(40),
    ]);
    if (e1) throw new Error(e1.message);
    if (e2) throw new Error(e2.message);
    currentDonor = donor;

    $("dn-name").textContent = donor.display_name;
    const meta = [donor.book_type,
                  [donor.city, donor.state].filter(Boolean).join(", "),
                  donor.employer && `Employer: ${donor.employer}`,
                  donor.occupation && `Occupation: ${donor.occupation}`]
      .filter(Boolean).join(" · ");
    $("dn-meta").textContent = meta;

    const links = [];
    if (donor.filer_slug) {
      links.push(`<a href="/" data-open-filer="${esc(donor.filer_slug)}">This donor is a committee — view its page →</a>`);
    }
    if (donor.related_filer_slug) {
      links.push(`<a href="/" data-open-filer="${esc(donor.related_filer_slug)}">Related candidate committee →</a>`);
    }
    $("dn-links").innerHTML = links.join("");
    $("dn-links").querySelectorAll("[data-open-filer]").forEach(a =>
      a.addEventListener("click", () => {
        sessionStorage.setItem("openFilerSlug", a.dataset.openFiler);
      }));

    $("dn-given").textContent = fmt$(donor.total_given);
    $("dn-received").textContent = fmt$(donor.total_received);
    $("dn-gifts").textContent = fmtN(donor.gift_count);
    $("dn-span").textContent = (donor.first_date && donor.last_date)
      ? `${donor.first_date.slice(0, 4)}–${donor.last_date.slice(0, 4)}` : "—";

    renderChart(prof?.by_year || []);
    renderRecipients(prof?.top_recipients || []);
    $("dn-aliases").innerHTML = (aliases || [])
      .map(a => `<span class="dn-alias">${esc(a.raw_name)}</span>`).join(" ") || "—";

    txnPage = 0;
    await loadTxns();

    $("dn-loading").hidden = true;
    $("dn-profile").hidden = false;
  } catch (err) {
    $("dn-loading").hidden = true;
    $("dn-error").hidden = false;
    $("dn-error").textContent = "Could not load donor: " + err.message;
  }
}

function renderChart(byYear) {
  const el = $("dn-chart");
  const inst = echarts.getInstanceByDom(el);
  if (inst) inst.dispose();
  if (!byYear.length) { el.innerHTML = '<span class="dn-sub">No dated transactions.</span>'; return; }
  const chart = echarts.init(el, null, { renderer: "svg" });
  chart.setOption({
    grid: { left: 70, right: 12, top: 12, bottom: 24 },
    xAxis: { type: "category", data: byYear.map(r => r.year) },
    yAxis: { type: "value", axisLabel: { formatter: v => "$" + (v >= 1e6 ? (v / 1e6) + "M" : (v / 1e3) + "K") } },
    tooltip: { trigger: "axis", valueFormatter: v => fmt$(v) },
    series: [
      { name: "Given", type: "bar", data: byYear.map(r => r.given), itemStyle: { color: "#3182ce" } },
      { name: "Received", type: "bar", data: byYear.map(r => r.received), itemStyle: { color: "#dd6b20" } },
    ],
  });
}

function renderRecipients(rows) {
  $("dn-recipients").querySelector("tbody").innerHTML = rows.map(r => {
    const name = r.slug
      ? `<a href="/" data-open-filer="${esc(r.slug)}">${esc(r.filer)}</a>`
      : esc(r.filer);
    return `<tr><td>${name}</td><td class="num">${fmtN(r.n)}</td><td class="num">${fmt$(r.total)}</td></tr>`;
  }).join("") || '<tr><td colspan="3">No contributions.</td></tr>';
  $("dn-recipients").querySelectorAll("[data-open-filer]").forEach(a =>
    a.addEventListener("click", () => sessionStorage.setItem("openFilerSlug", a.dataset.openFiler)));
}

async function loadTxns() {
  const sb = await getSupabase();
  const { data, error } = await sb.from("transactions")
    .select("tran_date, tran_type, amount, filer_canonical, purpose")
    .eq("donor_id", currentDonor.donor_id)
    .order("tran_date", { ascending: false, nullsFirst: false })
    .range(txnPage * PAGE, txnPage * PAGE + PAGE - 1);
  if (error) { console.warn(error.message); return; }
  $("dn-txns").querySelector("tbody").innerHTML = (data || []).map(r => `
    <tr><td>${esc(r.tran_date || "")}</td><td>${esc(r.tran_type)}</td>
    <td class="num">${fmt$(r.amount)}</td><td>${esc(r.filer_canonical || "")}</td>
    <td>${esc(r.purpose || "")}</td></tr>`).join("");
  $("dn-prev").disabled = txnPage === 0;
  $("dn-next").disabled = (data || []).length < PAGE;
  $("dn-page").textContent = `Page ${txnPage + 1}`;
}

// ── Init ────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initSearch();
  $("dn-prev").onclick = () => { if (txnPage > 0) { txnPage--; loadTxns(); } };
  $("dn-next").onclick = () => { txnPage++; loadTxns(); };
  const id = new URLSearchParams(location.search).get("d");
  if (id) loadProfile(id);
});
window.addEventListener("popstate", () => {
  const id = new URLSearchParams(location.search).get("d");
  if (id) loadProfile(id); else { $("dn-profile").hidden = true; }
});
