/**
 * app.js — ORESTAR Campaign Finance Dashboard
 *
 * Reads JSON data files from data/aggregated/ and renders:
 *   - Overview: stat cards + donut chart; per-filer or comparison views
 *   - Donors: top donors bar chart + sortable table; filer detail + untapped; pivot compare
 *   - Recipients: top recipients; per-filer top payees
 *   - Timeline: monthly line chart; multi-filer overlay
 *   - Search: fuzzy-searchable recent transactions with filer + date filters
 *
 * No build step. No framework. Works in any modern browser.
 */

"use strict";

// ── Data base path (relative to GitHub Pages root) ───────────────────────────
const DATA = "data/aggregated";

// ── Chart.js default theme ───────────────────────────────────────────────────
Chart.defaults.font.family = "system-ui, -apple-system, 'Segoe UI', sans-serif";
Chart.defaults.font.size   = 13;
Chart.defaults.color       = "#4a5568";

const PALETTE = [
  "#3182ce", "#e53e3e", "#38a169", "#d69e2e", "#805ad5",
  "#dd6b20", "#319795", "#667eea", "#f6ad55",
  "#68d391", "#63b3ed", "#fc8181", "#d6bcfa", "#fbd38d",
];

function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

// Fixed color map: base type → PALETTE color (consistent across all filter states)
const TYPE_COLOR_MAP = {};
[
  "Individual", "Business Entity", "Political Committee", "Other",
  "Labor Organization", "Candidate & Immediate Family",
  "Unregistered Committee", "Political Party Committee",
].forEach((t, i) => { TYPE_COLOR_MAP[t] = PALETTE[i]; });

function typeColor(label) {
  const base  = label.endsWith(" (out of state)") ? label.slice(0, -15) : label;
  const color = TYPE_COLOR_MAP[base] ?? PALETTE[Object.keys(TYPE_COLOR_MAP).length % PALETTE.length];
  return label.endsWith(" (out of state)") ? hexToRgba(color, 0.45) : color;
}

// ── Donut box DOM helpers ─────────────────────────────────────────────────────

function resetDonutBox() {
  const box = document.getElementById("overview-donut-box");
  box.querySelectorAll("canvas").forEach(c => { if (c._chart) { c._chart.destroy(); c._chart = null; } });
  const multiRow = box.querySelector(".multi-donut-row");
  if (multiRow) multiRow.remove();
  const legend = box.querySelector(".multi-donut-legend");
  if (legend) legend.remove();
  if (!document.getElementById("chart-contributor-type")) {
    const canvas = document.createElement("canvas");
    canvas.id = "chart-contributor-type";
    box.appendChild(canvas);
  }
}

function buildMultiDonutBox(profiles) {
  const box = document.getElementById("overview-donut-box");
  box.querySelectorAll("canvas").forEach(c => { if (c._chart) { c._chart.destroy(); c._chart = null; } });
  const single = document.getElementById("chart-contributor-type");
  if (single) single.remove();
  box.querySelectorAll(".multi-donut-row, .multi-donut-legend").forEach(el => el.remove());
  // Shared legend placeholder — populated by renderMultiDonutLegend after data is ready
  const legendEl = document.createElement("div");
  legendEl.className = "multi-donut-legend";
  box.appendChild(legendEl);
  const row = document.createElement("div");
  row.className = "multi-donut-row";
  profiles.forEach((p, i) => {
    const unit   = document.createElement("div");
    unit.className = "donut-unit";
    const title  = document.createElement("div");
    title.className = "donut-unit-title";
    title.textContent = p.name;
    const canvas = document.createElement("canvas");
    canvas.id = `chart-type-${i}`;
    unit.appendChild(title);
    unit.appendChild(canvas);
    row.appendChild(unit);
  });
  box.appendChild(row);
}

function renderMultiDonutLegend(types) {
  const el = document.querySelector("#overview-donut-box .multi-donut-legend");
  if (!el) return;
  el.innerHTML = types.map(t => `
    <span class="mdl-item">
      <span class="mdl-swatch" style="background:${typeColor(t)}"></span>
      <span class="mdl-label">${esc(t)}</span>
    </span>`).join("");
}

// ── Donut tooltip ─────────────────────────────────────────────────────────────

function makeDonutTooltip(typeRows) {
  return function({ chart, tooltip }) {
    const el = document.getElementById("donut-tooltip");
    if (!el) return;
    if (tooltip.opacity === 0 || !tooltip.dataPoints || !tooltip.dataPoints.length) {
      el.hidden = true;
      return;
    }

    const label = tooltip.dataPoints[0].label;
    const value = tooltip.dataPoints[0].raw;
    const all   = tooltip.dataPoints[0].dataset.data;
    const total = all.reduce((s, v) => s + v, 0);
    const pct   = total > 0 ? (value / total * 100).toFixed(1) : "0.0";
    const color = typeColor(label);
    // Look up by type name rather than array index for robustness
    const row   = typeRows ? typeRows.find(r => r.type === label) : null;

    let html = `
      <div class="dt-header">
        <span class="dt-swatch" style="background:${color}"></span>
        <span class="dt-type">${esc(label)}</span>
      </div>
      <div class="dt-amount">${fmt$(value)}</div>
      <div class="dt-pct">${pct}% of total</div>`;

    if (row && row.top_donors && row.top_donors.length) {
      html += `<div class="dt-donors-label">Top donors</div>`;
      row.top_donors.slice(0, 5).forEach(d => {
        html += `<div class="dt-donor">
          <span class="dt-donor-name">${esc(d.name)}</span>
          <span class="dt-donor-amt">${fmt$(d.total)}</span>
        </div>`;
      });
    }

    el.innerHTML = html;
    el.hidden = false;

    const rect = chart.canvas.getBoundingClientRect();
    let left = rect.left + tooltip.caretX + 16;
    let top  = rect.top  + tooltip.caretY - 20;
    if (left + 240 > window.innerWidth) left = rect.left + tooltip.caretX - 256;
    top = Math.max(top, 8);
    el.style.left = left + "px";
    el.style.top  = top  + "px";
    // Clamp bottom: if tooltip extends below viewport, flip it above the cursor
    if (top + el.offsetHeight + 8 > window.innerHeight) {
      top = Math.max(8, rect.top + tooltip.caretY - el.offsetHeight - 10);
      el.style.top = top + "px";
    }
  };
}

