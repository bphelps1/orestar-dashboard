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

// ── ECharts — no global defaults needed (configured per-instance) ────────────
const IS_MOBILE = window.innerWidth <= 768;

/** Tooltip position: constrain horizontally within chart, free vertically */
function tooltipPosition(point, params, dom, rect, size) {
  let x = point[0] + 10;
  if (x + size.contentSize[0] > size.viewSize[0]) x = point[0] - size.contentSize[0] - 10;
  if (x < 0) x = 0;
  return [x, point[1] - size.contentSize[1] / 2];
}

/** Initialize or re-initialize an ECharts instance on a DOM element */
function initEChart(el) {
  let instance = echarts.getInstanceByDom(el);
  if (instance) instance.dispose();
  return echarts.init(el, null, { renderer: 'svg' });
}

/** Resize all active ECharts instances */
window.addEventListener('resize', () => {
  document.querySelectorAll('.echart-container, .echart-container-tall').forEach(el => {
    const instance = echarts.getInstanceByDom(el);
    if (instance) instance.resize();
  });
});

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

// Short display names for donor type labels (used in legends and chart axes)
const TYPE_SHORT_NAMES = {
  "Business Entity": "Business",
  "Political Committee": "Political Cmte",
  "Individual": "Individual",
  "Other": "Other",
  "Labor Organization": "Labor",
  "Candidate & Immediate Family": "Candidate/Family",
  "Unregistered Committee": "Unreg. Cmte",
  "Political Party Committee": "Party Cmte",
};
function shortTypeName(name) { return TYPE_SHORT_NAMES[name] || name; }

function typeColor(label) {
  const base  = label.endsWith(" (out of state)") ? label.slice(0, -15) : label;
  const color = TYPE_COLOR_MAP[base] ?? PALETTE[Object.keys(TYPE_COLOR_MAP).length % PALETTE.length];
  return label.endsWith(" (out of state)") ? hexToRgba(color, 0.45) : color;
}

// ── Chart box DOM helpers ─────────────────────────────────────────────────────

function resetChartBox() {
  const box = document.getElementById("overview-donut-box");
  // Dispose any ECharts instances inside
  box.querySelectorAll('.echart-container, .echart-container-tall, .echart-container-radar').forEach(el => {
    const inst = echarts.getInstanceByDom(el);
    if (inst) inst.dispose();
  });
  // Remove any dynamically added elements (legends, multi-filer divs, radar layout)
  box.querySelectorAll('.stacked-area-legend, .multi-chart-row, .radar-layout').forEach(el => el.remove());
  // Ensure the main chart div exists
  let chartEl = document.getElementById("chart-contributor-type");
  if (!chartEl) {
    chartEl = document.createElement("div");
    chartEl.id = "chart-contributor-type";
    chartEl.className = "echart-container";
    box.appendChild(chartEl);
  }
  chartEl.className = "echart-container";
  chartEl.style.display = "";  // Restore visibility (hidden in multi-filer mode)
}

function buildMultiChartBox(profiles) {
  const box = document.getElementById("overview-donut-box");
  // Dispose existing charts
  box.querySelectorAll('.echart-container, .echart-container-tall').forEach(el => {
    const inst = echarts.getInstanceByDom(el);
    if (inst) inst.dispose();
  });
  // Remove old elements
  const main = document.getElementById("chart-contributor-type");
  if (main) main.remove();
  box.querySelectorAll('.multi-chart-row, .stacked-area-legend').forEach(el => el.remove());

  // Create container for each filer
  const row = document.createElement("div");
  row.className = "multi-chart-row";
  profiles.forEach((p, i) => {
    const unit = document.createElement("div");
    unit.className = "multi-chart-unit";
    const title = document.createElement("div");
    title.className = "multi-chart-title";
    title.textContent = p.name;
    const chartDiv = document.createElement("div");
    chartDiv.id = `chart-type-${i}`;
    chartDiv.className = "echart-container";
    unit.appendChild(title);
    unit.appendChild(chartDiv);
    row.appendChild(unit);
  });
  box.appendChild(row);
}

// ── Stacked Area Chart (Who Funds Oregon Campaigns) ─────────────────────────

