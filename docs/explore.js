/**
 * explore.js — filter, download, and query the full transactions dataset.
 *
 * Uses the anon Supabase client (lib/supabase.js):
 *   • Filtered browse/download → live SELECT on the public `transactions` table
 *   • Full-dataset download    → public Storage object (exports/transactions.csv.gz)
 *   • Ad-hoc SQL               → sql-query Edge Function (read-only role)
 */
"use strict";

const PAGE_SIZE = 100;
const STORAGE_BUCKET = "exports";
const FULL_CSV_OBJECT = "transactions.csv.gz";

// Columns shown in the browse table (subset of the full row).
// NOTE: contributor type lives in `book_type` on transaction rows.
// `contributor_type_label`, `party`, and `office` are empty in ORESTAR's
// transaction export (party/office are filer-level metadata), so they are
// deliberately not offered as filters or columns here.
const COLS = [
  { key: "tran_date",                   label: "Date" },
  { key: "tran_type",                   label: "Type" },
  { key: "amount",                      label: "Amount", num: true },
  { key: "filer_canonical",             label: "Committee" },
  { key: "contributor_payee_canonical", label: "Donor / Payee" },
  { key: "book_type",                   label: "Contributor type" },
  { key: "city",                        label: "City" },
  { key: "state",                       label: "State" },
  { key: "employer",                    label: "Employer" },
  { key: "occupation",                  label: "Occupation" },
  { key: "purpose",                     label: "Purpose" },
];
const SELECT_COLS = COLS.map(c => c.key).join(",") + ",tran_id";

let page = 0;
let sortCol = "tran_date";
let sortDir = false; // false = descending
let lastPageCount = 0;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtAmount = v => (v == null || v === "") ? "" : Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });

function readFilters() {
  return {
    filer: $("f-filer").value.trim(),
    payee: $("f-payee").value.trim(),
    type: $("f-type").value,
    ctype: $("f-ctype").value.trim(),
    dateStart: $("f-date-start").value,
    dateEnd: $("f-date-end").value,
    amtMin: $("f-amt-min").value,
    amtMax: $("f-amt-max").value,
  };
}

/** Apply the current filters to a supabase query builder. */
function applyFilters(q, f) {
  if (f.filer)     q = q.ilike("filer_canonical", `%${f.filer}%`);
  if (f.payee)     q = q.ilike("contributor_payee_canonical", `%${f.payee}%`);
  if (f.type)      q = q.eq("tran_type", f.type);
  if (f.ctype)     q = q.ilike("book_type", `%${f.ctype}%`);
  if (f.dateStart) q = q.gte("tran_date", f.dateStart);
  if (f.dateEnd)   q = q.lte("tran_date", f.dateEnd);
  if (f.amtMin !== "") q = q.gte("amount", Number(f.amtMin));
  if (f.amtMax !== "") q = q.lte("amount", Number(f.amtMax));
  return q;
}

function showError(el, msg) {
  const box = $(el);
  if (!msg) { box.hidden = true; box.textContent = ""; return; }
  box.hidden = false;
  box.textContent = msg;
}

// ── Browse ──────────────────────────────────────────────────────────────────
async function runSearch() {
  showError("xp-error", "");
  $("xp-status").textContent = "Loading…";
  try {
    const sb = await getSupabase();
    let q = applyFilters(sb.from("transactions").select(SELECT_COLS), readFilters());
    q = q.order(sortCol, { ascending: sortDir }).range(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE - 1);
    const { data, error } = await q;
    if (error) throw new Error(error.message);
    lastPageCount = data.length;
    renderTable(data);
    $("xp-status").textContent = data.length
      ? `Showing ${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + data.length}`
      : "No matching transactions";
    $("pg-prev").disabled = page === 0;
    $("pg-next").disabled = data.length < PAGE_SIZE;
    $("pg-label").textContent = `Page ${page + 1}`;
  } catch (e) {
    $("xp-status").textContent = "";
    showError("xp-error", e.message);
  }
}

function renderTable(rows) {
  $("xp-thead").innerHTML = "<tr>" + COLS.map(c =>
    `<th class="${c.num ? "num" : ""}" data-col="${c.key}">${c.label}${sortCol === c.key ? (sortDir ? " ▲" : " ▼") : ""}</th>`
  ).join("") + "</tr>";
  $("xp-tbody").innerHTML = rows.map(r => "<tr>" + COLS.map(c =>
    `<td class="${c.num ? "num" : ""}">${c.num ? fmtAmount(r[c.key]) : esc(r[c.key])}</td>`
  ).join("") + "</tr>").join("");
  $("xp-thead").querySelectorAll("th").forEach(th => {
    th.onclick = () => {
      const col = th.dataset.col;
      if (sortCol === col) sortDir = !sortDir; else { sortCol = col; sortDir = false; }
      page = 0;
      runSearch();
    };
  });
}

// ── Downloads ───────────────────────────────────────────────────────────────
// The API caps any single response at 1,000 rows, so a one-shot download would
// silently truncate (a committee can have 20k+ transactions). We page through
// with .range() and assemble the CSV here, so researchers get the complete
// filtered set without weakening the server-side cap.
const DOWNLOAD_CHUNK = 1000;
const DOWNLOAD_MAX_ROWS = 100000;   // beyond this, point them at the full dataset

