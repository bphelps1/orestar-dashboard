/**
 * data.js — dashboard data access layer.
 *
 * The dashboard used to fetch static JSON files from data/aggregated/. It now
 * reads the same aggregate blobs from Supabase:
 *   • dashboard_cache(key, data)   — summary, timeline, top_donors, …
 *   • filer_detail(slug, detail)   — one row per filer
 *
 * Each helper returns the exact same object shape the old JSON files had, so
 * the rendering code in app.js / recommend.js is unchanged apart from swapping
 * `fetchJSON('…/x.json')` for `DL.getBlob('x')`.
 *
 * Requires lib/supabase.js (getSupabase) to be loaded first.
 */
"use strict";

const DL = (() => {
  /** Fetch a whole-dashboard aggregate blob by key from dashboard_cache. */
  async function getBlob(key) {
    const sb = await getSupabase();
    const { data, error } = await sb
      .from("dashboard_cache")
      .select("data")
      .eq("key", key)
      .single();
    if (error) throw new Error(`Failed to load '${key}': ${error.message}`);
    return data.data;
  }

  /** Fetch a single filer's detail blob by slug from filer_detail. */
  async function getFilerDetail(slug) {
    const sb = await getSupabase();
    const { data, error } = await sb
      .from("filer_detail")
      .select("detail")
      .eq("slug", slug)
      .single();
    if (error) throw new Error(`Failed to load filer '${slug}': ${error.message}`);
    return data.detail;
  }

  return { getBlob, getFilerDetail };
})();