function makeStackedAreaChart(containerId, byMonthData, byTypeRows) {
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.className = "echart-container";

  // 1. Compute base types (merge "out of state" variants)
  const baseTypeMap = {};
  const months = Object.keys(byMonthData).filter(m => m >= "2006-01").sort();
  const monthlyBaseData = {};

  for (const month of months) {
    monthlyBaseData[month] = {};
    for (const entry of byMonthData[month]) {
      const base = entry.type.endsWith(" (out of state)") ? entry.type.slice(0, -15) : entry.type;
      if (!baseTypeMap[base]) baseTypeMap[base] = 0;
      baseTypeMap[base] += entry.total;
      if (!monthlyBaseData[month][base]) {
        monthlyBaseData[month][base] = { total: 0, top_donors: [] };
      }
      monthlyBaseData[month][base].total += entry.total;
      if (entry.top_donors) monthlyBaseData[month][base].top_donors.push(...entry.top_donors);
    }
    // Merge/dedupe top donors
    for (const base of Object.keys(monthlyBaseData[month])) {
      const donors = monthlyBaseData[month][base].top_donors;
      const merged = {};
      for (const d of donors) merged[d.name] = (merged[d.name] || 0) + d.total;
      monthlyBaseData[month][base].top_donors = Object.entries(merged)
        .map(([name, total]) => ({ name, total }))
        .sort((a, b) => b.total - a.total);
    }
  }

  // 2. Order base types
  let baseTypes;
  if (byTypeRows && byTypeRows.length) {
    const seen = new Set();
    baseTypes = [];
    for (const r of byTypeRows) {
      const base = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
      if (!seen.has(base) && baseTypeMap[base]) { seen.add(base); baseTypes.push(base); }
    }
    for (const bt of Object.keys(baseTypeMap)) { if (!baseTypes.includes(bt)) baseTypes.push(bt); }
  } else {
    baseTypes = Object.keys(baseTypeMap).sort((a, b) => baseTypeMap[b] - baseTypeMap[a]);
  }

  // 3. Format month labels
  const labels = months.map(m => {
    const [y, mo] = m.split("-");
    return new Date(+y, +mo - 1).toLocaleDateString("en-US", { month: "short", year: "2-digit" });
  });

  // 4. Compute totals for legend
  const baseTotals = {};
  let grandTotal = 0;
  for (const month of months) {
    for (const base of baseTypes) {
      const val = monthlyBaseData[month]?.[base]?.total || 0;
      baseTotals[base] = (baseTotals[base] || 0) + val;
      grandTotal += val;
    }
  }

  // 5. Build ECharts series
  const series = baseTypes.map(base => {
    const color = TYPE_COLOR_MAP[base] ?? PALETTE[Object.keys(TYPE_COLOR_MAP).length % PALETTE.length];
    return {
      name: base,
      type: 'line',
      stack: 'total',
      areaStyle: { opacity: 0.6 },
      emphasis: { focus: 'series' },
      symbol: 'none',
      lineStyle: { width: 1.5 },
      itemStyle: { color },
      data: months.map(m => Math.round(monthlyBaseData[m]?.[base]?.total || 0)),
    };
  });

  // 6. Create chart
  const chart = initEChart(el);
  const option = {
    tooltip: {
      trigger: 'axis',
      triggerOn: IS_MOBILE ? 'click' : 'mousemove',
      alwaysShowContent: false,
      confine: false,
      axisPointer: { type: 'cross' },
      extraCssText: IS_MOBILE ? 'max-width:260px;font-size:11px;' : '',
      position: tooltipPosition,
      formatter: function(params) {
        if (!params || !params.length) return '';
        const idx = params[0].dataIndex;
        const month = months[idx];
        const data = monthlyBaseData[month] || {};
        const total = params.reduce((s, p) => s + (p.value || 0), 0);
        const [y, mo] = month.split("-");
        const monthLabel = new Date(+y, +mo - 1).toLocaleDateString("en-US", { month: "short", year: "numeric" });

        const sorted = [...params].sort((a, b) => (b.value || 0) - (a.value || 0));
        // On mobile, show only types with values (skip zeros)
        const shown = sorted.filter(p => p.value > 0);
        const fs = IS_MOBILE ? '11px' : '13px';
        let html = `<div style="font-weight:600;margin-bottom:3px;font-size:${fs}">${monthLabel}</div>`;
        shown.forEach(p => {
          const pct = total > 0 ? (p.value / total * 100).toFixed(1) : "0.0";
          const isSelected = selectedDonorTypeGroup === p.seriesName;
          const bg = isSelected ? 'background:rgba(0,0,0,0.06);border-radius:3px;' : '';
          const name = IS_MOBILE ? shortTypeName(p.seriesName) : p.seriesName;
          html += `<div style="display:flex;align-items:center;gap:4px;padding:1px 3px;font-size:${fs};${bg}">
            ${p.marker}<span style="flex:1">${name}</span>
            <span style="font-weight:500">${fmtCompact$(p.value)}</span>
            <span style="color:#999;font-size:10px">${pct}%</span>
          </div>`;
        });
        html += `<div style="border-top:1px solid #eee;margin-top:3px;padding-top:3px;font-weight:600;text-align:right;font-size:${fs}">${fmtCompact$(total)}</div>`;

        // Top donors for selected type
        const maxDonors = IS_MOBILE ? 3 : 5;
        if (selectedDonorTypeGroup && data[selectedDonorTypeGroup]?.top_donors?.length) {
          const donors = data[selectedDonorTypeGroup].top_donors.slice(0, maxDonors);
          const label = IS_MOBILE ? shortTypeName(selectedDonorTypeGroup) : selectedDonorTypeGroup;
          html += `<div style="margin-top:3px;font-weight:600;font-size:10px">Top ${label} Donors</div>`;
          donors.forEach(d => {
            html += `<div style="display:flex;justify-content:space-between;font-size:10px;padding:1px 0">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px">${d.name}</span>
              <span style="font-weight:500;margin-left:6px;white-space:nowrap">${fmtCompact$(d.total)}</span>
            </div>`;
          });
        } else if (!selectedDonorTypeGroup) {
          html += `<div style="margin-top:3px;font-size:10px;color:#999;font-style:italic">Tap a colored area for top donors</div>`;
        }
        return html;
      },
    },
    grid: {
      left: IS_MOBILE ? 10 : 20,
      right: IS_MOBILE ? 10 : 20,
      top: 10,
      bottom: IS_MOBILE ? 80 : 40,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: { fontSize: IS_MOBILE ? 9 : 11, rotate: 45 },
      boundaryGap: false,
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: v => fmtCompact$(v),
        fontSize: IS_MOBILE ? 9 : 12,
      },
    },
    dataZoom: IS_MOBILE ? [
      { type: 'slider', start: 70, end: 100, height: 25, bottom: 5 },
      { type: 'inside' },
    ] : [{ type: 'inside' }],
    series,
  };
  chart.setOption(option);

  // 7. Click handler — use ZRender click since stacked areas with symbol:none
  //    don't fire ECharts series click events. Determine which series was
  //    clicked by checking the y-value against cumulative stack heights.
  function applySelection(typeName, dataIndex) {
    selectedDonorTypeGroup = selectedDonorTypeGroup === typeName ? null : typeName;
    const newSeries = baseTypes.map(base => ({
      name: base,
      areaStyle: {
        opacity: selectedDonorTypeGroup === null ? 0.6
          : selectedDonorTypeGroup === base ? 0.85 : 0.05,
      },
      lineStyle: {
        width: selectedDonorTypeGroup === base ? 2.5 : 1,
        opacity: selectedDonorTypeGroup === null ? 1
          : selectedDonorTypeGroup === base ? 1 : 0.1,
      },
    }));
    chart.setOption({ series: newSeries });
    updateStackedAreaLegendStyles(el.parentElement || el.closest('#overview-donut-box'), baseTypes);
    // Hide then re-show tooltip so formatter re-runs with updated selection
    // (ECharts won't re-render if tooltip is already visible at same position)
    chart.dispatchAction({ type: 'hideTip' });
    if (dataIndex != null) {
      setTimeout(() => {
        chart.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex });
      }, 80);
    }
  }

  chart.getZr().on('click', function(params) {
    const pointInGrid = chart.containPixel('grid', [params.offsetX, params.offsetY]);
    if (!pointInGrid) {
      // Clicked outside the grid (margins, title area) — reset selection
      if (selectedDonorTypeGroup) {
        applySelection(selectedDonorTypeGroup, null); // toggle off
      }
      chart.dispatchAction({ type: 'hideTip' });
      return;
    }

    const dataCoord = chart.convertFromPixel({ seriesIndex: 0 }, [params.offsetX, params.offsetY]);
    const dataIndex = Math.round(dataCoord[0]);
    if (dataIndex < 0 || dataIndex >= months.length) return;

    const month = months[dataIndex];
    const mData = monthlyBaseData[month] || {};
    const clickY = dataCoord[1];

    // Check if click is above all stacked data (white space above the chart data)
    let totalAtIndex = 0;
    for (const base of baseTypes) totalAtIndex += mData[base]?.total || 0;

    if (clickY > totalAtIndex) {
      // Clicked above the data — reset selection
      if (selectedDonorTypeGroup) {
        applySelection(selectedDonorTypeGroup, null);
      }
      chart.dispatchAction({ type: 'hideTip' });
      return;
    }

    // Walk up the stack to find which series was clicked
    let cumulative = 0;
    let clickedType = null;
    for (const base of baseTypes) {
      const val = mData[base]?.total || 0;
      cumulative += val;
      if (clickY <= cumulative) {
        clickedType = base;
        break;
      }
    }
    if (!clickedType && baseTypes.length) clickedType = baseTypes[baseTypes.length - 1];

    applySelection(clickedType, dataIndex);
  });

  // Also keep ECharts series click as fallback (works on desktop with hover)
  chart.on('click', function(params) {
    if (params.componentType === 'series') {
      applySelection(params.seriesName, params.dataIndex);
    }
  });

  // 8. Dismiss tooltip when tapping outside chart + legend area
  const chartBox = el.closest("#overview-donut-box") || el.parentElement;
  document.addEventListener("click", function(e) {
    if (chartBox && !chartBox.contains(e.target)) {
      chart.dispatchAction({ type: "hideTip" });
    }
  });

  // 9. Render custom legend (keep existing pattern)
  renderStackedAreaLegend(chart, baseTypes, baseTotals, grandTotal, byTypeRows);

  // 10. Update legend totals when dataZoom changes (pan/zoom/slider)
  function updateLegendForVisibleRange() {
    const opt = chart.getOption();
    const zoom = opt.dataZoom || [];
    let startPct = 0, endPct = 100;
    for (const dz of zoom) {
      if (dz.start != null) startPct = dz.start;
      if (dz.end != null) endPct = dz.end;
    }
    const startIdx = Math.floor(startPct / 100 * months.length);
    const endIdx = Math.ceil(endPct / 100 * months.length);
    const visibleMonths = months.slice(startIdx, endIdx);

    const visTotals = {};
    let visGrand = 0;
    for (const m of visibleMonths) {
      for (const base of baseTypes) {
        const val = monthlyBaseData[m]?.[base]?.total || 0;
        visTotals[base] = (visTotals[base] || 0) + val;
        visGrand += val;
      }
    }

    // Update legend item text
    const box = el.closest("#overview-donut-box") || el.parentElement;
    if (!box) return;
    const items = box.querySelectorAll(".stacked-area-legend-item");
    items.forEach((item, i) => {
      const base = baseTypes[i];
      if (!base) return;
      const total = visTotals[base] || 0;
      const pct = visGrand > 0 ? (total / visGrand * 100).toFixed(1) : "0.0";
      const amtEl = item.querySelector(".legend-type-amount");
      const pctEl = item.querySelector(".legend-type-pct");
      if (amtEl) amtEl.textContent = fmt$(total);
      if (pctEl) pctEl.textContent = `(${pct}%)`;
    });
  }

  chart.on('dataZoom', updateLegendForVisibleRange);

  return chart;
}

function renderStackedAreaLegend(chart, baseTypes, baseTotals, grandTotal, byTypeRows) {
  const el = chart.getDom();
  const box = el.closest("#overview-donut-box") || el.parentElement;
  if (!box) return;
  const existing = box.querySelector(".stacked-area-legend");
  if (existing) existing.remove();

  const legendDiv = document.createElement("div");
  legendDiv.className = "stacked-area-legend";

  baseTypes.forEach((base, i) => {
    const color = TYPE_COLOR_MAP[base] ?? PALETTE[Object.keys(TYPE_COLOR_MAP).length % PALETTE.length];
    const total = baseTotals[base] || 0;
    const pct = grandTotal > 0 ? (total / grandTotal * 100).toFixed(1) : "0.0";
    const item = document.createElement("span");
    item.className = "stacked-area-legend-item";
    item.innerHTML = `<span class="swatch" style="background:${color}"></span>
      <span class="legend-type-name">${esc(shortTypeName(base))}</span>
      <span class="legend-type-amount">${fmt$(total)}</span>
      <span class="legend-type-pct">(${pct}%)</span>`;

    item.addEventListener("click", () => {
      selectedDonorTypeGroup = selectedDonorTypeGroup === base ? null : base;
      // Update chart opacity
      const newSeries = baseTypes.map(bt => ({
        name: bt,
        areaStyle: {
          opacity: selectedDonorTypeGroup === null ? 0.6
            : selectedDonorTypeGroup === bt ? 0.85 : 0.05,
        },
        lineStyle: {
          width: selectedDonorTypeGroup === bt ? 2.5 : 1,
          opacity: selectedDonorTypeGroup === null ? 1
            : selectedDonorTypeGroup === bt ? 1 : 0.1,
        },
      }));
      chart.setOption({ series: newSeries });
      updateStackedAreaLegendStyles(box, baseTypes);
    });

    // Hover tooltip for all-time top donors
    item.addEventListener("mouseenter", () => {
      const typeRow = (byTypeRows || []).find(r => {
        const b = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
        return b === base;
      });
      if (!typeRow || !typeRow.top_donors || !typeRow.top_donors.length) return;
      // Use a simple title tooltip
      const donorText = typeRow.top_donors.slice(0, 5).map(d => `${d.name}: ${fmt$(d.total)}`).join('\n');
      item.title = `Top ${base} Donors (All Time)\n${donorText}`;
    });

    legendDiv.appendChild(item);
  });

  // Click background to deselect
  legendDiv.addEventListener("click", (e) => {
    if (e.target === legendDiv) {
      selectedDonorTypeGroup = null;
      const newSeries = baseTypes.map(bt => ({
        name: bt,
        areaStyle: { opacity: 0.6 },
        lineStyle: { width: 1.5, opacity: 1 },
      }));
      chart.setOption({ series: newSeries });
      updateStackedAreaLegendStyles(box, baseTypes);
    }
  });

  box.appendChild(legendDiv);
  updateStackedAreaLegendStyles(box, baseTypes);
}

