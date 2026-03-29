/**
 * Supabase client configuration.
 *
 * SUPABASE_URL and SUPABASE_ANON_KEY are public (anon) credentials —
 * safe to include in client-side code. Row-Level Security on the
 * database ensures only authenticated users can read/write protected tables.
 *
 * To configure: replace the placeholders below with your Supabase project values,
 * or set them via Vercel environment variables and a build step.
 */

// These will be replaced by actual values when you create the Supabase project
const SUPABASE_URL  = window.__SUPABASE_URL  || "https://zkyvnprygbfywniisugi.supabase.co";
const SUPABASE_ANON = window.__SUPABASE_ANON || "sb_publishable_8tWa4xOZ6i0NZzkq4hr9xw_Z9mADjQv";

// Load Supabase client from CDN
let _supabaseClient = null;

async function getSupabase() {
  if (_supabaseClient) return _supabaseClient;

  // supabase-js is loaded via script tag in the HTML
  if (typeof supabase !== "undefined" && supabase.createClient) {
    _supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON);
    return _supabaseClient;
  }

  throw new Error("Supabase client not loaded. Ensure the supabase-js script tag is included.");
}

async function getSession() {
  const sb = await getSupabase();
  const { data: { session } } = await sb.auth.getSession();
  return session;
}

async function requireAuth() {
  const session = await getSession();
  if (!session) {
    document.getElementById("app-content").hidden = true;
    document.getElementById("login-form").hidden = false;
    return null;
  }
  document.getElementById("app-content").hidden = false;
  document.getElementById("login-form").hidden = true;
  return session;
}

async function signIn(email, password) {
  const sb = await getSupabase();
  const { data, error } = await sb.auth.signInWithPassword({ email, password });
  if (error) throw error;
  return data;
}

async function signOut() {
  const sb = await getSupabase();
  await sb.auth.signOut();
  window.location.reload();
}

/** Get the current user's role from user_roles table. Returns null if not logged in or no role. */
async function getUserRole() {
  const session = await getSession();
  if (!session) return null;
  try {
    const sb = await getSupabase();
    const { data } = await sb.from("user_roles")
      .select("role")
      .eq("user_id", session.user.id)
      .single();
    return data?.role || null;
  } catch (e) {
    return null; // table may not exist yet
  }
}

/** Check if the current user has a specific role. */
async function hasRole(role) {
  return (await getUserRole()) === role;
}

/** Check if the current user has admin or reviewer access. */
async function isAdminOrReviewer() {
  const role = await getUserRole();
  return role === "admin" || role === "reviewer";
}