// ── Date-range filter helpers ─────────────────────────────────────────────────

// Returns ["2020","2021",...] for the active date range, or null (= all years).
function yearsInRange() {
  const ds = state.dateStart, de = state.dateEnd;
  if (!ds && !de) return null;
  const sy = ds ? +ds.slice(0, 4) : 2000;
  const ey = de ? +de.slice(0, 4) : new Date().getFullYear();
  const out = [];
  for (let y = sy; y <= ey; y++) out.push(String(y));
  return out;
}

// Filter an array of {month:"YYYY-MM", ...} rows to the active date range.
function filterMonthRows(rows) {
  const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
  const em = state.dateEnd   ? state.dateEnd.slice(0, 7)   : null;
  if (!sm && !em) return rows;
  return rows.filter(r => (!sm || r.month >= sm) && (!em || r.month <= em));
}

// Merge per-year {name, total} arrays for the given years (null = all).
function mergeByYear(byYear, years) {
  const src = years === null ? Object.keys(byYear || {}) : years;
  const map = new Map();
  src.forEach(yr => {
    (byYear[yr] || []).forEach(d => {
      map.set(d.name, (map.get(d.name) || 0) + d.total);
    });
  });
  return [...map.entries()]
    .map(([name, total]) => ({ name, total: Math.round(total * 100) / 100 }))
    .sort((a, b) => b.total - a.total);
}

// Same as mergeByYear but uses "type" as the key (contributor-type arrays).
function mergeTypeByYear(byYear, years) {
  const src = years === null ? Object.keys(byYear || {}) : years;
  const map = new Map();
  src.forEach(yr => {
    (byYear[yr] || []).forEach(d => {
      map.set(d.type, (map.get(d.type) || 0) + d.total);
    });
  });
  return [...map.entries()]
    .map(([type, total]) => ({ type, total: Math.round(total * 100) / 100 }))
    .sort((a, b) => b.total - a.total);
}

// Sum contributions/expenditures from a (possibly filtered) timeline array.
function statsFromTimeline(rows) {
  const totalIn    = rows.reduce((s, r) => s + (r.contributions || 0), 0);
  const totalInKind = rows.reduce((s, r) => s + (r.inkind       || 0), 0);
  const totalOut   = rows.reduce((s, r) => s + (r.expenditures  || 0), 0);
  return {
    totalIn:     Math.round(totalIn    * 100) / 100,
    totalInKind: Math.round(totalInKind * 100) / 100,
    totalOut:    Math.round(totalOut   * 100) / 100,
    cashOnHand:  Math.round((totalIn - totalOut) * 100) / 100,
  };
}

// ── Global state ─────────────────────────────────────────────────────────────
const state = { selectedFilers: [], dateStart: "", dateEnd: "" };
const filerCache = {};   // slug → Promise<filerDetail>
let filerIndex = [];

// ── Cached static data (fetched once) ────────────────────────────────────────
let summaryData      = null;
let byTypeDataGlobal = null;
let donorsData       = null;
let recipientsData   = null;
let timelineData     = null;
let fuseIndex        = null;
let allRecent        = [];

// ── Active tab ───────────────────────────────────────────────────────────────
let activeTab = "overview";

// ── Donors view mode ─────────────────────────────────────────────────────────
let donorsViewMode = "summary";  // "summary" | "by-year"

// ── Utility helpers ───────────────────────────────────────────────────────────

function fmt$(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function fmtNum(n) {
  return Number(n).toLocaleString("en-US");
}

// _cbv is set once per page-load so in-session re-requests reuse the same version,
// but a fresh page load always bypasses the CDN cache to get current data.
const _cbv = Date.now();
async function fetchJSON(path) {
  const sep  = path.includes("?") ? "&" : "?";
  const resp = await fetch(`${path}${sep}_v=${_cbv}`);
  if (!resp.ok) throw new Error(`Failed to load ${path}: ${resp.status}`);
  return resp.json();
}

function showError(msg) {
  const el = document.createElement("div");
  el.className = "error-msg";
  el.textContent = "⚠ " + msg;
  document.querySelector("main").prepend(el);
}

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function loadFilerProfile(slug) {
  if (!filerCache[slug]) {
    filerCache[slug] = fetchJSON(`${DATA}/filers/${slug}.json`);
  }
  return filerCache[slug];
}

// ── Tab switching ─────────────────────────────────────────────────────────────

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
    });
    document.querySelectorAll(".tab-panel").forEach(p => {
      p.classList.remove("active");
      p.hidden = true;
    });
    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
    activeTab = btn.dataset.tab;
    const panel = document.getElementById("tab-" + activeTab);
    panel.classList.add("active");
    panel.hidden = false;
    renderActiveTab();
  });
});

async function renderActiveTab() {
  try {
    await loaders[activeTab]();
  } catch (err) {
    console.error(`Error loading ${activeTab}:`, err);
    showError(`Could not load data for "${activeTab}" tab. ${err.message}`);
  }
}

function onStateChange() {
  renderActiveTab();
}

// ── Chart helpers ─────────────────────────────────────────────────────────────

