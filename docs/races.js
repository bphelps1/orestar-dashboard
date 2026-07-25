/**
 * races.js — legislative race map.
 *
 * Renders an ECharts choropleth of Oregon House (60) / Senate (30) districts,
 * shaded by total raised this cycle, from:
 *   • docs/assets/or_house.geojson / or_senate.geojson  (TIGER 2024, simplified)
 *   • dashboard_cache 'activity_snapshot' → legislative_map  (built by the
 *     daily pipeline; strict ORESTAR election-year gate, cycle = since 2025-01)
 *
 * Click a district → side panel lists candidates (party, raised, cash on
 * hand); committee links hand off to the dashboard via sessionStorage.
 */
"use strict";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt$ = v => Number(v || 0).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

let chart = null;
let mapData = null;          // legislative_map blob
let chamber = "house";       // 'house' | 'senate'
const GEO = { house: "assets/or_house.geojson", senate: "assets/or_senate.geojson" };
const registered = {};

function partyBadge(party) {
  const p = (party || "").toLowerCase();
  const cls = p.startsWith("dem") ? "D" : p.startsWith("rep") ? "R" : "other";
  const label = cls === "other" ? (party || "—") : cls;
  return `<span class="rc-party ${cls}">${esc(label)}</span>`;
}

/** Statewide races have no geography — render them as cards instead of a map. */
function renderStatewide() {
  chamber = "statewide";
  ["house", "senate", "statewide"].forEach(c =>
    $("rc-" + c).classList.toggle("active", c === "statewide"));
  $("rc-map").hidden = true;
  const list = $("rc-statewide-list");
  list.hidden = false;

  const races = mapData.statewide || {};
  list.innerHTML = Object.entries(races).map(([office, e]) => `
    <div class="rc-sw-card">
      <div class="rc-sw-head">
        <span class="rc-sw-office">${esc(office)}</span>
        <span class="rc-sw-total">${fmt$(e.total_raised)} raised this cycle</span>
      </div>
      <div class="rc-sw-sub">${e.candidates.length} candidate${e.candidates.length === 1 ? "" : "s"} on the ballot</div>
      ${e.candidates.map(c => {
        const nm = esc(c.candidate_name || c.name || "");
        const nameHtml = c.slug
          ? `<a href="/" data-slug="${esc(c.slug)}">${nm}</a>`
          : `<span class="rc-no-cmte-name">${nm}</span>`;
        const sub = c.slug ? esc(c.name || "") : "No committee on file";
        const nums = c.slug
          ? `<span>Raised: <b>${fmt$(c.raised_cycle)}</b></span>
             <span>Cash on hand: <b>${fmt$(c.cash_on_hand)}</b></span>`
          : `<span class="rc-no-cmte">Not reporting contributions</span>`;
        return `<div class="rc-cand${c.slug ? "" : " rc-cand-nocmte"}">
            <div class="rc-cand-name">${nameHtml}${partyBadge(c.party)}</div>
            <div class="rc-cand-sub">${sub}</div>
            <div class="rc-cand-nums">${nums}</div>
          </div>`;
      }).join("")}
    </div>`).join("") || '<div class="rc-empty">No statewide races on this ballot.</div>';

  list.querySelectorAll("a[data-slug]").forEach(a =>
    a.addEventListener("click", () => sessionStorage.setItem("openFilerSlug", a.dataset.slug)));

  $("rc-panel-title").textContent = "Statewide races";
  $("rc-panel-total").textContent = mapData.election || "";
  $("rc-panel-body").innerHTML =
    '<span class="rc-empty">Statewide offices are elected by the whole state, so they have no district map. '
    + 'Offices absent here are not on this ballot — Oregon staggers them across cycles.</span>';
}