function updateStackedAreaLegendStyles(container, baseTypes) {
  const legendDiv = container.querySelector ? container.querySelector(".stacked-area-legend") : container;
  if (!legendDiv) return;
  const items = legendDiv.querySelectorAll(".stacked-area-legend-item");
  items.forEach((item, i) => {
    const base = baseTypes[i];
    if (selectedDonorTypeGroup === null) {
      item.classList.remove("selected", "dimmed");
    } else if (selectedDonorTypeGroup === base) {
      item.classList.add("selected");
      item.classList.remove("dimmed");
    } else {
      item.classList.remove("selected");
      item.classList.add("dimmed");
    }
  });
}

// Fallback when no monthly data — simple pie chart
function makeSimplePieChart(containerId, typeRows) {
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.className = "echart-container";
  const chart = initEChart(el);
  chart.setOption({
    tooltip: {
      trigger: 'item',
      position: tooltipPosition,
      formatter: '{b}: {c} ({d}%)',
    },
    series: [{
      type: 'pie',
      radius: ['35%', '65%'],
      data: typeRows.map(r => ({
        name: r.type,
        value: Math.round(r.total),
        itemStyle: { color: typeColor(r.type) },
      })),
      label: { fontSize: IS_MOBILE ? 10 : 12 },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } },
    }],
  });
  return chart;
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

// ── Account summary tile definitions ─────────────────────────────────────────
// Values are CALCULATED from our scraped transaction data, NOT taken from the
// ORESTAR account summary page. ORESTAR account summary is used for validation
// only (discrepancy checking). The only value taken from ORESTAR is the first
// year's beginning balance, which anchors our running cash position.

const CALC_TILE_META = {
  // ── Calculated from transaction data ──────────────────────────────────────
  cash_contributions: {
    label: "Cash Contributions",
    tip: "<strong>Counted:</strong> Sum of every individual cash contribution transaction we scraped.<br><strong>Condition:</strong> Transactions with type 'C' and sub-type 'Cash Contribution'.<br><strong>Meaning:</strong> Total direct cash and check donations received by this committee.",
  },
  inkind_contributions: {
    label: "In-Kind Contributions",
    tip: "<strong>Counted:</strong> Sum of every in-kind contribution transaction we scraped.<br><strong>Condition:</strong> Transactions with type 'C' and sub-type containing 'In-Kind'.<br><strong>Meaning:</strong> The estimated fair-market value of goods, services, or event space donated instead of cash.",
  },
  total_contributions: {
    label: "Total Contributions",
    subtotal: true,
    compute: d => d.cash_contributions + d.inkind_contributions,
    tip: "<strong>Counted:</strong> Cash contributions + in-kind contributions.<br><strong>Meaning:</strong> Everything the committee received from donors.",
  },
  cash_expenditures: {
    label: "Cash Expenditures",
    tip: "<strong>Counted:</strong> Sum of every cash expenditure transaction we scraped.<br><strong>Condition:</strong> Transactions with type 'E', excluding non-cash-affecting sub-types like 'Account Payable' and 'Personal Expenditure for Reimbursement'.<br><strong>Meaning:</strong> Total cash actually spent on campaign operations.",
  },
  other_disbursements: {
    label: "Other Disbursements",
    tip: "<strong>Counted:</strong> Sum of non-expenditure cash outflows from our scraped transactions.<br><strong>Condition:</strong> Transactions with type 'OD' (Other Disbursement).<br><strong>Meaning:</strong> Miscellaneous cash payments that are not standard campaign expenditures.",
  },
  total_outflows: {
    label: "Total Outflows",
    subtotal: true,
    compute: d => d.cash_expenditures + d.other_disbursements,
    tip: "<strong>Counted:</strong> Cash expenditures + other disbursements.<br><strong>Meaning:</strong> Total cash paid out by the committee.",
  },
  // ── Cash Balance (calculated from transactions + first-year anchor) ───────
  beginning_balance: {
    label: "Beginning Balance",
    tip: "<strong>Counted:</strong> The committee's cash position at the start of the selected period.<br><strong>Condition:</strong> Anchored to the first-ever ORESTAR beginning balance for this committee, then rolled forward year-to-year using our transaction data.<br><strong>Meaning:</strong> How much cash the committee started with. Only the very first year's balance comes from ORESTAR; every subsequent year is calculated from the prior year's ending balance.",
  },
  other_receipts: {
    label: "Other Receipts",
    tip: "<strong>Counted:</strong> Sum of non-contribution cash received from our scraped transactions.<br><strong>Condition:</strong> Transactions with type 'OR', 'O', or 'OA' (excluding 'Account Payable Rescinded').<br><strong>Meaning:</strong> Refunds, interest, and other miscellaneous income that is not a contribution.",
  },
  net_cash_flow: {
    label: "Net Cash Flow",
    subtotal: true,
    compute: d => d.cash_contributions + d.other_receipts - d.cash_expenditures - d.other_disbursements,
    tip: "<strong>Counted:</strong> (Cash contributions + other receipts) minus (cash expenditures + other disbursements).<br><strong>Meaning:</strong> How much cash the committee gained or lost during this period.",
  },
  ending_cash_balance: {
    label: "Ending Cash Balance",
    subtotal: true,
    compute: d => d.beginning_balance + d.cash_contributions + d.other_receipts - d.cash_expenditures - d.other_disbursements,
    tip: "<strong>Counted:</strong> Beginning balance + net cash flow.<br><strong>Meaning:</strong> Our calculated cash position at the end of the period. This should closely match the ORESTAR ending balance if our transaction data is complete.",
  },
  // ── ORESTAR-reported values (for comparison / validation) ─────────────────
  orestar_ending: {
    label: "ORESTAR Ending Balance",
    orestar: true,
    tip: "<strong>Source:</strong> Scraped directly from the ORESTAR account summary page.<br><strong>Meaning:</strong> The official ending cash balance reported to the state. Compare this to our calculated ending balance above — any difference means our transaction data may be incomplete or categorized differently.",
  },
  orestar_discrepancy: {
    label: "Discrepancy",
    orestar: true,
    subtotal: true,
    tip: "<strong>Counted:</strong> Our calculated ending balance minus the ORESTAR-reported ending balance.<br><strong>Meaning:</strong> A positive number means we calculate more cash than ORESTAR reports; negative means less. Large discrepancies signal missing transactions or categorization differences.",
  },
  // ── ORESTAR-only balance sheet items (cannot be calculated from transactions) ──
  accounts_receivable: {
    label: "Accounts Receivable",
    orestar: true,
    tip: "<strong>Source:</strong> ORESTAR account summary only (not calculable from transactions).<br><strong>Meaning:</strong> Money pledged or owed to the committee that hasn't been received yet — like outstanding pledges or reimbursements expected.",
  },
  accounts_payable: {
    label: "Accounts Payable",
    orestar: true,
    tip: "<strong>Source:</strong> ORESTAR account summary only (not calculable from transactions).<br><strong>Meaning:</strong> Bills the committee owes but hasn't paid yet — like vendor invoices or outstanding obligations.",
  },
  total_outstanding_loans: {
    label: "Outstanding Loans",
    orestar: true,
    tip: "<strong>Source:</strong> ORESTAR account summary only (not calculable from transactions).<br><strong>Meaning:</strong> Total unpaid loan balances — how much the committee still owes on borrowed money.",
  },
  outstanding_personal_expenditures: {
    label: "Personal Expenditures Owed",
    orestar: true,
    tip: "<strong>Source:</strong> ORESTAR account summary only (not calculable from transactions).<br><strong>Meaning:</strong> Money a candidate or treasurer spent personally on behalf of the committee that hasn't been reimbursed yet.",
  },
};