function makeBarChart(canvasId, labels, values, label, color = "#3182ce") {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  if (ctx._chart) ctx._chart.destroy();
  const chart = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{ label, data: values, backgroundColor: color, borderRadius: 4 }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => " " + fmt$(ctx.raw),
          },
        },
      },
      scales: {
        x: {
          ticks: { callback: v => fmt$(v) },
          grid: { color: "#e2e8f0" },
        },
        y: {
          ticks: { font: { size: 11 } },
          grid: { display: false },
        },
      },
    },
  });
  ctx._chart = chart;
  return chart;
}

function makeDonutChart(canvasId, labels, values, title, typeRows = null, showLegend = true) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  if (ctx._chart) ctx._chart.destroy();

  // Hide tooltip when cursor leaves the canvas
  if (!ctx._tooltipLeaveAttached) {
    ctx.addEventListener("mouseleave", () => {
      const el = document.getElementById("donut-tooltip");
      if (el) el.hidden = true;
    });
    ctx._tooltipLeaveAttached = true;
  }

  const chart = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: labels.map(l => typeColor(l)),
        borderWidth: 2,
        borderColor: "#fff",
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: showLegend
          ? { position: "right", labels: { font: { size: 12 }, padding: 12 } }
          : { display: false },
        tooltip: {
          enabled: false,
          external: makeDonutTooltip(typeRows),
        },
      },
    },
  });
  ctx._chart = chart;
  return chart;
}

function makeLineChart(canvasId, labels, datasets) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  if (ctx._chart) ctx._chart.destroy();
  const chart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        tooltip: {
          callbacks: { label: ctx => " " + ctx.dataset.label + ": " + fmt$(ctx.raw) },
        },
      },
      scales: {
        x: { ticks: { maxRotation: 45, font: { size: 11 } }, grid: { color: "#e2e8f0" } },
        y: { ticks: { callback: v => fmt$(v) }, grid: { color: "#e2e8f0" } },
      },
    },
  });
  ctx._chart = chart;
  return chart;
}

// ── Sortable table helper ─────────────────────────────────────────────────────

function buildSortableTable(tableId, rows, columns) {
  // columns: [{key, label, fmt, cls}]
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.querySelector("tbody");
  let sortCol = columns[columns.length - 1].key;  // default sort: last column (numeric)
  let sortDir = "desc";

  function render() {
    const sorted = [...rows].sort((a, b) => {
      let va = a[sortCol], vb = b[sortCol];
      if (typeof va === "string") va = va.toLowerCase();
      if (typeof vb === "string") vb = vb.toLowerCase();
      if (va < vb) return sortDir === "asc" ? -1 : 1;
      if (va > vb) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    tbody.innerHTML = sorted.map((row, i) => {
      const cells = columns.map(col => {
        const v = row[col.key];
        const display = col.fmt ? col.fmt(v) : (v ?? "—");
        return `<td class="${col.cls || ""}">${display}</td>`;
      }).join("");
      return `<tr><td>${i + 1}</td>${cells}</tr>`;
    }).join("");
  }

  // Header sort listeners
  table.querySelectorAll("th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (sortCol === col) {
        sortDir = sortDir === "asc" ? "desc" : "asc";
      } else {
        sortCol = col;
        sortDir = "desc";
      }
      table.querySelectorAll("th.sortable").forEach(t => {
        t.classList.remove("sort-asc", "sort-desc");
      });
      th.classList.add("sort-" + sortDir);
      render();
    });
  });

  render();
}

// ── Filer selector ────────────────────────────────────────────────────────────