async function loadChamber(which) {
  if (which === "statewide") return renderStatewide();
  chamber = which;
  $("rc-map").hidden = false;
  $("rc-statewide-list").hidden = true;
  ["house", "senate", "statewide"].forEach(c =>
    $("rc-" + c).classList.toggle("active", c === which));

  if (!registered[which]) {
    const resp = await fetch(GEO[which]);
    if (!resp.ok) throw new Error(`Missing ${GEO[which]} — run the Build District GeoJSON workflow`);
    echarts.registerMap("or_" + which, await resp.json());
    registered[which] = true;
  }

  const districts = mapData[which] || {};
  const total = which === "house" ? 60 : 30;
  const series = [];
  for (let d = 1; d <= total; d++) {
    const entry = districts[String(d)];
    series.push({ name: String(d), value: entry ? entry.total_raised : null });
  }
  const max = Math.max(1, ...series.map(s => s.value || 0));

  chart.setOption({
    tooltip: {
      trigger: "item",
      formatter: p => {
        const entry = districts[p.name];
        const label = `${which === "house" ? "House" : "Senate"} District ${p.name}`;
        if (!entry) return `<b>${label}</b><br/>No declared candidates yet`;
        const top = entry.candidates[0];
        return `<b>${label}</b><br/>` +
          `${entry.candidates.length} candidate${entry.candidates.length > 1 ? "s" : ""} · ${fmt$(entry.total_raised)} raised<br/>` +
          (top ? `Top: ${esc(top.candidate_name || top.name)} (${fmt$(top.raised_cycle)})` : "");
      },
    },
    visualMap: {
      min: 0, max, calculable: true, orient: "horizontal",
      left: "center", bottom: 6, text: ["More raised", "Less"],
      inRange: { color: ["#e6f0fa", "#98bde0", "#3a77b3", "#1a4e80"] },
      formatter: v => "$" + (v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Math.round(v / 1e3) + "K"),
    },
    series: [{
      type: "map", map: "or_" + which, roam: true,
      nameProperty: "name",
      itemStyle: { areaColor: "#edf2f7", borderColor: "#fff" },
      emphasis: { label: { show: true }, itemStyle: { areaColor: "#f6ad55" } },
      select: { itemStyle: { areaColor: "#f6ad55" } },
      label: { show: false },
      data: series,
    }],
  }, { notMerge: true });

  $("rc-panel-title").textContent = "Select a district";
  $("rc-panel-total").textContent = "";
  $("rc-panel-body").innerHTML = '<span class="rc-empty">Click any district on the map.</span>';
}

function showDistrict(name) {
  const entry = (mapData[chamber] || {})[name];
  const label = `${chamber === "house" ? "House" : "Senate"} District ${name}`;
  $("rc-panel-title").textContent = label;
  if (!entry) {
    $("rc-panel-total").textContent = "";
    $("rc-panel-body").innerHTML = '<span class="rc-empty">No declared candidates with an upcoming election on file.</span>';
    return;
  }
  $("rc-panel-total").textContent =
    `${entry.candidates.length} candidate${entry.candidates.length > 1 ? "s" : ""} · ${fmt$(entry.total_raised)} raised this cycle`;
  $("rc-panel-body").innerHTML = entry.candidates.map(c => {
    // Candidates on the ballot with no committee on file: show them (that fact
    // is informative) but with no link and no money.
    const name = esc(c.candidate_name || c.name || "");
    const nameHtml = c.slug
      ? `<a href="/" data-slug="${esc(c.slug)}">${name}</a>`
      : `<span class="rc-no-cmte-name">${name}</span>`;
    const sub = c.slug
      ? `${esc(c.name || "")}${c.election ? " · " + esc(c.election) : ""}`
      : "No committee on file";
    const nums = c.slug
      ? `<span>Raised: <b>${fmt$(c.raised_cycle)}</b></span>
         <span>Cash on hand: <b>${fmt$(c.cash_on_hand)}</b></span>`
      : `<span class="rc-no-cmte">Not reporting contributions</span>`;
    return `
    <div class="rc-cand${c.slug ? "" : " rc-cand-nocmte"}">
      <div class="rc-cand-name">${nameHtml}${partyBadge(c.party)}</div>
      <div class="rc-cand-sub">${sub}</div>
      <div class="rc-cand-nums">${nums}</div>
    </div>`;
  }).join("");
  $("rc-panel-body").querySelectorAll("a[data-slug]").forEach(a =>
    a.addEventListener("click", () => sessionStorage.setItem("openFilerSlug", a.dataset.slug)));
}

async function init() {
  try {
    const snap = await DL.getBlob("activity_snapshot");
    mapData = snap.legislative_map;
    if (!mapData) throw new Error("legislative_map not in activity_snapshot yet — next daily refresh adds it");
    // Name the election the roster came from (moves primary → general on its own)
    if (mapData.election) {
      const el = document.querySelector(".provenance");
      if (el) el.textContent =
        `Legislative race map · ${mapData.election} · candidates on the ballot ` +
        `& fundraising this cycle (since Jan 2025)`;
    }
    chart = echarts.init($("rc-map"), null, { renderer: "svg" });
    chart.on("click", p => { if (p.componentType === "series") showDistrict(p.name); });
    window.addEventListener("resize", () => chart && chart.resize());
    await loadChamber("house");
    $("rc-house").onclick = () => loadChamber("house");
    $("rc-senate").onclick = () => loadChamber("senate");
    // Only offer Statewide when this ballot actually has statewide races —
    // Oregon staggers them, so 2026 has Governor only.
    const swCount = Object.keys(mapData.statewide || {}).length;
    const swBtn = $("rc-statewide");
    if (swCount) {
      swBtn.textContent = `Statewide (${swCount})`;
      swBtn.hidden = false;
      swBtn.onclick = () => loadChamber("statewide");
    }
  } catch (e) {
    $("rc-error").hidden = false;
    $("rc-error").textContent = e.message;
  }
}

document.addEventListener("DOMContentLoaded", init);
