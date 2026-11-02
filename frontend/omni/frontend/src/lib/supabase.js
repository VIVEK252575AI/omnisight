import { createClient } from "@supabase/supabase-js";

const rawUrl = import.meta.env.VITE_SUPABASE_URL || "";
const key = import.meta.env.VITE_SUPABASE_ANON_KEY || "";

let url = rawUrl.trim();
if (url && !/^https?:\/\//i.test(url)) url = "https://" + url;
url = url.replace(/\/+$/, "");

let supabase = null;
try {
  if (url && key) supabase = createClient(url, key);
} catch (e) {
  console.warn("Supabase client init failed:", e.message);
  supabase = null;
}

export { supabase };

export function isAuthConfigured() {
  return Boolean(url && key);
}
