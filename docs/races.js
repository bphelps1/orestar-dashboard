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

async function loadChamber(which) {
  chamber = which;
  $("rc-house").classList.toggle("active", which === "house");
  $("rc-senate").classList.toggle("active", which === "senate");

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
  $("rc-panel-body").innerHTML = entry.candidates.map(c => `
    <div class="rc-cand">
      <div class="rc-cand-name">
        <a href="/" data-slug="${esc(c.slug)}">${esc(c.candidate_name || c.name)}</a>${partyBadge(c.party)}
      </div>
      <div class="rc-cand-sub">${esc(c.name)}${c.election ? " · " + esc(c.election) : ""}</div>
      <div class="rc-cand-nums">
        <span>Raised: <b>${fmt$(c.raised_cycle)}</b></span>
        <span>Cash on hand: <b>${fmt$(c.cash_on_hand)}</b></span>
      </div>
    </div>`).join("");
  $("rc-panel-body").querySelectorAll("a[data-slug]").forEach(a =>
    a.addEventListener("click", () => sessionStorage.setItem("openFilerSlug", a.dataset.slug)));
}

async function init() {
  try {
    const snap = await DL.getBlob("activity_snapshot");
    mapData = snap.legislative_map;
    if (!mapData) throw new Error("legislative_map not in activity_snapshot yet — next daily refresh adds it");
    chart = echarts.init($("rc-map"), null, { renderer: "svg" });
    chart.on("click", p => { if (p.componentType === "series") showDistrict(p.name); });
    window.addEventListener("resize", () => chart && chart.resize());
    await loadChamber("house");
    $("rc-house").onclick = () => loadChamber("house");
    $("rc-senate").onclick = () => loadChamber("senate");
  } catch (e) {
    $("rc-error").hidden = false;
    $("rc-error").textContent = e.message;
  }
}

document.addEventListener("DOMContentLoaded", init);
