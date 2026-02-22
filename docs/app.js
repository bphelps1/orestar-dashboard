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

// ── Filer selector ────────────────────────────────────────────────────────────

function initFilerSelector() {
  const input    = document.getElementById("filer-search-input");
  const dropdown = document.getElementById("filer-dropdown");
  const chipRow  = document.getElementById("chip-input-row");
  const clearBtn = document.getElementById("filter-clear-btn");
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

  document.getElementById("last-updated").textContent = summaryData.last_updated
    ? new Date(summaryData.last_updated).toLocaleString() : "—";

  const n = state.selectedFilers.length;

  if (n === 0) {
    renderOverviewGlobal();
  } else if (n === 1) {
    const profile = await loadFilerProfile(state.selectedFilers[0].slug);
    renderOverviewSingleFiler(profile);
  } else {
    const profiles = await Promise.all(state.selectedFilers.map(f => loadFilerProfile(f.slug)));
    renderOverviewMultiFiler(profiles);
  }
}

function renderOverviewGlobal() {
  document.getElementById("stat-contributions").textContent = fmt$(summaryData.total_contributions);
  document.getElementById("stat-expenditures").textContent  = fmt$(summaryData.total_expenditures);
  const coh = summaryData.total_contributions - summaryData.total_expenditures;
  document.getElementById("stat-cash-on-hand").textContent  = fmt$(coh);
  document.getElementById("stat-transactions").textContent  = fmtNum(summaryData.total_transactions);
  document.getElementById("stat-range").textContent =
    `${summaryData.date_range_start} – ${summaryData.date_range_end}`;

  document.getElementById("overview-donut-title").textContent = "Contributions by Donor Type";

  if (byTypeDataGlobal && byTypeDataGlobal.length) {
    makeDonutChart(
      "chart-contributor-type",
      byTypeDataGlobal.map(r => r.type),
      byTypeDataGlobal.map(r => r.total),
      "Contributor Type",
    );
  }

  document.getElementById("filer-comparison-grid").hidden = true;
}

function renderOverviewSingleFiler(profile) {
  document.getElementById("stat-contributions").textContent = fmt$(profile.total_in);
  document.getElementById("stat-expenditures").textContent  = fmt$(profile.total_out);
  document.getElementById("stat-cash-on-hand").textContent  = fmt$(profile.cash_on_hand);
  document.getElementById("stat-transactions").textContent  = fmtNum(profile.tran_count);
  document.getElementById("stat-range").textContent         = profile.name;

  document.getElementById("overview-donut-title").textContent =
    `Contributions by Donor Type — ${profile.name}`;

  if (profile.by_contributor_type && profile.by_contributor_type.length) {
    makeDonutChart(
      "chart-contributor-type",
      profile.by_contributor_type.map(r => r.type),
      profile.by_contributor_type.map(r => r.total),
      "Contributor Type",
    );
  }

  document.getElementById("filer-comparison-grid").hidden = true;
}

function renderOverviewMultiFiler(profiles) {
  document.getElementById("stat-contributions").textContent = "—";
  document.getElementById("stat-expenditures").textContent  = "—";
  document.getElementById("stat-cash-on-hand").textContent  = "—";
  document.getElementById("stat-transactions").textContent  = "—";
  document.getElementById("stat-range").textContent         = "—";

  document.getElementById("overview-donut-title").textContent =
    "Contributions by Donor Type (Combined)";

  // Merge by_contributor_type across all selected filers
  const typeMap = new Map();
  profiles.forEach(p => {
    (p.by_contributor_type || []).forEach(t => {
      typeMap.set(t.type, (typeMap.get(t.type) || 0) + t.total);
    });
  });
  const merged = Array.from(typeMap.entries())
    .map(([type, total]) => ({ type, total }))
    .sort((a, b) => b.total - a.total);

  if (merged.length) {
    makeDonutChart(
      "chart-contributor-type",
      merged.map(r => r.type),
      merged.map(r => r.total),
      "Contributor Type",
    );
  }

  // Per-filer comparison cards
  const grid = document.getElementById("filer-comparison-grid");
  grid.hidden = false;
  grid.innerHTML = profiles.map(p => `
    <div class="filer-card">
      <div class="filer-card-name">${esc(p.name)}</div>
      <div class="filer-card-stats">
        <div class="filer-card-stat-label">Contributions</div>
        <div class="filer-card-stat-value">${fmt$(p.total_in)}</div>
        <div class="filer-card-stat-label">Expenditures</div>
        <div class="filer-card-stat-value">${fmt$(p.total_out)}</div>
        <div class="filer-card-stat-label">Cash on Hand</div>
        <div class="filer-card-stat-value">${fmt$(p.cash_on_hand)}</div>
        <div class="filer-card-stat-label">Transactions</div>
        <div class="filer-card-stat-value">${fmtNum(p.tran_count)}</div>
      </div>
    </div>
  `).join("");
}

