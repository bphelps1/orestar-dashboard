/**
 * app.js — ORESTAR Campaign Finance Dashboard
 *
 * Reads JSON data files from ../data/aggregated/ and renders:
 *   - Overview: stat cards + donut charts
 *   - Donors: top donors bar chart + sortable table
 *   - Recipients: top recipients bar chart + sortable table
 *   - Timeline: monthly line chart
 *   - Search: fuzzy-searchable recent transactions table
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
  "#dd6b20", "#319795", "#e53e3e", "#667eea", "#f6ad55",
  "#68d391", "#63b3ed", "#fc8181", "#d6bcfa", "#fbd38d",
];

// ── Utility helpers ───────────────────────────────────────────────────────────

function fmt$(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function fmtNum(n) {
  return Number(n).toLocaleString("en-US");
}

async function fetchJSON(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`Failed to load ${path}: ${resp.status}`);
  return resp.json();
}

function showError(msg) {
  const el = document.createElement("div");
  el.className = "error-msg";
  el.textContent = "⚠ " + msg;
  document.querySelector("main").prepend(el);
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
    const panel = document.getElementById("tab-" + btn.dataset.tab);
    panel.classList.add("active");
    panel.hidden = false;
  });
});

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

function makeDonutChart(canvasId, labels, values, title) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return null;
  if (ctx._chart) ctx._chart.destroy();
  const chart = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: PALETTE.slice(0, labels.length),
        borderWidth: 2,
        borderColor: "#fff",
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { position: "right", labels: { font: { size: 12 }, padding: 12 } },
        tooltip: {
          callbacks: { label: ctx => " " + ctx.label + ": " + fmt$(ctx.raw) },
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

// ── Overview ──────────────────────────────────────────────────────────────────

async function loadOverview() {
  const [summary, byType, byParty, byOffice] = await Promise.all([
    fetchJSON(`${DATA}/summary.json`),
    fetchJSON(`${DATA}/by_contributor_type.json`),
    fetchJSON(`${DATA}/by_party.json`),
    fetchJSON(`${DATA}/by_office_type.json`),
  ]);

  document.getElementById("stat-contributions").textContent = fmt$(summary.total_contributions);
  document.getElementById("stat-expenditures").textContent  = fmt$(summary.total_expenditures);
  document.getElementById("stat-transactions").textContent  = fmtNum(summary.total_transactions);
  document.getElementById("stat-range").textContent =
    `${summary.date_range_start} – ${summary.date_range_end}`;
  document.getElementById("last-updated").textContent = summary.last_updated
    ? new Date(summary.last_updated).toLocaleString() : "—";

  // Contributor type donut
  if (byType.length) {
    makeDonutChart(
      "chart-contributor-type",
      byType.map(r => r.type),
      byType.map(r => r.total),
      "Contributor Type",
    );
  }

  // Party donut
  if (Array.isArray(byParty) && byParty.length) {
    makeDonutChart(
      "chart-party",
      byParty.map(r => r.party),
      byParty.map(r => r.total),
      "Party",
    );
  } else {
    document.getElementById("chart-party")?.closest(".chart-box")
      ?.insertAdjacentHTML("beforeend", '<p class="loading-msg" style="font-size:.85rem;padding:16px">No party data available</p>');
  }

  // Office type bar
  if (Array.isArray(byOffice) && byOffice.length) {
    makeBarChart(
      "chart-office-type",
      byOffice.map(r => r.office_type),
      byOffice.map(r => r.total),
      "Contributions",
      "#3182ce",
    );
  }
}

// ── Donors ────────────────────────────────────────────────────────────────────

let donorsData = null;

async function loadDonors() {
  donorsData = await fetchJSON(`${DATA}/top_donors.json`);

  // Populate year selector
  const sel = document.getElementById("donor-year");
  Object.keys(donorsData.by_year || {}).sort().reverse().forEach(yr => {
    sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`);
  });
  sel.addEventListener("change", () => renderDonors(sel.value));
  renderDonors("all");
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

let recipientsData = null;

async function loadRecipients() {
  recipientsData = await fetchJSON(`${DATA}/top_recipients.json`);

  const sel = document.getElementById("recipient-year");
  Object.keys(recipientsData.by_year || {}).sort().reverse().forEach(yr => {
    sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`);
  });
  sel.addEventListener("change", () => renderRecipients(sel.value));
  renderRecipients("all");
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

let timelineData = null;

async function loadTimeline() {
  timelineData = await fetchJSON(`${DATA}/timeline.json`);

  // Populate year selector
  const years = [...new Set(timelineData.map(r => r.month.slice(0, 4)))].sort();
  const sel = document.getElementById("timeline-year");
  years.forEach(yr => sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`));
  sel.addEventListener("change", () => renderTimeline(sel.value));
  renderTimeline("all");
}

function renderTimeline(year) {
  const rows = year === "all"
    ? timelineData
    : timelineData.filter(r => r.month.startsWith(year));

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

// ── Search ────────────────────────────────────────────────────────────────────

let fuseIndex = null;
let allRecent = [];

async function loadSearch() {
  allRecent = await fetchJSON(`${DATA}/recent_transactions.json`);
  document.getElementById("search-count").textContent = fmtNum(allRecent.length);

  fuseIndex = new Fuse(allRecent, {
    keys: ["contributor_payee", "filer", "amount", "purpose"],
    threshold: 0.35,
    includeScore: false,
  });

  renderSearchResults(allRecent);

  const input   = document.getElementById("search-input");
  const typeEl  = document.getElementById("search-type");
  const clearEl = document.getElementById("search-clear");

  function doSearch() {
    const q    = input.value.trim();
    const type = typeEl.value;
    let results = q ? fuseIndex.search(q).map(r => r.item) : [...allRecent];
    if (type) results = results.filter(r => (r.tran_type || "").trim().toUpperCase() === type);
    document.getElementById("search-count").textContent = fmtNum(results.length);
    renderSearchResults(results);
  }

  input.addEventListener("input", doSearch);
  typeEl.addEventListener("change", doSearch);
  clearEl.addEventListener("click", () => {
    input.value = "";
    typeEl.value = "";
    doSearch();
  });
}

function renderSearchResults(rows) {
  const tbody = document.querySelector("#table-search tbody");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#718096;padding:24px">No transactions found.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.slice(0, 2000).map(r => {
    const type = (r.tran_type || "").trim().toUpperCase();
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

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Lazy tab loading ──────────────────────────────────────────────────────────
// Each tab's data is loaded once on first activation.

const loaded = {};

const loaders = {
  overview:   loadOverview,
  donors:     loadDonors,
  recipients: loadRecipients,
  timeline:   loadTimeline,
  search:     loadSearch,
};

async function lazyLoad(tab) {
  if (loaded[tab]) return;
  loaded[tab] = true;
  try {
    await loaders[tab]();
  } catch (err) {
    console.error(`Error loading ${tab}:`, err);
    showError(`Could not load data for "${tab}" tab. ${err.message}`);
  }
}

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => lazyLoad(btn.dataset.tab));
});

// ── Init: load Overview on page ready ────────────────────────────────────────
lazyLoad("overview");