const CALC_GROUPS = [
  {
    title: "Contributions (Calculated from Transactions)",
    fields: ["cash_contributions", "inkind_contributions", "total_contributions"],
  },
  {
    title: "Outflows (Calculated from Transactions)",
    fields: ["cash_expenditures", "other_disbursements", "total_outflows"],
  },
  {
    title: "Cash Balance (Calculated)",
    fields: [
      "beginning_balance", "cash_contributions", "other_receipts",
      "cash_expenditures", "other_disbursements",
      "net_cash_flow", "ending_cash_balance",
    ],
  },
  {
    title: "ORESTAR Validation",
    fields: ["orestar_ending", "orestar_discrepancy"],
  },
  {
    title: "ORESTAR-Reported Balances",
    fields: ["accounts_receivable", "accounts_payable", "total_outstanding_loans", "outstanding_personal_expenditures"],
  },
];

/**
 * Build a calculated account summary from the filer's transaction data.
 * All values come from our scraped row-by-row transaction data, NOT from
 * the ORESTAR account summary page. The only exception is the first-year
 * beginning balance (the anchor).
 */
/**
 * Build a calculated account summary, optionally filtered to a single year.
 * @param {object} profile - The filer's full profile JSON
 * @param {string} [year] - Optional year string (e.g. "2024") to filter to
 */
function buildCalcSummary(profile, year) {
  const timeline = profile.timeline || [];
  if (!timeline.length) return null;

  // Filter timeline rows if year is specified
  const rows = year
    ? timeline.filter(r => r.month && r.month.startsWith(year))
    : timeline;

  // Sum transaction-based values from the (filtered) timeline
  let cashContrib = 0, inkind = 0, cashExpend = 0, otherReceipts = 0, otherDisburse = 0;
  for (const row of rows) {
    cashContrib   += row.contributions       || 0;
    inkind        += row.inkind              || 0;
    cashExpend    += row.expenditures        || 0;
    otherReceipts += row.other_receipts      || 0;
    otherDisburse += row.other_disbursements || 0;
  }

  // Beginning balance
  const beginBalances = profile.beginning_balances || {};
  let beginBal;
  if (year) {
    // For a specific year, use that year's beginning balance
    beginBal = beginBalances[year] || 0;
  } else {
    // All time: use the first year's beginning balance
    const sortedYears = Object.keys(beginBalances).sort();
    const firstYear = sortedYears[0] || "";
    beginBal = firstYear ? (beginBalances[firstYear] || 0) : 0;
  }

  // Our calculated ending balance
  const endingCalc = beginBal + cashContrib + otherReceipts - cashExpend - otherDisburse;

  // ORESTAR-reported values (for comparison / validation)
  // If a specific year is selected, use the per-year ORESTAR data
  const orestarYearly = profile.orestar_yearly || {};
  const orestarYear = year ? (orestarYearly[year] || null) : null;
  const orestarCurrent = profile.orestar_account_summary || {};

  let orestarEnding = null;
  let orestarContrib = null, orestarExpend = null;
  let orestarOR = null, orestarOD = null, orestarBegBal = null;
  let hasOrestar = false;

  if (year && orestarYear) {
    // Per-year ORESTAR data from the yearly scraper
    orestarEnding = orestarYear.ending_cash_balance;
    orestarContrib = orestarYear.contributions;
    orestarExpend = orestarYear.expenditures;
    orestarOR = orestarYear.other_receipts;
    orestarOD = orestarYear.other_disbursements;
    orestarBegBal = orestarYear.beginning_balance;
    hasOrestar = true;
  } else if (!year && orestarCurrent.year) {
    // All-time: use the current year's ORESTAR account summary
    orestarEnding = orestarCurrent.ending_cash_balance != null
      ? orestarCurrent.ending_cash_balance : null;
    hasOrestar = true;
  }

  return {
    cash_contributions: Math.round(cashContrib * 100) / 100,
    inkind_contributions: Math.round(inkind * 100) / 100,
    cash_expenditures: Math.round(cashExpend * 100) / 100,
    other_disbursements: Math.round(otherDisburse * 100) / 100,
    beginning_balance: Math.round(beginBal * 100) / 100,
    other_receipts: Math.round(otherReceipts * 100) / 100,
    ending_cash_balance: Math.round(endingCalc * 100) / 100,
    // ORESTAR values for validation
    orestar_ending: orestarEnding,
    orestar_discrepancy: orestarEnding != null
      ? Math.round((endingCalc - orestarEnding) * 100) / 100
      : null,
    // Per-year ORESTAR breakdown (when available)
    orestar_beginning_balance: orestarBegBal,
    orestar_contributions: orestarContrib,
    orestar_expenditures: orestarExpend,
    orestar_other_receipts: orestarOR,
    orestar_other_disbursements: orestarOD,
    accounts_receivable: (year && orestarYear)
      ? (orestarYear.accounts_receivable || 0)
      : (orestarCurrent.accounts_receivable || 0),
    accounts_payable: (year && orestarYear)
      ? (orestarYear.accounts_payable || 0)
      : (orestarCurrent.accounts_payable || 0),
    total_outstanding_loans: (year && orestarYear)
      ? (orestarYear.total_outstanding_loans || 0)
      : (orestarCurrent.total_outstanding_loans || 0),
    outstanding_personal_expenditures: (year && orestarYear)
      ? (orestarYear.outstanding_personal_expenditures || 0)
      : (orestarCurrent.outstanding_personal_expenditures || 0),
    _has_orestar: hasOrestar,
    _has_orestar_yearly: year && !!orestarYear,
  };
}

function renderAcctSummary(profile) {
  const grid = document.getElementById("acct-summary-grid");
  const details = document.getElementById("acct-summary-details");
  const controls = document.getElementById("acct-summary-controls");
  const yearSelect = document.getElementById("acct-year-select");
  if (!grid || !details) return;

  if (!profile) {
    details.hidden = true;
    if (controls) controls.hidden = true;
    return;
  }

  // Populate year selector from available years in beginning_balances and orestar_yearly
  if (yearSelect && controls) {
    const balYears = Object.keys(profile.beginning_balances || {});
    const orestarYears = Object.keys(profile.orestar_yearly || {});
    const allYears = [...new Set([...balYears, ...orestarYears])].sort().reverse();

    const prevVal = yearSelect.value;
    yearSelect.innerHTML = '<option value="">All Time</option>';
    for (const yr of allYears) {
      yearSelect.innerHTML += `<option value="${yr}">${yr}</option>`;
    }
    yearSelect.value = prevVal && allYears.includes(prevVal) ? prevVal : "";
    controls.hidden = allYears.length === 0;

    // Wire change handler — use onchange so re-calls naturally replace the old handler
    yearSelect.onchange = () => {
      renderAcctSummaryTiles(profile, yearSelect.value || null);
    };
  }

  details.hidden = false;
  const selectedYear = yearSelect ? (yearSelect.value || null) : null;
  renderAcctSummaryTiles(profile, selectedYear);
}