// ── Donors ────────────────────────────────────────────────────────────────────

async function loadDonors() {
  if (!donorsData) {
    donorsData = await fetchJSON(`${DATA}/top_donors.json`);
  }

  const n = state.selectedFilers.length;

  if (n === 0) {
    document.getElementById("donors-global-view").hidden     = false;
    document.getElementById("donors-multi-view").hidden      = true;
    document.getElementById("untapped-donors-section").hidden = true;
    document.getElementById("donors-view-toggle").hidden     = false;

    // Year selector — populate once
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

    // View toggle — wire up once
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

    renderGlobalDonorsView();

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

    const allTimeDonors = profile.top_donors || [];

    if (isByYear) {
      renderDonorsByYear(profile.top_donors_by_year || {}, allTimeDonors);
    } else {
      const top20 = allTimeDonors.slice(0, 20);
      makeBarChart(
        "chart-top-donors",
        top20.map(r => r.name),
        top20.map(r => r.total),
        "Total Contributions",
        "#3182ce",
      );
      buildSortableTable("table-donors", allTimeDonors, [
        { key: "name",  label: "Donor" },
        { key: "total", label: "Total ($)", fmt: fmt$, cls: "num" },
      ]);
    }

    // Untapped donors: global top donors NOT in this filer's donor list
    const filerDonorNames = new Set(allTimeDonors.map(r => r.name.toLowerCase()));
    const untapped = (donorsData.all_time || []).filter(
      r => !filerDonorNames.has(r.name.toLowerCase())
    );
    buildSortableTable("table-untapped", untapped, [
      { key: "name",  label: "Donor" },
      { key: "total", label: "Global Total ($)", fmt: fmt$, cls: "num" },
    ]);

  } else {
    // Multi-filer: pivot table
    donorsViewMode = "summary";
    document.getElementById("donors-view-toggle").hidden     = true;
    document.getElementById("donors-global-view").hidden     = true;
    document.getElementById("donors-multi-view").hidden      = false;
    document.getElementById("untapped-donors-section").hidden = true;

    const profiles = await Promise.all(state.selectedFilers.map(f => loadFilerProfile(f.slug)));

    // Build pivot: donor name → { [filerName]: total }
    const donorMap = new Map();
    profiles.forEach(profile => {
      (profile.top_donors || []).forEach(d => {
        if (!donorMap.has(d.name)) donorMap.set(d.name, {});
        donorMap.get(d.name)[profile.name] = d.total;
      });
    });

    const filerNames = profiles.map(p => p.name);
    const pivotRows = Array.from(donorMap.entries())
      .map(([name, byFiler]) => {
        const total = Object.values(byFiler).reduce((s, v) => s + v, 0);
        return { name, ...byFiler, total };
      })
      .sort((a, b) => b.total - a.total);

    // Build thead dynamically
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

function renderGlobalDonorsView() {
  const isByYear = donorsViewMode === "by-year";
  document.getElementById("donor-year-group").hidden    = isByYear;
  document.getElementById("donors-chart-box").hidden    = isByYear;
  document.getElementById("donors-summary-table").hidden = isByYear;
  document.getElementById("donors-by-year-view").hidden = !isByYear;

  if (isByYear) {
    renderDonorsByYear(donorsData.by_year, donorsData.all_time);
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

  if (n === 0) {
    chartTitle.textContent = "Top 20 Recipients (by Contributions Received)";
    tableTitle.textContent = "Top 100 Recipients";

    const sel = document.getElementById("recipient-year");
    if (!sel._listenerAttached) {
      Object.keys(recipientsData.by_year || {}).sort().reverse().forEach(yr => {
        sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`);
      });
      sel.addEventListener("change", () => renderRecipients(sel.value));
      sel._listenerAttached = true;
    }
    renderRecipients(sel.value || "all");

  } else {
    const profile = await loadFilerProfile(state.selectedFilers[0].slug);
    chartTitle.textContent = `Top Spending by ${profile.name}`;
    tableTitle.textContent = `Top Spending by ${profile.name}`;

    const rows  = profile.top_payees || [];
    const top20 = rows.slice(0, 20);
    makeBarChart(
      "chart-top-recipients",
      top20.map(r => r.name),
      top20.map(r => r.total),
      "Expenditures",
      "#dd6b20",
    );
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

  if (n === 0) {
    const sel = document.getElementById("timeline-year");
    if (!sel._listenerAttached) {
      const years = [...new Set(timelineData.map(r => r.month.slice(0, 4)))].sort();
      years.forEach(yr => sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`));
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

function renderTimelineMultiFiler(profiles) {
  // Union of all months across profiles
  const monthSet = new Set();
  profiles.forEach(p => (p.timeline || []).forEach(t => monthSet.add(t.month)));
  const months = Array.from(monthSet).sort();

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
  timeline:   loadTimeline,
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
