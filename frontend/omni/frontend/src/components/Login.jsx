import { useState, useEffect } from "react";
import { supabase, isAuthConfigured } from "../lib/supabase";

export default function Login({ onAuth }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!supabase) return;
    const { data: sub } = supabase.auth.onAuthStateChange((_event, session) => {
      if (session) onAuth(session);
    });
    supabase.auth.getSession().then(({ data }) => {
      if (data.session) onAuth(data.session);
    });
    return () => sub.subscription.unsubscribe();
  }, [onAuth]);

  const signInGoogle = async () => {
    if (!supabase) {
      setError("Supabase auth not configured. Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY.");
      return;
    }
    setLoading(true);
    setError(null);
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: window.location.origin },
    });
    if (error) {
      setError(error.message);
      setLoading(false);
    }
  };

  if (!isAuthConfigured()) {
    return (
      <div style={{ display: "grid", placeItems: "center", height: "100vh", fontFamily: "sans-serif" }}>
        <div style={{ maxWidth: 420, padding: 24, textAlign: "center" }}>
          <h2>Auth not configured</h2>
          <p>Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY in frontend/.env to enable login.</p>
          <button onClick={() => onAuth(null)}>Continue without login (demo)</button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "grid", placeItems: "center", height: "100vh", fontFamily: "sans-serif", background: "#f7f7f8" }}>
      <div style={{ background: "#fff", padding: 32, borderRadius: 12, boxShadow: "0 4px 16px rgba(0,0,0,0.08)", textAlign: "center", maxWidth: 360 }}>
        <h2 style={{ marginBottom: 8 }}>OmniSight</h2>
        <p style={{ color: "#7e828f", marginBottom: 24 }}>Sign in to continue</p>
        {error && <p style={{ color: "#ef4444", marginBottom: 12 }}>{error}</p>}
        <button onClick={signInGoogle} disabled={loading} style={{ width: "100%", padding: "10px 16px", borderRadius: 8, border: "1px solid #dedee2", background: "#fff", cursor: "pointer", fontWeight: 600 }}>
          {loading ? "Redirecting..." : "Sign in with Google"}
        </button>
      </div>
    </div>
  );
}