function initFilerSelector() {
  const input       = document.getElementById("filer-search-input");
  const dropdown    = document.getElementById("filer-dropdown");
  const chipRow     = document.getElementById("chip-input-row");
  const clearBtn    = document.getElementById("filter-clear-btn");
  const dateStartEl = document.getElementById("date-start");
  const dateEndEl   = document.getElementById("date-end");

  const fuse = new Fuse(filerIndex, { keys: ["name"], threshold: 0.3 });

  let dropdownItems = [];
  let highlightIdx  = -1;

  function renderChips() {
    chipRow.querySelectorAll(".chip").forEach(c => c.remove());
    state.selectedFilers.forEach(entry => {
      const chip = document.createElement("div");
      chip.className = "chip";
      chip.innerHTML =
        `<span class="chip-label">${esc(entry.name)}</span>` +
        `<button class="chip-remove" data-slug="${esc(entry.slug)}" aria-label="Remove ${esc(entry.name)}">×</button>`;
      chipRow.insertBefore(chip, input);
    });
  }

  function addFiler(entry) {
    if (state.selectedFilers.find(f => f.slug === entry.slug)) return;
    state.selectedFilers.push(entry);
    renderChips();
    input.value = "";
    closeDropdown();
    updateClearBtn();
    onStateChange();
  }

  function removeFiler(slug) {
    state.selectedFilers = state.selectedFilers.filter(f => f.slug !== slug);
    renderChips();
    updateClearBtn();
    onStateChange();
  }

  function openDropdown(items) {
    dropdownItems = items;
    highlightIdx  = -1;
    if (items.length) {
      dropdown.innerHTML = items.map((item, i) =>
        `<li role="option" data-slug="${esc(item.slug)}" data-idx="${i}">${esc(item.name)}</li>`
      ).join("");
    } else {
      dropdown.innerHTML = `<li class="no-results">No results</li>`;
    }
    dropdown.hidden = false;
    document.getElementById("filer-combobox").setAttribute("aria-expanded", "true");
  }

  function closeDropdown() {
    dropdown.hidden = true;
    document.getElementById("filer-combobox").setAttribute("aria-expanded", "false");
    highlightIdx = -1;
  }

  function updateHighlight() {
    dropdown.querySelectorAll("li[data-slug]").forEach((li, i) => {
      li.classList.toggle("selected", i === highlightIdx);
    });
  }

  function updateClearBtn() {
    clearBtn.hidden = !(state.selectedFilers.length > 0 || state.dateStart || state.dateEnd);
  }

  input.addEventListener("focus", () => {
    const q = input.value.trim();
    const results = q
      ? fuse.search(q).map(r => r.item).slice(0, 20)
      : filerIndex.slice(0, 20);
    openDropdown(results);
  });

  input.addEventListener("input", () => {
    const q = input.value.trim();
    const results = q
      ? fuse.search(q).map(r => r.item).slice(0, 20)
      : filerIndex.slice(0, 20);
    openDropdown(results);
  });

  input.addEventListener("blur", () => {
    setTimeout(closeDropdown, 150);
  });

  input.addEventListener("keydown", e => {
    const items = dropdown.querySelectorAll("li[data-slug]");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      highlightIdx = Math.min(highlightIdx + 1, items.length - 1);
      updateHighlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      highlightIdx = Math.max(highlightIdx - 1, 0);
      updateHighlight();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlightIdx >= 0 && dropdownItems[highlightIdx]) {
        addFiler(dropdownItems[highlightIdx]);
      }
    } else if (e.key === "Escape") {
      closeDropdown();
    } else if (e.key === "Backspace" && input.value === "" && state.selectedFilers.length > 0) {
      const last = state.selectedFilers[state.selectedFilers.length - 1];
      removeFiler(last.slug);
    }
  });

  dropdown.addEventListener("mousedown", e => {
    const li = e.target.closest("li[data-slug]");
    if (!li) return;
    e.preventDefault();
    const idx = parseInt(li.dataset.idx);
    if (!isNaN(idx) && dropdownItems[idx]) {
      addFiler(dropdownItems[idx]);
    }
  });

  chipRow.addEventListener("click", e => {
    const btn = e.target.closest(".chip-remove");
    if (!btn) return;
    removeFiler(btn.dataset.slug);
  });

  // Date inputs update state but don't trigger a re-render — use Apply for that
  dateStartEl.addEventListener("change", () => {
    state.dateStart = dateStartEl.value;
    updateClearBtn();
  });

  dateEndEl.addEventListener("change", () => {
    state.dateEnd = dateEndEl.value;
    updateClearBtn();
  });

  document.getElementById("filter-apply-btn").addEventListener("click", () => {
    onStateChange();
  });

  clearBtn.addEventListener("click", () => {
    state.selectedFilers = [];
    state.dateStart = "";
    state.dateEnd   = "";
    dateStartEl.value = "";
    dateEndEl.value   = "";
    renderChips();
    updateClearBtn();
    onStateChange();
  });
}

// ── Overview ──────────────────────────────────────────────────────────────────

async function loadOverview() {
  if (!summaryData) {
    [summaryData, byTypeDataGlobal] = await Promise.all([
      fetchJSON(`${DATA}/summary.json`),
      fetchJSON(`${DATA}/by_contributor_type.json`),
    ]);
  }
  if (!timelineData) {
    timelineData = await fetchJSON(`${DATA}/timeline.json`);
  }

  document.getElementById("last-updated").textContent = summaryData.last_updated
    ? new Date(summaryData.last_updated).toLocaleString() : "—";

  const n = state.selectedFilers.length;

  if (n === 0) {
    renderOverviewGlobal();
    await loadTimeline();
  } else if (n === 1) {
    const profile = await loadFilerProfile(state.selectedFilers[0].slug);
    renderOverviewSingleFiler(profile);
    await loadTimeline();
  } else {
    const profiles = await Promise.all(state.selectedFilers.map(f => loadFilerProfile(f.slug)));
    renderOverviewMultiFiler(profiles);
  }
}

function renderOverviewGlobal() {
  document.getElementById("stat-contributions").textContent = fmt$(summaryData.total_contributions);
  document.getElementById("stat-inkind").textContent        = fmt$(summaryData.total_inkind || 0);
  document.getElementById("stat-expenditures").textContent  = fmt$(summaryData.total_expenditures);
  const coh = summaryData.total_contributions - summaryData.total_expenditures;
  document.getElementById("stat-cash-on-hand").textContent  = fmt$(coh);
  document.getElementById("stat-transactions").textContent  = fmtNum(summaryData.total_transactions);
  document.getElementById("stat-range").textContent =
    `${summaryData.date_range_start} – ${summaryData.date_range_end}`;

  document.getElementById("stat-cards").hidden             = false;
  document.getElementById("filer-comparison-grid").hidden  = true;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent = "Contributions by Donor Type";

  // byTypeDataGlobal is now {all_time:[...], by_year:{...}}; handle old flat-array format too
  const years = yearsInRange();
  let byTypeRows;
  if (Array.isArray(byTypeDataGlobal)) {
    byTypeRows = byTypeDataGlobal; // old cached format — show as-is
  } else if (byTypeDataGlobal) {
    byTypeRows = years
      ? mergeTypeByYear(byTypeDataGlobal.by_year || {}, years)
      : (byTypeDataGlobal.all_time || []);
  } else {
    byTypeRows = [];
  }
  resetDonutBox();
  if (byTypeRows.length) {
    // all_time rows carry top_donors; year-filtered rows do not
    const tooltipRows = years ? null : byTypeRows;
    makeDonutChart(
      "chart-contributor-type",
      byTypeRows.map(r => r.type),
      byTypeRows.map(r => r.total),
      "Contributor Type",
      tooltipRows,
    );
  }

  document.getElementById("filer-comparison-grid").hidden = true;
}

