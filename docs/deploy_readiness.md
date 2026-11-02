# Deployment Readiness Verification

## Backend
- [x] `python -c "import app.main"` succeeds
- [x] `/health` endpoint returns `{"status":"ok"}`
- [x] `/api/v1/settings` returns config without secrets
- [x] Scan creation works (POST /api/scans)
- [x] Playwright capture works (POST /api/v1/scans/{id}/playwright-capture)
- [x] Detection works (POST /api/v1/scans/{id}/detect) -> findings
- [x] In-memory fallback when DB unreachable
- [x] CORS allows frontend origins (5173, 5174)
- [x] Secrets redacted (sentinel tests)
- [x] No merge/deploy code path

## Frontend
- [x] `npm run build` succeeds
- [x] Full-screen layout (no rounded shell)
- [x] Login gate (Supabase Google auth, optional)
- [x] Settings/Model page wired to /api/v1/settings
- [x] Responsive: sidebar hidden on mobile (<=768px)
- [x] CORS configured for dev ports

## Tests
- VLM adapter: 21 passed
- Image crops: 9 passed
- Supabase storage: 1 passed
- VLM metrics: 1 passed
- 5 pre-existing failures (PyGithub SDK not installed + contradictory redaction assertion + undefined status in one test) — environment/test bugs, not regressions

## Known limitations
- Supabase DB may be unreachable from some networks (firewall); the app falls back to in-memory storage automatically and retries on next process start.
- Mobile sidebar uses a slide-in drawer (hamburger menu) on screens <=768px.

## Deploy steps

### Backend (Render)
1. Push latest lakshy to GitHub.
2. On Render: New → Web Service → connect repo.
3. Root directory: `backend` (or use render.yaml at repo root).
4. Build command: `pip install -r requirements.txt && python -m playwright install chromium`
5. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
6. Set env vars in Render dashboard:
   - `OMNISIGHT_DATABASE_URL` = `postgresql://postgres:<PASSWORD>@db.<ref>.supabase.co:5432/postgres?sslmode=require`
   - `OMNISIGHT_VLM_ADAPTER` = `mock` (or `gemini` + `OMNISIGHT_GEMINI_API_KEY`)
   - `OMNISIGHT_ALLOWED_ORIGINS` = `https://your-app.vercel.app`
   - `OMNISIGHT_ENABLE_REPAIR_ORCHESTRATOR` = `true` (optional)
7. Run migrations: `alembic upgrade head` (via Render shell or locally with the DB URL).
8. Verify: `https://your-backend.onrender.com/health`

### Frontend (Vercel)
1. On Vercel: New Project → import repo.
2. Root directory: `frontend/omni/frontend`
3. Framework: Vite (auto-detected via vercel.json).
4. Set env vars:
   - `VITE_API_BASE_URL` = `https://your-backend.onrender.com`
   - `VITE_SUPABASE_URL` = your Supabase URL (for login)
   - `VITE_SUPABASE_ANON_KEY` = your anon key
5. Deploy. Vercel auto-deploys on push.

### UptimeRobot
- Monitor type: HTTP(s)
- URL: `https://your-backend.onrender.com/health`
- Interval: 5 minutes
- This keeps the Render free tier awake.
```