async function downloadFiltered() {
  showError("xp-error", "");
  const btn = $("btn-download");
  btn.disabled = true;
  try {
    const sb = await getSupabase();
    const filters = readFilters();
    let rows = [];
    let truncated = false;

    for (let offset = 0; ; offset += DOWNLOAD_CHUNK) {
      $("xp-status").textContent = `Preparing CSV… ${rows.length.toLocaleString()} rows`;
      const { data, error } = await applyFilters(sb.from("transactions").select("*"), filters)
        // Stable key so paging can't skip or repeat rows between requests.
        .order("tran_id", { ascending: true })
        .range(offset, offset + DOWNLOAD_CHUNK - 1);
      if (error) throw new Error(error.message);
      rows = rows.concat(data);
      if (data.length < DOWNLOAD_CHUNK) break;
      if (rows.length >= DOWNLOAD_MAX_ROWS) { truncated = true; break; }
    }

    if (!rows.length) {
      $("xp-status").textContent = "No rows match these filters — nothing to download.";
      return;
    }

    const cols = Object.keys(rows[0]);
    const csv = [cols.map(csvCell).join(",")]
      .concat(rows.map(r => cols.map(c => csvCell(r[c])).join(",")))
      .join("\n");
    triggerDownload(csv, "text/csv", "orestar_filtered.csv");
    $("xp-status").textContent = truncated
      ? `Downloaded first ${rows.length.toLocaleString()} rows — narrow the filters, or use the full dataset.`
      : `Downloaded ${rows.length.toLocaleString()} rows.`;
  } catch (e) {
    $("xp-status").textContent = "";
    showError("xp-error", e.message);
  } finally {
    btn.disabled = false;
  }
}

function triggerDownload(text, mime, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function wireFullDownload() {
  const sb = await getSupabase();
  const { data } = sb.storage.from(STORAGE_BUCKET).getPublicUrl(FULL_CSV_OBJECT);
  if (data?.publicUrl) $("btn-download-all").href = data.publicUrl;
}

// ── SQL box ─────────────────────────────────────────────────────────────────
let sqlColumns = [];
let sqlRows = [];

async function runSQL() {
  showError("sql-error", "");
  $("sql-status").textContent = "Running…";
  $("btn-sql-csv").disabled = true;
  try {
    const sb = await getSupabase();
    const { data, error } = await sb.functions.invoke("sql-query", { body: { sql: $("sql-input").value } });
    if (error) {
      // Edge function returned a non-2xx; surface its JSON error if present.
      let msg = error.message;
      try { msg = (await error.context.json()).error || msg; } catch { /* ignore */ }
      throw new Error(msg);
    }
    if (data.error) throw new Error(data.error);
    sqlColumns = data.columns || [];
    sqlRows = data.rows || [];
    renderSQL(sqlColumns, sqlRows);
    $("sql-status").textContent =
      `${sqlRows.length} row${sqlRows.length === 1 ? "" : "s"}${data.truncated ? " (truncated)" : ""}`;
    $("btn-sql-csv").disabled = sqlRows.length === 0;
  } catch (e) {
    $("sql-status").textContent = "";
    renderSQL([], []);
    showError("sql-error", e.message);
  }
}

function renderSQL(cols, rows) {
  $("sql-thead").innerHTML = "<tr>" + cols.map(c => `<th>${esc(c)}</th>`).join("") + "</tr>";
  $("sql-tbody").innerHTML = rows.map(r => "<tr>" + cols.map(c => `<td>${esc(r[c])}</td>`).join("") + "</tr>").join("");
}

function downloadSQLCsv() {
  const head = sqlColumns.map(csvCell).join(",");
  const body = sqlRows.map(r => sqlColumns.map(c => csvCell(r[c])).join(",")).join("\n");
  triggerDownload(head + "\n" + body, "text/csv", "orestar_query.csv");
}

function csvCell(v) {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// ── Init ────────────────────────────────────────────────────────────────────
function resetFilters() {
  ["f-filer", "f-payee", "f-ctype", "f-date-start", "f-date-end", "f-amt-min", "f-amt-max"]
    .forEach(id => { $(id).value = ""; });
  $("f-type").value = "";
  page = 0; sortCol = "tran_date"; sortDir = false;
  runSearch();
}

document.addEventListener("DOMContentLoaded", () => {
  $("btn-search").onclick = () => { page = 0; runSearch(); };
  $("btn-reset").onclick = resetFilters;
  $("btn-download").onclick = downloadFiltered;
  $("btn-sql").onclick = runSQL;
  $("btn-sql-csv").onclick = downloadSQLCsv;
  $("pg-prev").onclick = () => { if (page > 0) { page--; runSearch(); } };
  $("pg-next").onclick = () => { if (lastPageCount === PAGE_SIZE) { page++; runSearch(); } };
  ["f-filer", "f-payee", "f-ctype"].forEach(id =>
    $(id).addEventListener("keydown", e => { if (e.key === "Enter") { page = 0; runSearch(); } }));
  wireFullDownload();
  runSearch();
});