function renderOverviewSingleFiler(profile) {
  document.getElementById("stat-contributions").textContent = fmt$(profile.total_in);
  document.getElementById("stat-inkind").textContent        = fmt$(profile.total_inkind || 0);
  document.getElementById("stat-expenditures").textContent  = fmt$(profile.total_out);
  document.getElementById("stat-cash-on-hand").textContent  = fmt$(profile.cash_on_hand);
  document.getElementById("stat-transactions").textContent  = fmtNum(profile.tran_count);
  document.getElementById("stat-range").textContent         = profile.name;

  document.getElementById("stat-cards").hidden             = false;
  document.getElementById("filer-comparison-grid").hidden  = true;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent =
    `Contributions by Donor Type — ${profile.name}`;

  const years = yearsInRange();
  const byTypeRows = years
    ? mergeTypeByYear(profile.by_contributor_type_by_year || {}, years)
    : (profile.by_contributor_type || []);
  resetDonutBox();
  if (byTypeRows.length) {
    // all_time (no year filter) carries top_donors; year-filtered rows do not
    const tooltipRows = years ? null : byTypeRows;
    makeDonutChart(
      "chart-contributor-type",
      byTypeRows.map(r => r.type),
      byTypeRows.map(r => r.total),
      "Contributor Type",
      tooltipRows,
    );
  }

  document.getElementById("filer-comparison-grid").hidden = true;
}

function renderOverviewMultiFiler(profiles) {
  document.getElementById("stat-cards").hidden             = true;
  document.getElementById("filer-comparison-grid").hidden  = false;
  document.getElementById("overview-donut-box").hidden     = true;
  document.getElementById("overview-timeline-box").hidden  = true;

  document.getElementById("overview-donut-title").textContent =
    "Contributions by Donor Type";

  const years = yearsInRange();

  // Build one donut per filer, side by side, with a single shared legend
  buildMultiDonutBox(profiles);

  // Collect all unique types across filers (preserving order by first appearance)
  const legendTypes = [];
  const perFilerRows = profiles.map(p => years
    ? mergeTypeByYear(p.by_contributor_type_by_year || {}, years)
    : (p.by_contributor_type || []));
  perFilerRows.forEach(rows => {
    rows.forEach(r => { if (!legendTypes.includes(r.type)) legendTypes.push(r.type); });
  });
  renderMultiDonutLegend(legendTypes);

  profiles.forEach((p, i) => {
    const byTypeRows = perFilerRows[i];
    if (byTypeRows.length) {
      const tooltipRows = years ? null : byTypeRows;
      makeDonutChart(
        `chart-type-${i}`,
        byTypeRows.map(r => r.type),
        byTypeRows.map(r => r.total),
        p.name,
        tooltipRows,
        false, // legend shown separately via renderMultiDonutLegend
      );
    }
  });

  document.getElementById("filer-comparison-grid").innerHTML = profiles.map(p => `
    <div class="filer-card">
      <div class="filer-card-name">${esc(p.name)}</div>
      <div class="filer-card-stats">
        <div class="filer-card-stat-label">Cash Contributions</div>
        <div class="filer-card-stat-value">${fmt$(p.total_in)}</div>
        <div class="filer-card-stat-label">In-Kind Received</div>
        <div class="filer-card-stat-value">${fmt$(p.total_inkind || 0)}</div>
        <div class="filer-card-stat-label">Total Expenditures</div>
        <div class="filer-card-stat-value">${fmt$(p.total_out)}</div>
        <div class="filer-card-stat-label">Cash on Hand</div>
        <div class="filer-card-stat-value">${fmt$(p.cash_on_hand)}</div>
        <div class="filer-card-stat-label">Total Transactions</div>
        <div class="filer-card-stat-value">${fmtNum(p.tran_count)}</div>
      </div>
    </div>`).join("");
}

// ── Donors ────────────────────────────────────────────────────────────────────