function renderAcctSummaryTiles(profile, year) {
  const grid = document.getElementById("acct-summary-grid");
  if (!grid) return;

  const calcData = buildCalcSummary(profile, year);
  if (!calcData) {
    grid.innerHTML = "<p>No data available for this year.</p>";
    return;
  }

  grid.innerHTML = CALC_GROUPS.map(group => {
    // Hide ORESTAR validation/balance groups if no ORESTAR data
    if (group.title.startsWith("ORESTAR") && !calcData._has_orestar) return "";

    const tiles = group.fields.map(field => {
      const meta = CALC_TILE_META[field];
      if (!meta) return "";

      let val;
      if (meta.compute) {
        val = meta.compute(calcData);
      } else {
        val = calcData[field] != null ? calcData[field] : 0;
      }

      // For discrepancy, show N/A if no ORESTAR data
      if (field === "orestar_discrepancy" && val === null) return "";
      if (field === "orestar_ending" && val === null) return "";

      const isSubtotal = meta.subtotal;
      const isOrestar = meta.orestar;
      let cls = "acct-tile";
      if (isSubtotal) cls += " acct-tile-subtotal";
      if (isOrestar) cls += " acct-tile-orestar";

      // Color the discrepancy tile by severity
      let extraStyle = "";
      if (field === "orestar_discrepancy" && val !== null) {
        const abs = Math.abs(val);
        const sev = discrepancySeverity(abs);
        if (sev === "red") extraStyle = ' style="border-color:#fc8181;background:#fff5f5"';
        else if (sev === "yellow") extraStyle = ' style="border-color:#ecc94b;background:#fffff0"';
      }

      const helpBtn = meta.tip
        ? `<span class="acct-tile-help" tabindex="0" role="button" aria-label="Info">?<span class="acct-tile-tip">${meta.tip}</span></span>`
        : "";

      const displayVal = val != null ? fmt$(val) : "N/A";

      return `<div class="${cls}"${extraStyle}>
        ${helpBtn}
        <div class="acct-tile-label">${esc(meta.label)}</div>
        <div class="acct-tile-value">${displayVal}</div>
      </div>`;
    }).join("");

    if (!tiles.trim()) return "";

    return `<div class="acct-group">
      <div class="acct-group-title">${esc(group.title)}</div>
      <div class="acct-tile-grid">${tiles}</div>
    </div>`;
  }).join("");

  // Per-year discrepancy warnings
  const disc = profile.yearly_discrepancies;
  if (disc && Object.keys(disc).length > 0) {
    const discYears = Object.keys(disc).sort();
    let discHTML = `<div class="acct-group">
      <div class="acct-group-title">Yearly Discrepancies (Calculated vs. ORESTAR)</div>
      <div class="disc-table">
        <div class="disc-table-header">
          <span class="disc-col-year">Year</span>
          <span class="disc-col-num">Our Begin</span>
          <span class="disc-col-num">Our Net</span>
          <span class="disc-col-num">Our End</span>
          <span class="disc-col-num">ORESTAR End</span>
          <span class="disc-col-num disc-col-diff">Difference</span>
        </div>`;
    // If a specific year is selected, show only that year; otherwise show all
    const showYears = year ? discYears.filter(y => y === year) : discYears;
    for (const yr of showYears) {
      const d = disc[yr];
      const severity = Math.abs(d.discrepancy) >= 10000 ? "disc-severe"
        : Math.abs(d.discrepancy) >= 1000 ? "disc-warn" : "disc-minor";
      discHTML += `<div class="disc-row ${severity}">
        <span class="disc-col-year">${yr}</span>
        <span class="disc-col-num">${fmt$(d.our_begin)}</span>
        <span class="disc-col-num">${fmt$(d.our_net)}</span>
        <span class="disc-col-num">${fmt$(d.our_end)}</span>
        <span class="disc-col-num">${fmt$(d.orestar_end)}</span>
        <span class="disc-col-num disc-col-diff">${d.discrepancy > 0 ? "+" : ""}${fmt$(d.discrepancy)}</span>
      </div>`;
    }
    discHTML += `</div></div>`;
    grid.innerHTML += discHTML;
  }
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

let selectedDonorTypeGroup = null;

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

/** Compact dollar format for chart axis ticks: $1.2B, $50M, $500K, $50 */
function fmtCompact$(n) {
  if (n === null || n === undefined || isNaN(n)) return "";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e9) return sign + "$" + (abs / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (abs >= 1e6) return sign + "$" + (abs / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1e3) return sign + "$" + (abs / 1e3).toFixed(0) + "K";
  return sign + "$" + abs.toFixed(0);
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

function makeBarChart(containerId, labels, values, label, color = "#3182ce") {
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.className = "echart-container-tall";

  const chart = initEChart(el);
  chart.setOption({
    tooltip: {
      trigger: 'axis',
      position: tooltipPosition,
      axisPointer: { type: 'shadow' },
      formatter: params => params[0] ? `${params[0].name}<br/>${fmt$(params[0].value)}` : '',
    },
    grid: {
      left: IS_MOBILE ? 120 : 180,
      right: 30,
      top: 10,
      bottom: 20,
    },
    xAxis: {
      type: 'value',
      axisLabel: {
        formatter: v => fmtCompact$(v),
        fontSize: IS_MOBILE ? 9 : 12,
      },
    },
    yAxis: {
      type: 'category',
      data: labels.slice().reverse(),
      axisLabel: {
        fontSize: IS_MOBILE ? 10 : 12,
        width: IS_MOBILE ? 110 : 170,
        overflow: 'truncate',
        ellipsis: '...',
      },
    },
    series: [{
      type: 'bar',
      data: values.slice().reverse(),
      itemStyle: {
        color: color,
        borderRadius: [0, 4, 4, 0],
      },
      barMaxWidth: 30,
    }],
  });

  return chart;
}

function makeLineChart(containerId, labels, datasets) {
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.className = "echart-container";

  const chart = initEChart(el);
  const series = datasets.map(ds => ({
    name: ds.label,
    type: 'line',
    data: ds.data,
    symbol: 'none',
    lineStyle: { color: ds.borderColor, width: 2 },
    itemStyle: { color: ds.borderColor },
    areaStyle: ds.fill ? { color: ds.backgroundColor, opacity: 0.3 } : undefined,
    smooth: ds.tension ? true : false,
  }));

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      position: tooltipPosition,
      formatter: params => {
        let html = `<div style="font-weight:600;margin-bottom:4px">${params[0]?.axisValue || ''}</div>`;
        params.forEach(p => {
          html += `<div>${p.marker} ${p.seriesName}: <strong>${fmt$(p.value)}</strong></div>`;
        });
        return html;
      },
    },
    legend: {
      data: datasets.map(d => d.label),
      top: 0,
      textStyle: { fontSize: IS_MOBILE ? 10 : 12 },
    },
    grid: {
      left: IS_MOBILE ? 10 : 20,
      right: IS_MOBILE ? 10 : 20,
      top: 35,
      bottom: IS_MOBILE ? 80 : 40,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: {
        fontSize: IS_MOBILE ? 9 : 11,
        rotate: 45,
      },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: v => fmtCompact$(v),
        fontSize: IS_MOBILE ? 9 : 12,
      },
    },
    dataZoom: IS_MOBILE ? [
      { type: 'slider', start: 70, end: 100, height: 25, bottom: 5 },
      { type: 'inside' },
    ] : [{ type: 'inside' }],
    series,
  });

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
    // Restore default range display
    dateStartEl.value = "2006-01-01";
    dateEndEl.value   = (summaryData && summaryData.date_range_end) || "";
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
  if (!dsEl.value) dsEl.value = "2006-01-01";
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
  if (cohNote) cohNote.textContent = "Calculated from transaction data";
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
  resetChartBox();
  if (byTypeRows.length && byTypeDataGlobal && byTypeDataGlobal.by_month) {
    document.getElementById("overview-donut-title").textContent = "Who Funds Oregon Campaigns";
    // Filter by_month data to the active date range
    let filteredByMonth = byTypeDataGlobal.by_month;
    if (hasDate) {
      const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
      const em = state.dateEnd ? state.dateEnd.slice(0, 7) : null;
      filteredByMonth = {};
      for (const [m, entries] of Object.entries(byTypeDataGlobal.by_month)) {
        if ((!sm || m >= sm) && (!em || m <= em)) filteredByMonth[m] = entries;
      }
    }
    makeStackedAreaChart("chart-contributor-type", filteredByMonth, byTypeRows);
  } else if (byTypeRows.length) {
    makeSimplePieChart("chart-contributor-type", byTypeRows);
  }

  document.getElementById("filer-comparison-grid").hidden = true;
  renderAcctSummary(null);  // No per-filer account summary in global view
  fitStatCards();

  // Fundraising Pulse and Races to Watch are fixed-period snapshots —
  // hide them when a custom date filter is active
  const pulseEl = document.getElementById("campaign-pulse");
  const racesEl = document.getElementById("races-to-watch");
  if (hasDate) {
    if (pulseEl) pulseEl.hidden = true;
    if (racesEl) racesEl.hidden = true;
  } else {
    loadCampaignPulse();
  }
  loadPartyFundraising();
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
      <div class="disc-row"><span>Calculated cash on hand:</span><span>${fmt$(profile.cash_on_hand)}</span></div>
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
    ind.title = "Estimated: no ORESTAR beginning balance data scraped yet. Cash on hand is calculated from transactions with a $0 starting balance.";
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
    return '<span class="coh-indicator coh-estimated" tabindex="0" title="Estimated: no ORESTAR beginning balance data scraped yet">EST</span>';
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
  const pulseEl = document.getElementById("campaign-pulse");
  if (pulseEl) pulseEl.hidden = true;
  const partyBox = document.getElementById("party-fundraising-box");
  if (partyBox) partyBox.hidden = true;
  const racesBox = document.getElementById("races-to-watch");
  if (racesBox) racesBox.hidden = true;
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
  if (cohNote) cohNote.textContent = "Calculated from transaction data";
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
  resetChartBox();
  if (byTypeRows.length) {
    if (profile.by_contributor_type_by_month) {
      makeStackedAreaChart("chart-contributor-type", profile.by_contributor_type_by_month, byTypeRows);
    } else {
      makeSimplePieChart("chart-contributor-type", byTypeRows);
    }
  }

  document.getElementById("filer-comparison-grid").hidden = true;
  renderAcctSummary(profile);
  fitStatCards();
}

