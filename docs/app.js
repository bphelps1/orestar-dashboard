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
// Merges case-insensitively so e.g. "DaVita" and "Davita" don't become
// two separate rows; the first-encountered capitalisation is used for display.
function mergeByYear(byYear, years) {
  const src = years === null ? Object.keys(byYear || {}) : years;
  const totalMap = new Map(); // lowercase key → total
  const nameMap  = new Map(); // lowercase key → display name (first seen)
  src.forEach(yr => {
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

// Same as mergeByYear but uses "type" as the key (contributor-type arrays).
// Sorts by base-type combined total so in-state/out-of-state pairs stay adjacent.
// Also merges top_donors lists across years so tooltips work for filtered views.
function mergeTypeByYear(byYear, years) {
  const src = years === null ? Object.keys(byYear || {}) : years;
  const totalMap = new Map();
  const donorMap = new Map(); // type → Map<name, total>
  src.forEach(yr => {
    (byYear[yr] || []).forEach(d => {
      totalMap.set(d.type, (totalMap.get(d.type) || 0) + d.total);
      if (d.top_donors && d.top_donors.length) {
        if (!donorMap.has(d.type)) donorMap.set(d.type, new Map());
        const dm = donorMap.get(d.type);
        d.top_donors.forEach(donor => {
          dm.set(donor.name, (dm.get(donor.name) || 0) + donor.total);
        });
      }
    });
  });
  const map = totalMap;
  const entries = [...map.entries()]
    .map(([type, total]) => {
      const obj = { type, total: Math.round(total * 100) / 100 };
      if (donorMap.has(type)) {
        obj.top_donors = [...donorMap.get(type).entries()]
          .map(([name, t]) => ({ name, total: Math.round(t * 100) / 100 }))
          .sort((a, b) => b.total - a.total)
          .slice(0, 5);
      }
      return obj;
    });
  // Compute combined total per base type so pairs sort together
  const baseTotals = new Map();
  entries.forEach(e => {
    const base = e.type.endsWith(" (out of state)") ? e.type.slice(0, -15) : e.type;
    baseTotals.set(base, (baseTotals.get(base) || 0) + e.total);
  });
  return entries.sort((a, b) => {
    const baseA = a.type.endsWith(" (out of state)") ? a.type.slice(0, -15) : a.type;
    const baseB = b.type.endsWith(" (out of state)") ? b.type.slice(0, -15) : b.type;
    const diff = (baseTotals.get(baseB) || 0) - (baseTotals.get(baseA) || 0);
    if (diff !== 0) return diff;
    // Same base: in-state before out-of-state
    return (a.type.endsWith(" (out of state)") ? 1 : 0) - (b.type.endsWith(" (out of state)") ? 1 : 0);
  });
}

// Same as mergeTypeByYear but filters by the active date range at month precision.
function mergeTypeByMonth(byMonth) {
  const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
  const em = state.dateEnd   ? state.dateEnd.slice(0, 7)   : null;
  const keys = Object.keys(byMonth || {})
    .filter(m => (!sm || m >= sm) && (!em || m <= em));
  const totalMap = new Map();
  const donorMap = new Map();
  keys.forEach(mo => {
    (byMonth[mo] || []).forEach(d => {
      totalMap.set(d.type, (totalMap.get(d.type) || 0) + d.total);
      if (d.top_donors && d.top_donors.length) {
        if (!donorMap.has(d.type)) donorMap.set(d.type, new Map());
        const dm = donorMap.get(d.type);
        d.top_donors.forEach(donor => {
          dm.set(donor.name, (dm.get(donor.name) || 0) + donor.total);
        });
      }
    });
  });
  const entries = [...totalMap.entries()]
    .map(([type, total]) => {
      const obj = { type, total: Math.round(total * 100) / 100 };
      if (donorMap.has(type)) {
        obj.top_donors = [...donorMap.get(type).entries()]
          .map(([name, t]) => ({ name, total: Math.round(t * 100) / 100 }))
          .sort((a, b) => b.total - a.total)
          .slice(0, 5);
      }
      return obj;
    });
  const baseTotals = new Map();
  entries.forEach(e => {
    const base = e.type.endsWith(" (out of state)") ? e.type.slice(0, -15) : e.type;
    baseTotals.set(base, (baseTotals.get(base) || 0) + e.total);
  });
  return entries.sort((a, b) => {
    const baseA = a.type.endsWith(" (out of state)") ? a.type.slice(0, -15) : a.type;
    const baseB = b.type.endsWith(" (out of state)") ? b.type.slice(0, -15) : b.type;
    const diff = (baseTotals.get(baseB) || 0) - (baseTotals.get(baseA) || 0);
    if (diff !== 0) return diff;
    return (a.type.endsWith(" (out of state)") ? 1 : 0) - (b.type.endsWith(" (out of state)") ? 1 : 0);
  });
}

// Sum contributions/expenditures/count from a (possibly filtered) timeline array.
// When beginningBalances is provided, cash on hand is calculated as:
//   beginning_balance[earliest_year] + contributions + other_receipts - expenditures - other_disbursements
function statsFromTimeline(rows, beginningBalances) {
  const totalIn    = rows.reduce((s, r) => s + (r.contributions || 0), 0);
  const totalInKind = rows.reduce((s, r) => s + (r.inkind       || 0), 0);
  const totalOut   = rows.reduce((s, r) => s + (r.expenditures  || 0), 0);
  const totalOR    = rows.reduce((s, r) => s + (r.other_receipts || 0), 0);
  const totalOD    = rows.reduce((s, r) => s + (r.other_disbursements || 0), 0);
  const count      = rows.reduce((s, r) => s + (r.count         || 0), 0);

  // Determine beginning balance for the earliest year in the filtered rows
  let beginBal = 0;
  if (beginningBalances && rows.length > 0) {
    const earliestMonth = rows[0].month || "";  // timeline is sorted by month
    const earliestYear = earliestMonth.slice(0, 4);
    // Find the beginning balance for this year (or the earliest available)
    if (earliestYear && beginningBalances[earliestYear] !== undefined) {
      beginBal = beginningBalances[earliestYear];
    } else {
      // Fall back to earliest available beginning balance
      const sortedYears = Object.keys(beginningBalances).sort();
      if (sortedYears.length > 0) {
        beginBal = beginningBalances[sortedYears[0]];
      }
    }
  }

  const netFlow = totalIn + totalOR - totalOut - totalOD;

  return {
    totalIn:     Math.round(totalIn    * 100) / 100,
    totalInKind: Math.round(totalInKind * 100) / 100,
    totalOut:    Math.round(totalOut   * 100) / 100,
    cashOnHand:  Math.round((beginBal + netFlow) * 100) / 100,
    count,
  };
}

// ── Shared field-metadata map (centralized tile definitions) ─────────────────
const ACCT_FIELD_META = {
  // Contributions group
  orestar_contributions: {
    label: "Cash Contributions",
    tip: "<strong>Counted:</strong> All monetary donations received.<br><strong>Condition:</strong> ORESTAR sub-type 'Cash Contribution'.<br><strong>Meaning:</strong> Direct cash and check donations to the committee.",
  },
  loans_received: {
    label: "Loans Received (non-exempt)",
    tip: "<strong>Counted:</strong> Loans received that count toward contribution limits.<br><strong>Condition:</strong> Non-exempt loans reported to ORESTAR.<br><strong>Meaning:</strong> Borrowed money that the committee must repay and that counts as a contribution.",
  },
  inkind_contributions: {
    label: "In-Kind Contributions",
    tip: "<strong>Counted:</strong> Non-cash contributions (goods, services, etc.).<br><strong>Condition:</strong> Reported as in-kind under ORESTAR contribution types.<br><strong>Meaning:</strong> Things of value given to the committee instead of cash.",
  },
  total_contributions_calc: {
    label: "Total Contributions",
    subtotal: true,
    fields: ["orestar_contributions", "loans_received", "inkind_contributions"],
    tip: "<strong>Counted:</strong> Sum of cash, loans (non-exempt), and in-kind contributions.<br><strong>Meaning:</strong> Everything received that counts toward contribution limits.",
  },
  // Expenditures group
  orestar_expenditures: {
    label: "Cash Expenditures",
    tip: "<strong>Counted:</strong> All cash payments made by the committee.<br><strong>Condition:</strong> ORESTAR sub-type 'Cash Expenditure'.<br><strong>Meaning:</strong> Money spent on campaign operations, ads, staff, etc.",
  },
  loan_payments: {
    label: "Loan Payments (non-exempt)",
    tip: "<strong>Counted:</strong> Loan repayments that count toward expenditure totals.<br><strong>Condition:</strong> Non-exempt loan payments reported to ORESTAR.<br><strong>Meaning:</strong> Money used to repay non-exempt loans.",
  },
  inkind_expenditures: {
    label: "In-Kind Expenditures",
    tip: "<strong>Counted:</strong> Non-cash expenditures (goods, services provided).<br><strong>Condition:</strong> Reported as in-kind under ORESTAR expenditure types.<br><strong>Meaning:</strong> Non-monetary costs borne by the committee.",
  },
  total_expenditures_calc: {
    label: "Total Expenditures",
    subtotal: true,
    fields: ["orestar_expenditures", "loan_payments", "inkind_expenditures"],
    tip: "<strong>Counted:</strong> Sum of cash, loan payments (non-exempt), and in-kind expenditures.<br><strong>Meaning:</strong> Everything spent that counts toward expenditure totals.",
  },
  // Cash Balance group
  beginning_balance: {
    label: "Beginning Balance (Previous Year)",
    tip: "<strong>Counted:</strong> Cash the committee had at the start of the reporting year.<br><strong>Condition:</strong> Carried over from the prior year's ending balance in ORESTAR.<br><strong>Meaning:</strong> Starting cash position for the year.",
  },
  orestar_other_receipts: {
    label: "Other Receipts",
    tip: "<strong>Counted:</strong> Non-contribution money received (refunds, interest, etc.).<br><strong>Condition:</strong> ORESTAR 'Other Receipt' types (OR, O, OA).<br><strong>Meaning:</strong> Miscellaneous income that isn't a contribution.",
  },
  loans_received_exempt: {
    label: "Loans Received (exempt)",
    tip: "<strong>Counted:</strong> Loans that do not count toward contribution limits.<br><strong>Condition:</strong> Exempt loans as classified by ORESTAR.<br><strong>Meaning:</strong> Borrowed money not subject to contribution limits.",
  },
  cash_balance_inflow_subtotal: {
    label: "Subtotal (Inflows)",
    subtotal: true,
    fields: ["beginning_balance", "orestar_contributions", "orestar_other_receipts", "loans_received_exempt"],
    tip: "<strong>Meaning:</strong> Total money available before expenditures.",
  },
  orestar_other_disbursements: {
    label: "Other Disbursements",
    tip: "<strong>Counted:</strong> Non-expenditure cash outflows.<br><strong>Condition:</strong> ORESTAR 'Other Disbursement' type (OD).<br><strong>Meaning:</strong> Miscellaneous payments that aren't standard expenditures.",
  },
  loan_payments_exempt: {
    label: "Loan Payments (exempt)",
    tip: "<strong>Counted:</strong> Repayments on exempt loans.<br><strong>Condition:</strong> Payments on loans classified as exempt by ORESTAR.<br><strong>Meaning:</strong> Money used to repay loans not subject to limits.",
  },
  cash_balance_outflow_subtotal: {
    label: "Subtotal (Outflows)",
    subtotal: true,
    fields: ["orestar_expenditures", "orestar_other_disbursements", "loan_payments_exempt"],
    tip: "<strong>Meaning:</strong> Total money paid out.",
  },
  balance_adjustments: {
    label: "Balance Adjustments",
    tip: "<strong>Counted:</strong> Manual corrections to the cash balance.<br><strong>Condition:</strong> Filed as 'Balance Adjustment' in ORESTAR.<br><strong>Meaning:</strong> Corrections for errors, write-offs, or reclassifications.",
  },
  ending_cash_balance: {
    label: "Ending Cash Balance",
    subtotal: true,
    tip: "<strong>Counted:</strong> Inflow subtotal minus outflow subtotal plus adjustments.<br><strong>Condition:</strong> From ORESTAR account summary page.<br><strong>Meaning:</strong> How much cash the committee has at year-end.",
  },
  // Financial Status group
  cash_balance_fs: {
    label: "Cash Balance",
    tip: "<strong>Counted:</strong> Current cash on hand per ORESTAR financial status.<br><strong>Condition:</strong> From ORESTAR account summary 'Financial Status' section.<br><strong>Meaning:</strong> Liquid cash the committee currently holds.",
  },
  accounts_receivable: {
    label: "Accounts Receivable",
    tip: "<strong>Counted:</strong> Money owed to the committee but not yet received.<br><strong>Condition:</strong> Outstanding receivables reported to ORESTAR.<br><strong>Meaning:</strong> Pledged or expected payments not yet collected.",
  },
  fs_assets_subtotal: {
    label: "Subtotal (Assets)",
    subtotal: true,
    fields: ["cash_balance_fs", "accounts_receivable"],
    tip: "<strong>Meaning:</strong> Total assets: cash plus receivables.",
  },
  total_outstanding_loans: {
    label: "Total Outstanding Loans",
    tip: "<strong>Counted:</strong> All unpaid loan balances.<br><strong>Condition:</strong> Loans reported in ORESTAR that haven't been fully repaid.<br><strong>Meaning:</strong> How much the committee still owes on loans.",
  },
  outstanding_personal_expenditures: {
    label: "Outstanding Personal Expenditures",
    tip: "<strong>Counted:</strong> Personal spending awaiting reimbursement.<br><strong>Condition:</strong> Expenditures made personally by a candidate/treasurer not yet repaid.<br><strong>Meaning:</strong> Personal money spent on behalf of the committee.",
  },
  accounts_payable: {
    label: "Accounts Payable",
    tip: "<strong>Counted:</strong> Bills and obligations not yet paid.<br><strong>Condition:</strong> Outstanding payables reported to ORESTAR.<br><strong>Meaning:</strong> Money the committee owes to vendors or service providers.",
  },
  fs_liabilities_subtotal: {
    label: "Subtotal (Liabilities)",
    subtotal: true,
    fields: ["total_outstanding_loans", "outstanding_personal_expenditures", "accounts_payable"],
    tip: "<strong>Meaning:</strong> Total outstanding obligations.",
  },
  balance_deficit: {
    label: "Balance / Deficit",
    subtotal: true,
    tip: "<strong>Counted:</strong> Assets minus liabilities.<br><strong>Condition:</strong> From ORESTAR account summary.<br><strong>Meaning:</strong> The committee's net financial position. Negative means deficit.",
  },
};

const ACCT_GROUPS = [
  {
    title: "Contributions",
    fields: ["orestar_contributions", "loans_received", "inkind_contributions", "total_contributions_calc"],
  },
  {
    title: "Expenditures",
    fields: ["orestar_expenditures", "loan_payments", "inkind_expenditures", "total_expenditures_calc"],
  },
  {
    title: "Cash Balance",
    fields: [
      "beginning_balance", "orestar_contributions", "orestar_other_receipts", "loans_received_exempt",
      "cash_balance_inflow_subtotal",
      "orestar_expenditures", "orestar_other_disbursements", "loan_payments_exempt",
      "cash_balance_outflow_subtotal",
      "balance_adjustments", "ending_cash_balance",
    ],
  },
  {
    title: "Financial Status",
    fields: [
      "cash_balance_fs", "accounts_receivable", "fs_assets_subtotal",
      "total_outstanding_loans", "outstanding_personal_expenditures", "accounts_payable",
      "fs_liabilities_subtotal", "balance_deficit",
    ],
  },
];

function renderAcctSummary(acctData) {
  const grid = document.getElementById("acct-summary-grid");
  const details = document.getElementById("acct-summary-details");
  if (!grid || !details) return;

  if (!acctData || !acctData.year) {
    details.hidden = true;
    return;
  }
  details.hidden = false;

  grid.innerHTML = ACCT_GROUPS.map(group => {
    const rows = group.fields.map(field => {
      const meta = ACCT_FIELD_META[field];
      if (!meta) return "";
      let val;
      if (meta.subtotal && meta.fields) {
        val = meta.fields.reduce((s, f) => s + (acctData[f] || 0), 0);
      } else {
        val = acctData[field] != null ? acctData[field] : 0;
      }
      const cls = meta.subtotal ? "acct-row acct-subtotal" : "acct-row";
      const helpBtn = meta.tip
        ? `<span class="acct-help" tabindex="0" role="button" aria-label="Info">?<span class="acct-tip">${meta.tip}</span></span>`
        : "";
      return `<div class="${cls}">
        <span class="acct-label">${esc(meta.label)} ${helpBtn}</span>
        <span class="acct-value">${fmt$(val)}</span>
      </div>`;
    }).join("");
    return `<div class="acct-group">
      <div class="acct-group-title">${esc(group.title)}</div>
      ${rows}
    </div>`;
  }).join("");
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

let donorFilerMap    = null; // donor_name_lower → {slug, name, confidence} | {candidates, confidence:"ambiguous"}

// ── Active tab ───────────────────────────────────────────────────────────────
let activeTab = "overview";

// ── Donors view mode ─────────────────────────────────────────────────────────
let donorsViewMode = "summary";  // "summary" | "by-year"

// ── Stat card auto-fit ────────────────────────────────────────────────────────
// Binary-searches for the largest font size (px) that keeps each value on one
// line within its card. Runs after the next layout frame so clientWidth is valid.
function fitStatCards() {
  requestAnimationFrame(() => {
    document.querySelectorAll('#stat-cards .card-value').forEach(el => {
      let lo = 12, hi = 26;           // search range in px
      el.style.fontSize = hi + 'px';
      if (el.scrollWidth <= el.clientWidth) return; // already fits
      while (hi - lo > 1) {
        const mid = Math.floor((lo + hi) / 2);
        el.style.fontSize = mid + 'px';
        if (el.scrollWidth <= el.clientWidth) lo = mid; else hi = mid;
      }
      el.style.fontSize = lo + 'px';
    });
  });
}

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

// ── Donor → Filer linking layer ────────────────────────────────────────────────

async function ensureDonorFilerMap() {
  if (donorFilerMap !== null) return;
  try {
    donorFilerMap = await fetchJSON(`${DATA}/donor_filer_map.json`);
  } catch {
    donorFilerMap = {};
  }
}

function lookupDonorFiler(donorName) {
  if (!donorFilerMap || !donorName) return null;
  const key = donorName.toLowerCase().trim();
  return donorFilerMap[key] || null;
}

// Build a filer index lookup by slug (for preview data)
function filerIndexBySlug() {
  const map = {};
  filerIndex.forEach(f => { map[f.slug] = f; });
  return map;
}

// Render a donor name cell, making it a link if it maps to a filer
function renderDonorCell(name) {
  const match = lookupDonorFiler(name);
  if (!match) return esc(name);

  if (match.confidence === "high") {
    return `<a class="donor-filer-link" href="#" data-slug="${esc(match.slug)}"
              data-donor="${esc(name)}" tabindex="0">${esc(name)}</a>`;
  }
  if (match.confidence === "ambiguous" && match.candidates) {
    return `<span class="donor-ambiguous" data-donor="${esc(name)}" tabindex="0"
              title="Multiple filer matches — click to choose">${esc(name)} <span class="ambig-icon">⋯</span></span>`;
  }
  return esc(name);
}

// Preview popover state
let _previewPopover = null;
let _previewTimeout = null;

function hidePreviewPopover() {
  if (_previewPopover) {
    _previewPopover.remove();
    _previewPopover = null;
  }
}

async function showPreviewPopover(slug, anchorEl) {
  hidePreviewPopover();
  const pop = document.createElement("div");
  pop.className = "donor-preview-popover";
  pop.innerHTML = '<div class="preview-loading">Loading…</div>';
  document.body.appendChild(pop);
  _previewPopover = pop;

  // Position near the anchor
  const rect = anchorEl.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.left = Math.min(rect.right + 8, window.innerWidth - 320) + "px";
  pop.style.top = Math.max(rect.top - 10, 8) + "px";

  try {
    const profile = await loadFilerProfile(slug);
    if (_previewPopover !== pop) return; // stale

    // Respect current filters
    const hasDate = state.dateStart || state.dateEnd;
    let stats;
    if (hasDate) {
      stats = statsFromTimeline(filterMonthRows(profile.timeline || []), profile.beginning_balances);
    } else {
      stats = {
        totalIn: profile.total_in,
        totalOut: profile.total_out,
        cashOnHand: profile.cash_on_hand,
      };
    }

    // Top 5 donors (respect date filter)
    const years = yearsInRange();
    const donors = years
      ? mergeByYear(profile.top_donors_by_year || {}, years).slice(0, 5)
      : (profile.top_donors || []).slice(0, 5);

    pop.innerHTML = `
      <div class="preview-header">${esc(profile.name)}</div>
      <div class="preview-stat"><span>Contributions:</span><span>${fmt$(stats.totalIn)}</span></div>
      <div class="preview-stat"><span>Expenditures:</span><span>${fmt$(stats.totalOut)}</span></div>
      <div class="preview-stat"><span>Cash on Hand:</span><span>${fmt$(stats.cashOnHand)}</span></div>
      ${donors.length ? '<div class="preview-donors-label">Top 5 Donors</div>' : ''}
      ${donors.map(d => `<div class="preview-donor"><span>${esc(d.name)}</span><span>${fmt$(d.total)}</span></div>`).join("")}
      <div class="preview-action">Click to view full details</div>
    `;

    // Reposition if it goes offscreen
    const popRect = pop.getBoundingClientRect();
    if (popRect.bottom > window.innerHeight) {
      pop.style.top = Math.max(8, window.innerHeight - popRect.height - 8) + "px";
    }
    if (popRect.right > window.innerWidth) {
      pop.style.left = Math.max(8, rect.left - popRect.width - 8) + "px";
    }
  } catch {
    if (_previewPopover === pop) {
      pop.innerHTML = '<div class="preview-loading">Could not load preview</div>';
    }
  }
}

// Ambiguous chooser popover
function showAmbiguousChooser(donorName, anchorEl) {
  hidePreviewPopover();
  const match = lookupDonorFiler(donorName);
  if (!match || match.confidence !== "ambiguous" || !match.candidates) return;

  const pop = document.createElement("div");
  pop.className = "donor-preview-popover donor-chooser";
  pop.innerHTML = `
    <div class="preview-header">Multiple filers match "${esc(donorName)}"</div>
    <div class="chooser-list">
      ${match.candidates.map(c =>
        `<button class="chooser-item" data-slug="${esc(c.slug)}">${esc(c.name)}</button>`
      ).join("")}
    </div>
    <div class="preview-action">Select a filer to view</div>
  `;
  document.body.appendChild(pop);
  _previewPopover = pop;

  const rect = anchorEl.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.left = Math.min(rect.right + 8, window.innerWidth - 280) + "px";
  pop.style.top = Math.max(rect.top - 10, 8) + "px";

  pop.addEventListener("click", e => {
    const btn = e.target.closest(".chooser-item");
    if (!btn) return;
    const slug = btn.dataset.slug;
    hidePreviewPopover();
    navigateToFiler(slug);
  });
}

function navigateToFiler(slug) {
  const entry = filerIndex.find(f => f.slug === slug);
  if (!entry) return;
  // Add this filer to selection and switch to overview
  state.selectedFilers = [entry];
  document.querySelectorAll(".chip").forEach(c => c.remove());
  const chipRow = document.getElementById("chip-input-row");
  const input = document.getElementById("filer-search-input");
  const chip = document.createElement("div");
  chip.className = "chip";
  chip.innerHTML =
    `<span class="chip-label">${esc(entry.name)}</span>` +
    `<button class="chip-remove" data-slug="${esc(entry.slug)}" aria-label="Remove ${esc(entry.name)}">×</button>`;
  chipRow.insertBefore(chip, input);
  input.value = "";
  document.getElementById("filter-clear-btn").hidden = false;
  // Switch to overview tab
  document.querySelectorAll(".tab-btn").forEach(b => {
    b.classList.remove("active");
    b.setAttribute("aria-selected", "false");
  });
  document.querySelectorAll(".tab-panel").forEach(p => {
    p.classList.remove("active");
    p.hidden = true;
  });
  const overviewBtn = document.querySelector('.tab-btn[data-tab="overview"]');
  overviewBtn.classList.add("active");
  overviewBtn.setAttribute("aria-selected", "true");
  activeTab = "overview";
  document.getElementById("tab-overview").classList.add("active");
  document.getElementById("tab-overview").hidden = false;
  renderActiveTab();
}

// Global delegated event handlers for donor links and previews
document.addEventListener("click", e => {
  // Handle donor-filer link clicks
  const link = e.target.closest(".donor-filer-link");
  if (link) {
    e.preventDefault();
    hidePreviewPopover();
    navigateToFiler(link.dataset.slug);
    return;
  }
  // Handle ambiguous donor clicks
  const ambig = e.target.closest(".donor-ambiguous");
  if (ambig) {
    e.preventDefault();
    showAmbiguousChooser(ambig.dataset.donor, ambig);
    return;
  }
  // Click outside popover dismisses it
  if (_previewPopover && !_previewPopover.contains(e.target)) {
    hidePreviewPopover();
  }
});

document.addEventListener("mouseenter", e => {
  const link = e.target.closest(".donor-filer-link");
  if (!link) return;
  clearTimeout(_previewTimeout);
  _previewTimeout = setTimeout(() => showPreviewPopover(link.dataset.slug, link), 200);
}, true);

document.addEventListener("mouseleave", e => {
  const link = e.target.closest(".donor-filer-link");
  if (!link) return;
  clearTimeout(_previewTimeout);
  // Delay hide so user can move to popover
  _previewTimeout = setTimeout(() => {
    if (_previewPopover && !_previewPopover.matches(":hover")) {
      hidePreviewPopover();
    }
  }, 300);
}, true);

document.addEventListener("focusin", e => {
  const link = e.target.closest(".donor-filer-link");
  if (link) {
    clearTimeout(_previewTimeout);
    showPreviewPopover(link.dataset.slug, link);
  }
});

document.addEventListener("focusout", e => {
  const link = e.target.closest(".donor-filer-link");
  if (link) {
    clearTimeout(_previewTimeout);
    _previewTimeout = setTimeout(hidePreviewPopover, 300);
  }
});

// ── Sortable table helper ─────────────────────────────────────────────────────

function buildSortableTable(tableId, rows, columns, searchEl = null) {
  // columns: [{key, label, fmt, cls, linkDonor}]
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.querySelector("tbody");
  let sortCol = columns[columns.length - 1].key;  // default sort: last column (numeric)
  let sortDir = "desc";

  function visible() {
    if (!searchEl || !searchEl.value.trim()) return rows;
    const q = searchEl.value.trim().toLowerCase();
    return rows.filter(r => String(r[columns[0].key] || "").toLowerCase().includes(q));
  }

  function render() {
    const sorted = [...visible()].sort((a, b) => {
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
        let display;
        if (col.linkDonor && v && donorFilerMap) {
          display = renderDonorCell(v);
        } else {
          display = col.fmt ? col.fmt(v) : (v != null ? esc(String(v)) : "—");
        }
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

  if (searchEl && !searchEl._listenerAttached) {
    searchEl.addEventListener("input", render);
    searchEl._listenerAttached = true;
  }

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
    // Restore default range display (populated once summaryData is available)
    dateStartEl.value = (summaryData && summaryData.date_range_start) || "";
    dateEndEl.value   = (summaryData && summaryData.date_range_end)   || "";
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

  // Pre-fill date inputs with the dataset's full range (only if still blank)
  const dsEl = document.getElementById("date-start");
  const deEl = document.getElementById("date-end");
  if (!dsEl.value && summaryData.date_range_start) dsEl.value = summaryData.date_range_start;
  if (!deEl.value && summaryData.date_range_end)   deEl.value = summaryData.date_range_end;

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
    await loadTimeline();
  }
}

function renderOverviewGlobal() {
  const hasDate = state.dateStart || state.dateEnd;
  if (hasDate) {
    const globalBeginBal = summaryData.global_beginning_balances || {};
    const { totalIn, totalInKind, totalOut, cashOnHand, count } = statsFromTimeline(filterMonthRows(timelineData || []), globalBeginBal);
    document.getElementById("stat-contributions").textContent = fmt$(totalIn);
    document.getElementById("stat-inkind").textContent        = fmt$(totalInKind);
    document.getElementById("stat-expenditures").textContent  = fmt$(totalOut);
    document.getElementById("stat-cash-on-hand").textContent  = fmt$(cashOnHand);
    document.getElementById("stat-transactions").textContent  = count ? fmtNum(count) : "—";
  } else {
    document.getElementById("stat-contributions").textContent = fmt$(summaryData.total_contributions);
    document.getElementById("stat-inkind").textContent        = fmt$(summaryData.total_inkind || 0);
    document.getElementById("stat-expenditures").textContent  = fmt$(summaryData.total_expenditures);
    const coh = summaryData.global_cash_on_hand != null
      ? summaryData.global_cash_on_hand
      : summaryData.total_contributions - summaryData.total_expenditures;
    document.getElementById("stat-cash-on-hand").textContent  = fmt$(coh);
    document.getElementById("stat-transactions").textContent  = fmtNum(summaryData.total_transactions);
  }
  updateCohIndicator(null);  // No single-filer indicator for global view
  const cohNote = document.getElementById("coh-note");
  if (cohNote) cohNote.textContent = summaryData.global_beginning_balances
    ? "Includes ORESTAR beginning balances"
    : "";
  document.getElementById("stat-cards").hidden             = false;
  document.getElementById("filer-comparison-grid").hidden  = true;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent = "Contributions by Donor Type";

  // byTypeDataGlobal is now {all_time:[...], by_year:{...}, by_month:{...}}
  let byTypeRows;
  if (Array.isArray(byTypeDataGlobal)) {
    byTypeRows = byTypeDataGlobal; // old cached format — show as-is
  } else if (byTypeDataGlobal) {
    if (hasDate && byTypeDataGlobal.by_month) {
      byTypeRows = mergeTypeByMonth(byTypeDataGlobal.by_month);
    } else if (hasDate) {
      byTypeRows = mergeTypeByYear(byTypeDataGlobal.by_year || {}, yearsInRange());
    } else {
      byTypeRows = byTypeDataGlobal.all_time || [];
    }
  } else {
    byTypeRows = [];
  }
  resetDonutBox();
  if (byTypeRows.length) {
    makeDonutChart(
      "chart-contributor-type",
      byTypeRows.map(r => r.type),
      byTypeRows.map(r => r.total),
      "Contributor Type",
      byTypeRows,
    );
  }

  document.getElementById("filer-comparison-grid").hidden = true;
  renderAcctSummary(null);  // No per-filer account summary in global view
  fitStatCards();
}

// ── Discrepancy severity thresholds (absolute dollar difference) ──────────
function discrepancySeverity(absDisc) {
  if (absDisc < 500) return "gray";
  if (absDisc <= 2000) return "yellow";
  return "red";
}

function formatTimestamp(unixTs) {
  if (!unixTs) return "unknown";
  const d = new Date(unixTs * 1000);
  return d.toLocaleString("en-US", {
    hour: "numeric", minute: "2-digit", hour12: true,
    month: "long", day: "numeric", year: "numeric",
  });
}

function updateCohIndicator(profile) {
  const ind = document.getElementById("coh-indicator");
  if (!ind) return;

  // Remove any existing popover
  const existing = ind.parentElement.querySelector(".disc-popover");
  if (existing) existing.remove();

  if (!profile) {
    ind.hidden = true;
    return;
  }
  const src = profile.cash_on_hand_source;
  const disc = profile.orestar_discrepancy || 0;
  const absDisc = Math.abs(disc);

  if (src === "orestar" && absDisc > 0.01) {
    const severity = discrepancySeverity(absDisc);
    ind.hidden = false;
    ind.className = `coh-indicator coh-warn-${severity}`;
    ind.textContent = "\u26a0";
    ind.setAttribute("tabindex", "0");
    ind.setAttribute("role", "button");
    ind.setAttribute("aria-label", `ORESTAR discrepancy: ${fmt$(absDisc)}`);
    ind.removeAttribute("title");

    // Build rich popover
    const acct = profile.orestar_account_summary || {};
    const orestarEnding = acct.ending_cash_balance != null ? acct.ending_cash_balance : null;
    const scrapeTs = acct.scrape_ts || 0;

    const popover = document.createElement("div");
    popover.className = "disc-popover";
    popover.setAttribute("role", "tooltip");
    popover.innerHTML = `
      <div class="disc-row"><span>ORESTAR ending cash balance:</span><span>${orestarEnding != null ? fmt$(orestarEnding) : "N/A"}</span></div>
      <div class="disc-row"><span>Website cash on hand:</span><span>${fmt$(profile.cash_on_hand)}</span></div>
      <div class="disc-row disc-diff"><span>Difference:</span><span>${disc >= 0 ? "+" : ""}${fmt$(disc)}</span></div>
      <div class="disc-ts">ORESTAR account summary scraped at: ${formatTimestamp(scrapeTs)}</div>
    `;
    popover.hidden = true;
    ind.parentElement.style.position = "relative";
    ind.parentElement.appendChild(popover);

    function showPopover() { popover.hidden = false; }
    function hidePopover() { popover.hidden = true; }
    ind.addEventListener("mouseenter", showPopover);
    ind.addEventListener("mouseleave", hidePopover);
    ind.addEventListener("focus", showPopover);
    ind.addEventListener("blur", hidePopover);
    ind.addEventListener("click", () => { popover.hidden = !popover.hidden; });
  } else if (src === "calculated") {
    ind.hidden = false;
    ind.className = "coh-indicator coh-estimated";
    ind.textContent = "EST";
    ind.setAttribute("tabindex", "0");
    ind.title = "Estimated: no ORESTAR beginning balance available. Cash on hand is calculated from transactions only.";
  } else {
    ind.hidden = true;
  }
}

// Build discrepancy indicator HTML for multi-filer cards (inline)
function cohIndicatorHTML(profile) {
  const src = profile.cash_on_hand_source;
  const disc = profile.orestar_discrepancy || 0;
  const absDisc = Math.abs(disc);
  if (src === "calculated") {
    return '<span class="coh-indicator coh-estimated" tabindex="0" title="Estimated: no ORESTAR beginning balance available">EST</span>';
  }
  if (absDisc > 0.01) {
    const severity = discrepancySeverity(absDisc);
    const acct = profile.orestar_account_summary || {};
    const tsText = formatTimestamp(acct.scrape_ts || 0);
    const tip = `ORESTAR ending: ${fmt$(acct.ending_cash_balance || 0)} | Website: ${fmt$(profile.cash_on_hand)} | Diff: ${fmt$(disc)} | Scraped: ${tsText}`;
    return `<span class="coh-indicator coh-warn-${severity}" tabindex="0" title="${esc(tip)}">\u26a0</span>`;
  }
  return '';
}

function renderOverviewSingleFiler(profile) {
  const hasDate = state.dateStart || state.dateEnd;
  if (hasDate) {
    const { totalIn, totalInKind, totalOut, cashOnHand, count } =
      statsFromTimeline(filterMonthRows(profile.timeline || []), profile.beginning_balances);
    document.getElementById("stat-contributions").textContent = fmt$(totalIn);
    document.getElementById("stat-inkind").textContent        = fmt$(totalInKind);
    document.getElementById("stat-expenditures").textContent  = fmt$(totalOut);
    document.getElementById("stat-cash-on-hand").textContent  = fmt$(cashOnHand);
    document.getElementById("stat-transactions").textContent  = count ? fmtNum(count) : "—";
  } else {
    document.getElementById("stat-contributions").textContent = fmt$(profile.total_in);
    document.getElementById("stat-inkind").textContent        = fmt$(profile.total_inkind || 0);
    document.getElementById("stat-expenditures").textContent  = fmt$(profile.total_out);
    document.getElementById("stat-cash-on-hand").textContent  = fmt$(profile.cash_on_hand);
    document.getElementById("stat-transactions").textContent  = fmtNum(profile.tran_count);
  }
  updateCohIndicator(profile);
  const cohNote = document.getElementById("coh-note");
  if (cohNote) cohNote.textContent = profile.cash_on_hand_source === "orestar"
    ? "Includes ORESTAR beginning balances"
    : "Estimated from transactions only";
  document.getElementById("stat-cards").hidden             = false;
  document.getElementById("filer-comparison-grid").hidden  = true;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent =
    `Contributions by Donor Type — ${profile.name}`;

  const byTypeRows = hasDate && profile.by_contributor_type_by_month
    ? mergeTypeByMonth(profile.by_contributor_type_by_month)
    : hasDate
      ? mergeTypeByYear(profile.by_contributor_type_by_year || {}, yearsInRange())
      : (profile.by_contributor_type || []);
  resetDonutBox();
  if (byTypeRows.length) {
    makeDonutChart(
      "chart-contributor-type",
      byTypeRows.map(r => r.type),
      byTypeRows.map(r => r.total),
      "Contributor Type",
      byTypeRows,
    );
  }

  document.getElementById("filer-comparison-grid").hidden = true;
  renderAcctSummary(profile.orestar_account_summary || null);
  fitStatCards();
}

function renderOverviewMultiFiler(profiles) {
  document.getElementById("stat-cards").hidden             = true;
  document.getElementById("filer-comparison-grid").hidden  = false;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent =
    "Contributions by Donor Type";

  const hasDate = state.dateStart || state.dateEnd;

  // Build one donut per filer, side by side, with a single shared legend
  buildMultiDonutBox(profiles);

  // Collect all unique types across filers (preserving order by first appearance)
  const legendTypes = [];
  const perFilerRows = profiles.map(p => hasDate && p.by_contributor_type_by_month
    ? mergeTypeByMonth(p.by_contributor_type_by_month)
    : hasDate
      ? mergeTypeByYear(p.by_contributor_type_by_year || {}, yearsInRange())
      : (p.by_contributor_type || []));
  perFilerRows.forEach(rows => {
    rows.forEach(r => { if (!legendTypes.includes(r.type)) legendTypes.push(r.type); });
  });
  renderMultiDonutLegend(legendTypes);

  profiles.forEach((p, i) => {
    const byTypeRows = perFilerRows[i];
    if (byTypeRows.length) {
      makeDonutChart(
        `chart-type-${i}`,
        byTypeRows.map(r => r.type),
        byTypeRows.map(r => r.total),
        p.name,
        byTypeRows,
        false, // legend shown separately via renderMultiDonutLegend
      );
    }
  });

  document.getElementById("filer-comparison-grid").innerHTML = profiles.map(p => {
    const hasDate = state.dateStart || state.dateEnd;
    const s = hasDate
      ? statsFromTimeline(filterMonthRows(p.timeline || []), p.beginning_balances)
      : { totalIn: p.total_in, totalInKind: p.total_inkind || 0, totalOut: p.total_out, cashOnHand: p.cash_on_hand, count: p.tran_count };
    const tranCount = s.count ? fmtNum(s.count) : "—";
    const cohInd = cohIndicatorHTML(p);
    return `
    <div class="filer-card">
      <div class="filer-card-name">${esc(p.name)}</div>
      <div class="filer-card-stats">
        <div class="filer-card-stat-label">Cash Contributions</div>
        <div class="filer-card-stat-value">${fmt$(s.totalIn)}</div>
        <div class="filer-card-stat-label">In-Kind Received</div>
        <div class="filer-card-stat-value">${fmt$(s.totalInKind)}</div>
        <div class="filer-card-stat-label">Total Expenditures</div>
        <div class="filer-card-stat-value">${fmt$(s.totalOut)}</div>
        <div class="filer-card-stat-label">Cash on Hand ${cohInd}</div>
        <div class="filer-card-stat-value">${fmt$(s.cashOnHand)}</div>
        <div class="filer-card-stat-label">Total Transactions</div>
        <div class="filer-card-stat-value">${tranCount}</div>
      </div>
    </div>`;
  }).join("");
  renderAcctSummary(null);  // Hide in multi-filer mode
}

// ── Donors ────────────────────────────────────────────────────────────────────

async function loadDonors() {
  if (!donorsData) {
    donorsData = await fetchJSON(`${DATA}/top_donors.json`);
  }
  await ensureDonorFilerMap();

  const n = state.selectedFilers.length;
  const years = yearsInRange();

  // Attach toggle button listeners once (works for both global and single-filer views)
  const toggleBtns = document.querySelectorAll("#donors-view-toggle .toggle-btn");
  if (toggleBtns[0] && !toggleBtns[0]._listenerAttached) {
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
        { key: "name",  label: "Donor", linkDonor: true },
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
      { key: "name",  label: "Donor", linkDonor: true },
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
    const filerNames = profiles.map(p => p.name);

    // Build a lookup map per filer: donor name (lowercased) → total
    const filerDonorMaps = profiles.map(profile => {
      const donors = years
        ? mergeByYear(profile.top_donors_by_year || {}, years)
        : (profile.top_donors || []);
      return new Map(donors.map(d => [d.name.toLowerCase(), { name: d.name, total: d.total }]));
    });

    // Use global top-1000 donors as the row universe so the table reaches 1000 rows
    const globalDonors = years
      ? mergeByYear(donorsData.by_year || {}, years).slice(0, 1000)
      : (donorsData.all_time || []).slice(0, 1000);

    const pivotRows = globalDonors.map(d => {
      const byFiler = {};
      filerDonorMaps.forEach((map, i) => {
        const hit = map.get(d.name.toLowerCase());
        if (hit) byFiler[filerNames[i]] = hit.total;
      });
      const total = Object.values(byFiler).reduce((s, v) => s + v, 0);
      return { name: d.name, ...byFiler, total };
    }).filter(row => row.total > 0);

    document.getElementById("donors-multi-title").textContent =
      `Donor Comparison: ${filerNames.join(" vs ")}`;

    let multiSortCol = "total";
    let multiSortDir = "desc";

    const thead = document.getElementById("donors-multi-thead");

    function buildMultiThead() {
      thead.innerHTML = `<tr>
        <th>#</th>
        <th class="sortable" data-col="name">Donor</th>
        ${filerNames.map(n => `<th class="num sortable" data-col="${esc(n)}">${esc(n)}</th>`).join("")}
        <th class="num sortable" data-col="total">Total ($)</th>
      </tr>`;
      thead.querySelectorAll("th.sortable").forEach(th => {
        if (th.dataset.col === multiSortCol) th.classList.add("sort-" + multiSortDir);
        th.addEventListener("click", () => {
          const col = th.dataset.col;
          multiSortDir = multiSortCol === col && multiSortDir === "desc" ? "asc" : "desc";
          multiSortCol = col;
          buildMultiThead();
          renderMultiPivot();
        });
      });
    }

    const multiSearchEl = document.getElementById("donors-multi-search");
    if (multiSearchEl) multiSearchEl.value = "";

    function renderMultiPivot() {
      const q = multiSearchEl ? multiSearchEl.value.trim().toLowerCase() : "";
      const filtered = q ? pivotRows.filter(r => r.name.toLowerCase().includes(q)) : pivotRows;
      const sorted = [...filtered].sort((a, b) => {
        if (multiSortCol === "name") {
          return multiSortDir === "asc"
            ? a.name.localeCompare(b.name)
            : b.name.localeCompare(a.name);
        }
        const va = a[multiSortCol] ?? -1;
        const vb = b[multiSortCol] ?? -1;
        return multiSortDir === "asc" ? va - vb : vb - va;
      });
      const tbody = document.querySelector("#table-donors-multi tbody");
      tbody.innerHTML = sorted.map((row, i) => `<tr>
        <td>${i + 1}</td>
        <td>${donorFilerMap ? renderDonorCell(row.name) : esc(row.name)}</td>
        ${filerNames.map(n => `<td class="num">${row[n] !== undefined ? fmt$(row[n]) : "—"}</td>`).join("")}
        <td class="num">${fmt$(row.total)}</td>
      </tr>`).join("");
    }

    if (multiSearchEl && !multiSearchEl._listenerAttached) {
      multiSearchEl.addEventListener("input", renderMultiPivot);
      multiSearchEl._listenerAttached = true;
    }

    buildMultiThead();
    renderMultiPivot();
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
      { key: "name",  label: "Donor", linkDonor: true },
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

  // Rows from allTime (sorted by total desc, capped at 1000); fill in per-year from map
  const allRows = (allTime || []).slice(0, 1000).map(d => ({
    name: d.name,
    total: d.total,
    ...Object.fromEntries(years.map(yr => [yr, donorYearMap.get(d.name)?.[yr] ?? null])),
  }));

  let sortCol = "total";
  let sortDir = "desc";

  const searchEl = document.getElementById("donors-by-year-search");
  if (searchEl) searchEl.value = "";

  const thead = document.getElementById("donors-by-year-thead");
  thead.innerHTML = `<tr>
    <th>#</th>
    <th class="sortable" data-col="name">Donor</th>
    ${years.map(yr => `<th class="num sortable" data-col="${yr}">${yr}</th>`).join("")}
    <th class="num sortable" data-col="total">All Time</th>
  </tr>`;

  function getByYearRows() {
    if (!searchEl || !searchEl.value.trim()) return allRows;
    const q = searchEl.value.trim().toLowerCase();
    return allRows.filter(r => r.name.toLowerCase().includes(q));
  }

  function renderByYearTable() {
    const rows = getByYearRows();
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
      <td>${donorFilerMap ? renderDonorCell(row.name) : esc(row.name)}</td>
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

  if (searchEl && !searchEl._listenerAttached) {
    searchEl.addEventListener("input", renderByYearTable);
    searchEl._listenerAttached = true;
  }

  renderByYearTable();
}

function renderDonors(year) {
  const rows = (year === "all"
    ? donorsData.all_time
    : (donorsData.by_year[year] || [])).slice(0, 1000);

  const top20 = rows.slice(0, 20);
  makeBarChart(
    "chart-top-donors",
    top20.map(r => r.name),
    top20.map(r => r.total),
    "Total Contributions",
    "#3182ce",
  );

  const searchEl = document.getElementById("donors-summary-search");
  if (searchEl) searchEl.value = "";
  buildSortableTable("table-donors", rows, [
    { key: "name",  label: "Donor", linkDonor: true },
    { key: "total", label: "Total ($)", fmt: fmt$, cls: "num" },
  ], searchEl);
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
  // Load donor→filer map in background (non-blocking)
  ensureDonorFilerMap().catch(() => {});
  initFilerSelector();
  renderActiveTab();
})();