async function loadDonors() {
  if (!donorsData) {
    donorsData = await fetchJSON(`${DATA}/top_donors.json`);
  }

  const n = state.selectedFilers.length;
  const years = yearsInRange();

  if (n === 0) {
    document.getElementById("donors-global-view").hidden      = false;
    document.getElementById("donors-multi-view").hidden       = true;
    document.getElementById("untapped-donors-section").hidden = true;
    document.getElementById("donors-view-toggle").hidden      = false;

    // Hide year selector when date range is active (date range takes precedence)
    document.getElementById("donor-year-group").hidden = donorsViewMode === "by-year" || !!years;

    const sel = document.getElementById("donor-year");
    if (!sel._listenerAttached) {
      Object.keys(donorsData.by_year || {}).sort().reverse().forEach(yr => {
        sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`);
      });
      sel.addEventListener("change", () => {
        if (donorsViewMode === "summary") renderDonors(sel.value);
      });
      sel._listenerAttached = true;
    }

    const toggleBtns = document.querySelectorAll("#donors-view-toggle .toggle-btn");
    if (!toggleBtns[0]._listenerAttached) {
      toggleBtns.forEach(btn => {
        btn.addEventListener("click", () => {
          toggleBtns.forEach(b => b.classList.remove("active"));
          btn.classList.add("active");
          donorsViewMode = btn.dataset.view;
          renderActiveTab();
        });
        btn._listenerAttached = true;
      });
    }

    renderGlobalDonorsView(years);

  } else if (n === 1) {
    const profile = await loadFilerProfile(state.selectedFilers[0].slug);
    document.getElementById("donors-global-view").hidden      = false;
    document.getElementById("donors-multi-view").hidden       = true;
    document.getElementById("untapped-donors-section").hidden = false;
    document.getElementById("donors-view-toggle").hidden      = false;
    document.getElementById("donor-year-group").hidden        = true;

    const isByYear = donorsViewMode === "by-year";
    document.getElementById("donors-chart-box").hidden      = isByYear;
    document.getElementById("donors-summary-table").hidden  = isByYear;
    document.getElementById("donors-by-year-view").hidden   = !isByYear;

    // Date-filtered or all-time donor list
    const allTimeDonors = years
      ? mergeByYear(profile.top_donors_by_year || {}, years).slice(0, 50)
      : (profile.top_donors || []);

    if (isByYear) {
      const relevantYears = years || Object.keys(profile.top_donors_by_year || {});
      const filteredByYear = {};
      relevantYears.forEach(yr => {
        if ((profile.top_donors_by_year || {})[yr]) filteredByYear[yr] = profile.top_donors_by_year[yr];
      });
      renderDonorsByYear(filteredByYear, allTimeDonors);
    } else {
      const top20 = allTimeDonors.slice(0, 20);
      makeBarChart("chart-top-donors",
        top20.map(r => r.name), top20.map(r => r.total),
        "Total Contributions", "#3182ce");
      buildSortableTable("table-donors", allTimeDonors, [
        { key: "name",  label: "Donor" },
        { key: "total", label: "Total ($)", fmt: fmt$, cls: "num" },
      ]);
    }

    // Untapped: global top donors (date-filtered) not in this filer's donor list
    const globalDonors = years
      ? mergeByYear(donorsData.by_year || {}, years)
      : (donorsData.all_time || []);
    const filerDonorNames = new Set(allTimeDonors.map(r => r.name.toLowerCase()));
    const untapped = globalDonors.filter(r => !filerDonorNames.has(r.name.toLowerCase()));
    buildSortableTable("table-untapped", untapped, [
      { key: "name",  label: "Donor" },
      { key: "total", label: "Global Total ($)", fmt: fmt$, cls: "num" },
    ]);

  } else {
    // Multi-filer: pivot table
    donorsViewMode = "summary";
    document.getElementById("donors-view-toggle").hidden      = true;
    document.getElementById("donors-global-view").hidden      = true;
    document.getElementById("donors-multi-view").hidden       = false;
    document.getElementById("untapped-donors-section").hidden = true;

    const profiles = await Promise.all(state.selectedFilers.map(f => loadFilerProfile(f.slug)));

    const donorMap = new Map();
    profiles.forEach(profile => {
      const donors = years
        ? mergeByYear(profile.top_donors_by_year || {}, years).slice(0, 50)
        : (profile.top_donors || []);
      donors.forEach(d => {
        if (!donorMap.has(d.name)) donorMap.set(d.name, {});
        donorMap.get(d.name)[profile.name] = d.total;
      });
    });

    const filerNames = profiles.map(p => p.name);
    const pivotRows = [...donorMap.entries()]
      .map(([name, byFiler]) => {
        const total = Object.values(byFiler).reduce((s, v) => s + v, 0);
        return { name, ...byFiler, total };
      })
      .sort((a, b) => b.total - a.total);

    const thead = document.getElementById("donors-multi-thead");
    thead.innerHTML = `<tr>
      <th>#</th>
      <th>Donor</th>
      ${filerNames.map(n => `<th class="num">${esc(n)}</th>`).join("")}
      <th class="num">Total ($)</th>
    </tr>`;

    document.getElementById("donors-multi-title").textContent =
      `Donor Comparison: ${filerNames.join(" vs ")}`;

    const tbody = document.querySelector("#table-donors-multi tbody");
    tbody.innerHTML = pivotRows.map((row, i) => `<tr>
      <td>${i + 1}</td>
      <td>${esc(row.name)}</td>
      ${filerNames.map(n => `<td class="num">${row[n] !== undefined ? fmt$(row[n]) : "—"}</td>`).join("")}
      <td class="num">${fmt$(row.total)}</td>
    </tr>`).join("");
  }
}

function renderGlobalDonorsView(years) {
  const isByYear = donorsViewMode === "by-year";
  document.getElementById("donor-year-group").hidden    = isByYear || !!years;
  document.getElementById("donors-chart-box").hidden    = isByYear;
  document.getElementById("donors-summary-table").hidden = isByYear;
  document.getElementById("donors-by-year-view").hidden = !isByYear;

  if (isByYear) {
    const relevantYears = years || Object.keys(donorsData.by_year || {});
    const filteredByYear = {};
    relevantYears.forEach(yr => {
      if (donorsData.by_year[yr]) filteredByYear[yr] = donorsData.by_year[yr];
    });
    renderDonorsByYear(filteredByYear, mergeByYear(donorsData.by_year, years));
  } else if (years) {
    const rows  = mergeByYear(donorsData.by_year, years);
    const top20 = rows.slice(0, 20);
    makeBarChart("chart-top-donors",
      top20.map(r => r.name), top20.map(r => r.total),
      "Total Contributions", "#3182ce");
    buildSortableTable("table-donors", rows, [
      { key: "name",  label: "Donor" },
      { key: "total", label: "Total ($)", fmt: fmt$, cls: "num" },
    ]);
  } else {
    const sel = document.getElementById("donor-year");
    renderDonors(sel.value || "all");
  }
}

function renderDonorsByYear(byYear, allTime) {
  const years = Object.keys(byYear || {}).sort();

  // Build map: donor name → { [year]: total }
  const donorYearMap = new Map();
  years.forEach(yr => {
    (byYear[yr] || []).forEach(d => {
      if (!donorYearMap.has(d.name)) donorYearMap.set(d.name, {});
      donorYearMap.get(d.name)[yr] = d.total;
    });
  });

  // Rows from allTime (sorted by total desc); fill in per-year from map
  const rows = (allTime || []).map(d => ({
    name: d.name,
    total: d.total,
    ...Object.fromEntries(years.map(yr => [yr, donorYearMap.get(d.name)?.[yr] ?? null])),
  }));

  let sortCol = "total";
  let sortDir = "desc";

  const thead = document.getElementById("donors-by-year-thead");
  thead.innerHTML = `<tr>
    <th>#</th>
    <th class="sortable" data-col="name">Donor</th>
    ${years.map(yr => `<th class="num sortable" data-col="${yr}">${yr}</th>`).join("")}
    <th class="num sortable" data-col="total">All Time</th>
  </tr>`;

  function renderByYearTable() {
    const sorted = [...rows].sort((a, b) => {
      const va = a[sortCol] ?? -1;
      const vb = b[sortCol] ?? -1;
      if (sortCol === "name") return sortDir === "asc"
        ? a.name.localeCompare(b.name)
        : b.name.localeCompare(a.name);
      return sortDir === "asc" ? va - vb : vb - va;
    });
    const tbody = document.querySelector("#table-donors-by-year tbody");
    tbody.innerHTML = sorted.map((row, i) => `<tr>
      <td>${i + 1}</td>
      <td>${esc(row.name)}</td>
      ${years.map(yr => `<td class="num">${row[yr] != null ? fmt$(row[yr]) : "—"}</td>`).join("")}
      <td class="num">${fmt$(row.total)}</td>
    </tr>`).join("");
  }

  // Sort listeners (thead is rebuilt each call so no accumulation)
  thead.querySelectorAll("th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      sortDir = sortCol === col && sortDir === "desc" ? "asc" : "desc";
      sortCol = col;
      thead.querySelectorAll("th.sortable").forEach(t => t.classList.remove("sort-asc", "sort-desc"));
      th.classList.add("sort-" + sortDir);
      renderByYearTable();
    });
  });
  thead.querySelector(`th[data-col="total"]`).classList.add("sort-desc");

  renderByYearTable();
}

function renderDonors(year) {
  const rows = year === "all"
    ? donorsData.all_time
    : (donorsData.by_year[year] || []);

  const top20 = rows.slice(0, 20);
  makeBarChart(
    "chart-top-donors",
    top20.map(r => r.name),
    top20.map(r => r.total),
    "Total Contributions",
    "#3182ce",
  );

  buildSortableTable("table-donors", rows, [
    { key: "name",  label: "Donor" },
    { key: "total", label: "Total ($)", fmt: fmt$, cls: "num" },
  ]);
}

// ── Recipients ────────────────────────────────────────────────────────────────

async function loadRecipients() {
  if (!recipientsData) {
    recipientsData = await fetchJSON(`${DATA}/top_recipients.json`);
  }

  const chartTitle = document.getElementById("recipients-chart-title");
  const tableTitle = document.getElementById("recipients-table-title");
  const n = state.selectedFilers.length;
  const years = yearsInRange();

  if (n === 0) {
    chartTitle.textContent = "Top 20 Recipients (by Contributions Received)";
    tableTitle.textContent = "Top 100 Recipients";

    // Hide year selector when date range is active
    document.getElementById("recipient-year-group").hidden = !!years;

    const sel = document.getElementById("recipient-year");
    if (!sel._listenerAttached) {
      Object.keys(recipientsData.by_year || {}).sort().reverse().forEach(yr => {
        sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`);
      });
      sel.addEventListener("change", () => renderRecipients(sel.value));
      sel._listenerAttached = true;
    }

    if (years) {
      const rows  = mergeByYear(recipientsData.by_year, years);
      const top20 = rows.slice(0, 20);
      makeBarChart("chart-top-recipients",
        top20.map(r => r.name), top20.map(r => r.total),
        "Total Received", "#38a169");
      buildSortableTable("table-recipients", rows, [
        { key: "name",  label: "Committee / Candidate" },
        { key: "total", label: "Total Received ($)", fmt: fmt$, cls: "num" },
      ]);
    } else {
      renderRecipients(sel.value || "all");
    }

  } else {
    const profile = await loadFilerProfile(state.selectedFilers[0].slug);
    chartTitle.textContent = `Top Spending by ${profile.name}`;
    tableTitle.textContent = `Top Spending by ${profile.name}`;

    const rows = years
      ? mergeByYear(profile.top_payees_by_year || {}, years).slice(0, 50)
      : (profile.top_payees || []);
    const top20 = rows.slice(0, 20);
    makeBarChart("chart-top-recipients",
      top20.map(r => r.name), top20.map(r => r.total),
      "Expenditures", "#dd6b20");
    buildSortableTable("table-recipients", rows, [
      { key: "name",  label: "Payee" },
      { key: "total", label: "Total Paid ($)", fmt: fmt$, cls: "num" },
    ]);
  }
}