function renderOverviewMultiFiler(profiles) {
  const pulseEl = document.getElementById("campaign-pulse");
  if (pulseEl) pulseEl.hidden = true;
  const partyBox = document.getElementById("party-fundraising-box");
  if (partyBox) partyBox.hidden = true;
  const racesBox = document.getElementById("races-to-watch");
  if (racesBox) racesBox.hidden = true;
  document.getElementById("stat-cards").hidden             = true;
  document.getElementById("filer-comparison-grid").hidden  = false;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent =
    "Funding Mix Comparison";

  const hasDate = state.dateStart || state.dateEnd;

  // Build radar chart + table in a two-card layout
  resetChartBox();
  const donutBox = document.getElementById("overview-donut-box");

  // Create the two-card layout wrapper
  let radarLayout = donutBox.querySelector(".radar-layout");
  if (radarLayout) radarLayout.remove();
  radarLayout = document.createElement("div");
  radarLayout.className = "radar-layout";

  // Card 1: Radar chart
  const radarCard = document.createElement("div");
  radarCard.className = "radar-card";
  const radarChartDiv = document.createElement("div");
  radarChartDiv.id = "chart-radar-multi";
  radarChartDiv.className = IS_MOBILE ? "echart-container-radar" : "echart-container";
  radarCard.appendChild(radarChartDiv);
  radarLayout.appendChild(radarCard);

  // Card 2: Table (built later)
  const tableCard = document.createElement("div");
  tableCard.className = "radar-card radar-card-table";
  radarLayout.appendChild(tableCard);

  // Hide the main chart-contributor-type (used for single-filer stacked area)
  const mainChart = document.getElementById("chart-contributor-type");
  if (mainChart) mainChart.style.display = "none";

  donutBox.appendChild(radarLayout);

  const el = radarChartDiv;
  if (el) {
    const chart = initEChart(el);

    // Collect per-filer type data, merge to base types
    const perFilerRows = profiles.map(p => hasDate && p.by_contributor_type_by_month
      ? mergeTypeByMonth(p.by_contributor_type_by_month)
      : hasDate
        ? mergeTypeByYear(p.by_contributor_type_by_year || {}, yearsInRange())
        : (p.by_contributor_type || []));

    // Get all base types across all filers
    const baseTypeSet = new Set();
    perFilerRows.forEach(rows => {
      rows.forEach(r => {
        const base = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
        baseTypeSet.add(base);
      });
    });
    // Sort by total across all filers
    const baseTotals = {};
    perFilerRows.forEach(rows => {
      rows.forEach(r => {
        const base = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
        baseTotals[base] = (baseTotals[base] || 0) + r.total;
      });
    });
    const baseTypes = [...baseTypeSet].sort((a, b) => (baseTotals[b] || 0) - (baseTotals[a] || 0));

    // Build percentage data + dollar amounts per filer
    const filerData = profiles.map((p, i) => {
      const rows = perFilerRows[i];
      const byBase = {};
      const topDonorsByBase = {};
      let total = 0;
      rows.forEach(r => {
        const base = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
        byBase[base] = (byBase[base] || 0) + r.total;
        total += r.total;
        // Merge top donors across in-state/out-of-state
        if (r.top_donors) {
          if (!topDonorsByBase[base]) topDonorsByBase[base] = {};
          r.top_donors.forEach(d => {
            topDonorsByBase[base][d.name] = (topDonorsByBase[base][d.name] || 0) + d.total;
          });
        }
      });
      // Sort and take top 5 per type
      for (const bt of Object.keys(topDonorsByBase)) {
        topDonorsByBase[bt] = Object.entries(topDonorsByBase[bt])
          .sort((a, b) => b[1] - a[1])
          .slice(0, 5)
          .map(([name, amt]) => ({ name, total: amt }));
      }
      return { byBase, topDonorsByBase, total };
    });

    const series = profiles.map((p, i) => {
      const { byBase, total } = filerData[i];
      const color = PALETTE[i % PALETTE.length];
      return {
        name: p.name,
        type: 'radar',
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: { width: 2, color },
        itemStyle: { color },
        areaStyle: { color, opacity: 0.15 },
        data: [{
          value: baseTypes.map(bt => total > 0 ? Math.round(byBase[bt] || 0) / total * 100 : 0),
          name: p.name,
        }],
      };
    });

    chart.setOption({
      tooltip: {
        trigger: 'item',
        position: tooltipPosition,
        formatter: function(params) {
          const name = params.name;
          const values = params.value;
          let html = `<div style="font-weight:600;margin-bottom:4px">${name}</div>`;
          baseTypes.forEach((bt, i) => {
            if (values[i] > 0) {
              html += `<div style="display:flex;justify-content:space-between;gap:12px;font-size:12px">
                <span>${shortTypeName(bt)}</span><span style="font-weight:500">${values[i].toFixed(1)}%</span>
              </div>`;
            }
          });
          return html;
        },
      },
      legend: {
        data: profiles.map(p => p.name),
        bottom: 0,
        itemGap: IS_MOBILE ? 8 : 20,
        textStyle: { fontSize: IS_MOBILE ? 8 : 12 },
      },
      radar: {
        indicator: (() => {
          // Uniform scale: same max on all axes so each ring = same %
          let globalMax = 0;
          series.forEach(s => {
            s.data[0].value.forEach(v => { if (v > globalMax) globalMax = v; });
          });
          const uniformMax = Math.max(Math.ceil(globalMax * 1.15), 1);
          return baseTypes.map(bt => ({ name: shortTypeName(bt), max: uniformMax }));
        })(),
        shape: 'polygon',
        radius: IS_MOBILE ? '35%' : '65%',
        center: ['50%', '48%'],
        axisName: {
          fontSize: IS_MOBILE ? 8 : 11,
          color: '#6b7280',
        },
        splitArea: { areaStyle: { color: ['#fff', '#f9fafb'] } },
        splitLine: { lineStyle: { color: '#e5e7eb' } },
        axisLine: { lineStyle: { color: '#e5e7eb' } },
      },
      series,
    });

    // Build comparison table below the radar chart
    const tableDiv = document.createElement("div");
    tableDiv.className = "radar-compare-table";

    // Build row data for sortable table
    const tableRows = baseTypes.map(bt => {
      const row = { type: bt, amounts: [], donors: [] };
      profiles.forEach((p, i) => {
        row.amounts.push(filerData[i].byBase[bt] || 0);
        row.donors.push(filerData[i].topDonorsByBase[bt] || []);
      });
      return row;
    });

    let sortCol = -1; // -1 = type name, 0+ = filer index
    let sortDir = "desc";

    function renderRadarTable() {
      const sorted = [...tableRows].sort((a, b) => {
        if (sortCol === -1) {
          return sortDir === "asc"
            ? a.type.localeCompare(b.type)
            : b.type.localeCompare(a.type);
        }
        return sortDir === "asc"
          ? a.amounts[sortCol] - b.amounts[sortCol]
          : b.amounts[sortCol] - a.amounts[sortCol];
      });

      let thtml = `<table class="data-table radar-table"><thead><tr>
        <th class="sortable radar-sort" data-col="-1">Donor Type</th>`;
      profiles.forEach((p, i) => {
        const color = PALETTE[i % PALETTE.length];
        thtml += `<th class="sortable radar-sort num" data-col="${i}" style="color:${color}">${esc(p.name)}</th>`;
      });
      thtml += `</tr></thead><tbody>`;

      sorted.forEach(row => {
        thtml += `<tr><td class="radar-table-type">${esc(shortTypeName(row.type))}</td>`;
        profiles.forEach((p, i) => {
          const amt = row.amounts[i];
          const pct = filerData[i].total > 0 ? (amt / filerData[i].total * 100).toFixed(1) : "0.0";
          const donors = row.donors[i];
          const hasDonors = donors.length > 0;
          thtml += `<td class="num${hasDonors ? ' radar-cell-hover' : ''}"${hasDonors ? ` data-bt="${esc(row.type)}" data-fi="${i}"` : ''}>${fmtCompact$(amt)} <span class="radar-pct">${pct}%</span></td>`;
        });
        thtml += `</tr>`;
      });

      thtml += `<tr class="radar-table-total"><td>Total</td>`;
      profiles.forEach((p, i) => {
        thtml += `<td class="num">${fmtCompact$(filerData[i].total)}</td>`;
      });
      thtml += `</tr></tbody></table>`;

      tableDiv.innerHTML = thtml;

      // Mark current sort column
      tableDiv.querySelectorAll(".radar-sort").forEach(th => {
        const col = parseInt(th.dataset.col);
        th.classList.remove("sort-asc", "sort-desc");
        if (col === sortCol) th.classList.add("sort-" + sortDir);
      });

      // Wire sort clicks
      tableDiv.querySelectorAll(".radar-sort").forEach(th => {
        th.style.cursor = "pointer";
        th.addEventListener("click", () => {
          const col = parseInt(th.dataset.col);
          if (col === sortCol) {
            sortDir = sortDir === "desc" ? "asc" : "desc";
          } else {
            sortCol = col;
            sortDir = col === -1 ? "asc" : "desc";
          }
          renderRadarTable();
        });
      });

      // Wire hover tooltips
      wireRadarTooltips();
    }

    // Create a shared tooltip div
    let radarTip = document.getElementById("radar-table-tip");
    if (!radarTip) {
      radarTip = document.createElement("div");
      radarTip.id = "radar-table-tip";
      radarTip.className = "radar-table-tip";
      document.body.appendChild(radarTip);
    }

    function wireRadarTooltips() {
      tableDiv.querySelectorAll(".radar-cell-hover").forEach(cell => {
        const bt = cell.dataset.bt;
        const fi = parseInt(cell.dataset.fi);
        const row = tableRows.find(r => r.type === bt);
        if (!row) return;
        const donors = row.donors[fi] || [];
        if (!donors.length) return;

        cell.addEventListener("mouseenter", () => {
          let html = `<div style="font-weight:600;margin-bottom:4px">Top ${shortTypeName(bt)} Donors</div>`;
          html += `<div style="font-size:11px;color:#6b7280;margin-bottom:4px">${esc(profiles[fi].name)}</div>`;
          donors.forEach(d => {
            html += `<div style="display:flex;justify-content:space-between;gap:12px;font-size:12px;padding:1px 0">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(d.name)}</span>
              <span style="font-weight:500;white-space:nowrap">${fmtCompact$(d.total)}</span>
            </div>`;
          });
          radarTip.innerHTML = html;
          radarTip.hidden = false;
          const rect = cell.getBoundingClientRect();
          let left = rect.left;
          let top = rect.bottom + 6;
          if (left + 260 > window.innerWidth) left = window.innerWidth - 265;
          if (left < 5) left = 5;
          radarTip.style.left = left + "px";
          radarTip.style.top = (top + window.scrollY) + "px";
        });

        cell.addEventListener("mouseleave", () => {
          radarTip.hidden = true;
        });
      });
    }

    tableCard.appendChild(tableDiv);
    renderRadarTable();
  }

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
      const searchEl = document.getElementById("donors-summary-search");
      if (searchEl) searchEl.value = "";
      buildSortableTable("table-donors", allTimeDonors, [
        { key: "name",  label: "Donor", linkDonor: true },
        { key: "total", label: "Total ($)", fmt: fmt$, cls: "num" },
      ], searchEl);
    }

  } else {
    // Multi-filer: pivot table
    donorsViewMode = "summary";
    document.getElementById("donors-view-toggle").hidden      = true;
    document.getElementById("donors-global-view").hidden      = true;
    document.getElementById("donors-multi-view").hidden       = false;

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

  // Scroll to the right so the most recent years are visible first
  const scrollBox = document.querySelector('.by-year-scroll');
  if (scrollBox) {
    setTimeout(() => { scrollBox.scrollLeft = scrollBox.scrollWidth; }, 0);
  }
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
    renderTimeline("all");

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
      ? timelineData.filter(r => r.month >= "2006-01")
      : timelineData.filter(r => r.month.startsWith(year));
  }

  makeLineChart(
    "chart-timeline",
    rows.map(r => r.month),
    [
      {
        label: "Contributions",
        data: rows.map(r => r.contributions || 0),
        borderColor: "#16a34a",
        backgroundColor: "rgba(22,163,74,0.08)",
        fill: true,
        tension: 0.3,
        pointRadius: rows.length > 60 ? 0 : 3,
      },
      {
        label: "Expenditures",
        data: rows.map(r => r.expenditures || 0),
        borderColor: "#d97706",
        backgroundColor: "rgba(217,119,6,0.08)",
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

// ── Party Fundraising ───────────────────────────────────────────────────
async function loadPartyFundraising() {
  try {
    const data = await fetchJSON(`${DATA}/by_party_type.json`);
    if (!data || !data.by_year) return;
    const box = document.getElementById("party-fundraising-box");
    if (!box) return;
    box.hidden = false;

    // Aggregate years by BASE donor type, tracking in-state vs out-of-state separately
    // Respect the active date range filter
    const demIn = {}, demOut = {}, repIn = {}, repOut = {};
    const startYr = state.dateStart ? state.dateStart.slice(0, 4) : "2006";
    const endYr = state.dateEnd ? state.dateEnd.slice(0, 4) : "9999";
    const years = Object.keys(data.by_year).filter(y => y >= startYr && y <= endYr);
    for (const y of years) {
      for (const t of (data.by_year[y]?.Democrat || [])) {
        const isOOS = t.type.endsWith(" (out of state)");
        const base = isOOS ? t.type.slice(0, -15) : t.type;
        if (isOOS) { demOut[base] = (demOut[base] || 0) + t.total; }
        else       { demIn[base]  = (demIn[base] || 0)  + t.total; }
      }
      for (const t of (data.by_year[y]?.Republican || [])) {
        const isOOS = t.type.endsWith(" (out of state)");
        const base = isOOS ? t.type.slice(0, -15) : t.type;
        if (isOOS) { repOut[base] = (repOut[base] || 0) + t.total; }
        else       { repIn[base]  = (repIn[base] || 0)  + t.total; }
      }
    }

    // Get all unique base types, sorted by combined total
    const allTotals = {};
    for (const obj of [demIn, demOut, repIn, repOut]) {
      for (const [k, v] of Object.entries(obj)) allTotals[k] = (allTotals[k] || 0) + v;
    }
    const allTypes = Object.keys(allTotals).sort((a, b) => allTotals[b] - allTotals[a]);

    const displayLabels = allTypes.map(t => shortTypeName(t));

    const el = document.getElementById("chart-party-fundraising");
    if (!el) return;
    el.className = "echart-container-compact";

    // Dispose existing
    const existing = echarts.getInstanceByDom(el);
    if (existing) existing.dispose();

    const chart = echarts.init(el, null, { renderer: 'svg' });
    chart.setOption({
      tooltip: {
        trigger: 'axis',
        position: tooltipPosition,
        axisPointer: { type: 'shadow' },
        formatter: params => {
          if (!params || !params.length) return '';
          let html = `<div style="font-weight:600;margin-bottom:4px">${params[0]?.name || ''}</div>`;
          // Group by party, sum in-state + out-of-state
          let demTotal = 0, repTotal = 0;
          params.forEach(p => {
            if (p.seriesName.startsWith('Dem')) demTotal += p.value || 0;
            if (p.seriesName.startsWith('Rep')) repTotal += p.value || 0;
          });
          params.forEach(p => {
            if (p.value) html += `<div>${p.marker} ${p.seriesName}: <strong>${fmt$(p.value)}</strong></div>`;
          });
          if (demTotal && repTotal) {
            html += `<div style="border-top:1px solid #eee;margin-top:3px;padding-top:3px;font-size:11px;color:#666">
              Dem total: ${fmt$(demTotal)} · Rep total: ${fmt$(repTotal)}</div>`;
          }
          return html;
        },
      },
      legend: {
        data: ['Dem (in-state)', 'Dem (out of state)', 'Rep (in-state)', 'Rep (out of state)'],
        top: 0,
        itemGap: IS_MOBILE ? 10 : 20,
        padding: [0, 0, 8, 0],
        textStyle: { fontSize: IS_MOBILE ? 9 : 11 },
      },
      grid: {
        left: IS_MOBILE ? 10 : 20,
        right: IS_MOBILE ? 10 : 20,
        top: 55,
        bottom: 10,
        containLabel: true,
      },
      xAxis: {
        type: 'category',
        data: displayLabels,
        axisLabel: {
          rotate: 30,
          fontSize: IS_MOBILE ? 9 : 12,
          interval: 0,
        },
      },
      yAxis: {
        type: 'value',
        axisLabel: {
          formatter: v => fmtCompact$(v),
          fontSize: IS_MOBILE ? 9 : 12,
        },
      },
      series: [
        {
          name: 'Dem (in-state)',
          type: 'bar',
          stack: 'dem',
          data: allTypes.map(t => demIn[t] || 0),
          itemStyle: { color: '#2563eb' },
        },
        {
          name: 'Dem (out of state)',
          type: 'bar',
          stack: 'dem',
          data: allTypes.map(t => demOut[t] || 0),
          itemStyle: { color: '#93bbfd' },
        },
        {
          name: 'Rep (in-state)',
          type: 'bar',
          stack: 'rep',
          data: allTypes.map(t => repIn[t] || 0),
          itemStyle: { color: '#dc2626' },
        },
        {
          name: 'Rep (out of state)',
          type: 'bar',
          stack: 'rep',
          data: allTypes.map(t => repOut[t] || 0),
          itemStyle: { color: '#fca5a5' },
        },
      ],
    });
  } catch (e) { console.warn("Party fundraising load failed:", e); }
}

// ── Races to Watch ──────────────────────────────────────────────────────
function renderRacesToWatch() {
  if (!activitySnapshot || !activitySnapshot.races || !activitySnapshot.races.length) return;
  const races = activitySnapshot.races;
  const el = document.getElementById("races-to-watch");
  const list = document.getElementById("races-list");
  if (!el || !list) return;
  el.hidden = false;

  function raceRowHTML(c) {
    const partyClass = c.party === "Democrat" ? "party-d" : c.party === "Republican" ? "party-r" : "party-other";
    const badge = c.party ? `<span class="pulse-entry-party ${partyClass}">${c.party.charAt(0)}</span>` : "";
    return `<div class="race-row" ${c.slug ? `data-slug="${esc(c.slug)}" style="cursor:pointer"` : ""}>
        <span class="race-col-name">${badge} ${esc(c.candidate_name || c.name)}</span>
        <span class="race-col-raised">${fmt$(c.raised_cycle)}</span>
        <span class="race-col-coh">${fmt$(c.cash_on_hand)}</span>
      </div>`;
  }

  let html = "";
  for (let ri = 0; ri < races.length; ri++) {
    const race = races[ri];
    const visible = race.candidates.slice(0, 5);
    const extra = race.candidates.slice(5);
    html += `<div class="race-card">
      <div class="race-card-header">
        <span class="race-office">${esc(race.office)}</span>
        <span class="race-total">${fmt$(race.total_raised)}</span>
      </div>
      <div class="race-table">
        <div class="race-table-header">
          <span class="race-col-name">Candidate</span>
          <span class="race-col-raised">Raised</span>
          <span class="race-col-coh">Cash on Hand</span>
        </div>`;
    for (const c of visible) html += raceRowHTML(c);
    if (extra.length > 0) {
      html += `<div class="race-extra" id="race-extra-${ri}" hidden>`;
      for (const c of extra) html += raceRowHTML(c);
      html += `</div>`;
      html += `<div class="race-show-more" data-race-idx="${ri}">Show ${extra.length} more</div>`;
    }
    html += `</div></div>`;
  }
  list.innerHTML = html;

  // Wire up click-to-navigate on race rows
  list.querySelectorAll(".race-row[data-slug]").forEach(row => {
    row.addEventListener("click", () => selectFilerBySlug(row.dataset.slug));
  });

  // Wire up show-more toggles
  list.querySelectorAll(".race-show-more").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx = btn.dataset.raceIdx;
      const extra = document.getElementById(`race-extra-${idx}`);
      if (!extra) return;
      extra.hidden = !extra.hidden;
      btn.textContent = extra.hidden
        ? `Show ${extra.children.length} more`
        : "Show fewer";
    });
  });
}

// ── Fundraising Pulse ────────────────────────────────────────────────────
let activitySnapshot = null;
let pulseCurrentPeriod = "30d";
const PULSE_ROWS = 3;

async function loadCampaignPulse() {
  try {
    const resp = await fetch(`${DATA}/activity_snapshot.json`);
    if (!resp.ok) return;
    activitySnapshot = await resp.json();
    await ensureDonorFilerMap();
    const el = document.getElementById("campaign-pulse");
    if (el) { el.hidden = false; renderPulsePeriod(pulseCurrentPeriod); }
    renderRacesToWatch();
    // Wire up period toggle
    const toggle = document.getElementById("pulse-period-toggle");
    if (toggle) {
      toggle.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-period]");
        if (!btn) return;
        toggle.querySelectorAll(".toggle-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        pulseCurrentPeriod = btn.dataset.period;
        renderPulsePeriod(pulseCurrentPeriod);
      });
    }
  } catch (e) { console.warn("Campaign Pulse load failed:", e); }
}

function renderPulsePeriod(periodKey) {
  if (!activitySnapshot) return;
  const period = activitySnapshot.periods[periodKey];
  if (!period) return;
  renderPulseRaising(period);
  renderPulseMomentum(period);
  renderPulseDonors(period);

  // Wire up click-to-expand on pulse entry names
  document.querySelectorAll(".pulse-entry-name").forEach(el => {
    el.style.cursor = "pointer";
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      el.classList.toggle("expanded");
    });
  });
}