function renderRecipients(year) {
  const rows = year === "all"
    ? recipientsData.all_time
    : (recipientsData.by_year[year] || []);

  const top20 = rows.slice(0, 20);
  makeBarChart(
    "chart-top-recipients",
    top20.map(r => r.name),
    top20.map(r => r.total),
    "Total Received",
    "#38a169",
  );

  buildSortableTable("table-recipients", rows, [
    { key: "name",  label: "Committee / Candidate" },
    { key: "total", label: "Total Received ($)", fmt: fmt$, cls: "num" },
  ]);
}

// ── Timeline ──────────────────────────────────────────────────────────────────

async function loadTimeline() {
  if (!timelineData) {
    timelineData = await fetchJSON(`${DATA}/timeline.json`);
  }

  const n = state.selectedFilers.length;
  const hasDate = state.dateStart || state.dateEnd;

  if (n === 0) {
    // Hide year filter when date range is active
    document.getElementById("timeline-year-group").hidden = !!hasDate;

    const sel = document.getElementById("timeline-year");
    if (!sel._listenerAttached) {
      const allYears = [...new Set(timelineData.map(r => r.month.slice(0, 4)))].sort();
      allYears.forEach(yr => sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`));
      sel.addEventListener("change", () => renderTimeline(sel.value));
      sel._listenerAttached = true;
    }
    renderTimeline(sel.value || "all");

  } else {
    const profiles = await Promise.all(state.selectedFilers.map(f => loadFilerProfile(f.slug)));
    renderTimelineMultiFiler(profiles);
  }
}

function renderTimeline(year) {
  // Date range takes precedence over year dropdown
  let rows;
  if (state.dateStart || state.dateEnd) {
    rows = filterMonthRows(timelineData);
  } else {
    rows = year === "all"
      ? timelineData
      : timelineData.filter(r => r.month.startsWith(year));
  }

  makeLineChart(
    "chart-timeline",
    rows.map(r => r.month),
    [
      {
        label: "Contributions",
        data: rows.map(r => r.contributions || 0),
        borderColor: "#3182ce",
        backgroundColor: "rgba(49,130,206,0.08)",
        fill: true,
        tension: 0.3,
        pointRadius: rows.length > 60 ? 0 : 3,
      },
      {
        label: "Expenditures",
        data: rows.map(r => r.expenditures || 0),
        borderColor: "#e53e3e",
        backgroundColor: "rgba(229,62,62,0.08)",
        fill: true,
        tension: 0.3,
        pointRadius: rows.length > 60 ? 0 : 3,
      },
    ],
  );
}

function renderTimelineMultiFiler(profiles) {
  // Union of all months across profiles, filtered to date range
  const monthSet = new Set();
  profiles.forEach(p => (p.timeline || []).forEach(t => monthSet.add(t.month)));
  const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
  const em = state.dateEnd   ? state.dateEnd.slice(0, 7)   : null;
  const months = [...monthSet].sort().filter(m => (!sm || m >= sm) && (!em || m <= em));

  const datasets = [];
  profiles.forEach((profile, idx) => {
    const color  = PALETTE[idx % PALETTE.length];
    const byMonth = new Map((profile.timeline || []).map(t => [t.month, t]));

    datasets.push({
      label: `${profile.name} (Contributions)`,
      data: months.map(m => (byMonth.get(m) || {}).contributions || 0),
      borderColor: color,
      backgroundColor: "transparent",
      fill: false,
      tension: 0.3,
      pointRadius: months.length > 60 ? 0 : 3,
    });
    datasets.push({
      label: `${profile.name} (Expenditures)`,
      data: months.map(m => (byMonth.get(m) || {}).expenditures || 0),
      borderColor: color,
      backgroundColor: "transparent",
      borderDash: [5, 5],
      fill: false,
      tension: 0.3,
      pointRadius: months.length > 60 ? 0 : 3,
    });
  });

  makeLineChart("chart-timeline", months, datasets);
}

// ── Search ────────────────────────────────────────────────────────────────────

async function loadSearch() {
  if (!allRecent.length) {
    allRecent = await fetchJSON(`${DATA}/recent_transactions.json`);
    fuseIndex = new Fuse(allRecent, {
      keys: ["contributor_payee", "filer", "amount", "purpose"],
      threshold: 0.35,
      includeScore: false,
    });
  }

  const input   = document.getElementById("search-input");
  const typeEl  = document.getElementById("search-type");
  const clearEl = document.getElementById("search-clear");

  if (!input._listenerAttached) {
    input.addEventListener("input", applySearchFilters);
    typeEl.addEventListener("change", applySearchFilters);
    clearEl.addEventListener("click", () => {
      input.value = "";
      typeEl.value = "";
      applySearchFilters();
    });
    input._listenerAttached = true;
  }

  applySearchFilters();
}

function applySearchFilters() {
  const input  = document.getElementById("search-input");
  const typeEl = document.getElementById("search-type");
  const q      = input ? input.value.trim() : "";
  const type   = typeEl ? typeEl.value : "";

  let results = q && fuseIndex
    ? fuseIndex.search(q).map(r => r.item)
    : [...allRecent];

  if (type) {
    results = results.filter(r => (r.tran_type || "").trim().toUpperCase() === type);
  }

  // Filer filter
  if (state.selectedFilers.length > 0) {
    const selectedNames = new Set(state.selectedFilers.map(f => f.name.toLowerCase()));
    results = results.filter(r => selectedNames.has((r.filer || "").toLowerCase()));
  }

  // Date range filter
  if (state.dateStart) {
    results = results.filter(r => r.filed_date && r.filed_date >= state.dateStart);
  }
  if (state.dateEnd) {
    results = results.filter(r => r.filed_date && r.filed_date <= state.dateEnd);
  }

  document.getElementById("search-count").textContent = fmtNum(results.length);
  renderSearchResults(results);
}

function renderSearchResults(rows) {
  const tbody = document.querySelector("#table-search tbody");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#718096;padding:24px">No transactions found.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.slice(0, 2000).map(r => {
    const type  = (r.tran_type || "").trim().toUpperCase();
    const badge = `<span class="badge badge-${type}">${type === "C" ? "Contribution" : type === "E" ? "Expenditure" : type}</span>`;
    return `<tr>
      <td>${r.filed_date || "—"}</td>
      <td>${badge}</td>
      <td>${esc(r.contributor_payee || "")}</td>
      <td>${esc(r.filer || "")}</td>
      <td class="num">${fmt$(r.amount)}</td>
      <td>${esc(r.purpose || "")}</td>
    </tr>`;
  }).join("");
}

// ── Tab loaders map ───────────────────────────────────────────────────────────

const loaders = {
  overview:   loadOverview,
  donors:     loadDonors,
  recipients: loadRecipients,
  search:     loadSearch,
};

// ── Init ──────────────────────────────────────────────────────────────────────

(async function init() {
  try {
    filerIndex = await fetchJSON(`${DATA}/filer_index.json`);
  } catch (e) {
    filerIndex = [];
  }
  initFilerSelector();
  renderActiveTab();
})();