function pulseEntryHTML(entry, valueHTML, extra = "") {
  const partyClass = entry.party === "Democrat" ? "party-d" : entry.party === "Republican" ? "party-r" : "party-other";
  const partyBadge = entry.party ? `<span class="pulse-entry-party ${partyClass}">${entry.party.charAt(0)}</span>` : "";
  const slug = entry.slug || "";
  return `<div class="pulse-entry" ${slug ? `data-slug="${slug}" onclick="selectFilerBySlug('${slug}')"` : ""}>
    ${partyBadge}
    <span class="pulse-entry-name" title="${entry.name || ''}">${entry.name || ''}</span>
    <span class="pulse-entry-value">${valueHTML}</span>
    ${extra}
  </div>`;
}

function renderPulseRaising(data) {
  const body = document.getElementById("pulse-raising-body");
  if (!body) return;
  let html = "";
  const tiers = [
    { key: "statewide", label: "Statewide" },
    { key: "legislative", label: "Legislative" },
    { key: "local", label: "Local" },
  ];
  for (const tier of tiers) {
    const entries = (data.by_office_tier[tier.key] || []).slice(0, PULSE_ROWS);
    if (!entries.length) continue;
    html += `<div class="pulse-tier-label">${tier.label}</div>`;
    for (const e of entries) {
      html += pulseEntryHTML(e, "$" + Number(e.raised).toLocaleString("en-US", { maximumFractionDigits: 0 }));
    }
  }
  body.innerHTML = html || '<div class="pulse-entry-meta">No data</div>';
}

function renderPulseMomentum(data) {
  const body = document.getElementById("pulse-momentum-body");
  if (!body) return;
  let html = "";
  const entries = (data.top_growth || []).slice(0, PULSE_ROWS * 2);
  for (const e of entries) {
    const growthText = e.is_new
      ? `<span class="pulse-new-badge">New</span>`
      : `<span class="pulse-growth-pct">+${e.growth_pct}%</span>`;
    html += pulseEntryHTML(e,
      "$" + Number(e.raised).toLocaleString("en-US", { maximumFractionDigits: 0 }),
      growthText
    );
  }
  body.innerHTML = html || '<div class="pulse-entry-meta">No data</div>';
}

function renderPulseDonors(data) {
  const body = document.getElementById("pulse-donors-body");
  if (!body) return;
  let html = "";
  const entries = (data.top_donors || []).slice(0, PULSE_ROWS * 2);
  for (const e of entries) {
    const hasDetails = e.details && e.details.length > 1;
    const donorKey = e.name.toLowerCase();
    const filerLink = donorFilerMap && donorFilerMap[donorKey];
    const nameHTML = filerLink
      ? `<a href="#" class="pulse-entry-name pulse-donor-link" onclick="event.preventDefault();selectFilerBySlug('${esc(filerLink.slug)}')" title="${esc(e.name)}">${esc(e.name)}</a>`
      : `<span class="pulse-entry-name" title="${esc(e.name)}">${esc(e.name)}</span>`;

    const metaHTML = hasDetails
      ? `<div class="pulse-donor-cmtes pulse-donor-expand" data-donor="${esc(e.name)}">${e.committees} committees ▾</div>`
      : (e.committees > 1 ? `<div class="pulse-donor-cmtes">${e.committees} committees</div>` : "");

    html += `<div class="pulse-entry pulse-donor-entry">
      <div class="pulse-donor-top">
        ${nameHTML}
        <span class="pulse-entry-value">$${Number(e.total).toLocaleString("en-US", { maximumFractionDigits: 0 })}</span>
      </div>
      ${metaHTML}
    </div>`;

    if (hasDetails) {
      html += `<div class="pulse-donor-details" data-donor-details="${esc(e.name)}" hidden>`;
      for (const d of e.details) {
        const detailClick = d.slug ? `onclick="selectFilerBySlug('${esc(d.slug)}')" style="cursor:pointer"` : "";
        html += `<div class="pulse-donor-detail" ${detailClick}>
          <span class="pulse-donor-detail-name">${esc(d.filer)}</span>
          <span class="pulse-donor-detail-amt">$${Number(d.amount).toLocaleString("en-US", { maximumFractionDigits: 0 })}</span>
        </div>`;
      }
      html += `</div>`;
    }
  }
  body.innerHTML = html || '<div class="pulse-entry-meta">No data</div>';

  // Wire up expand/collapse
  body.querySelectorAll(".pulse-donor-expand").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.donor;
      const details = body.querySelector(`[data-donor-details="${name}"]`);
      if (details) {
        details.hidden = !details.hidden;
        btn.textContent = details.hidden ? `${btn.textContent.replace(" ▴", "")} ▾`.replace(" ▾ ▾", " ▾") : btn.textContent.replace(" ▾", " ▴");
      }
    });
  });
}

function selectFilerBySlug(slug) {
  // Navigate to the filer's page via the existing filer selector
  if (!filerIndex) return;
  const filer = filerIndex.find(f => f.slug === slug);
  if (!filer) return;
  // Clear existing selections and select this filer
  state.selectedFilers = [filer];
  // Trigger re-render
  navigateToFiler(slug);
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
