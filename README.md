# 👁️ OmniSight

# OmniSight — Multimodal UI Self-Healing & RPA Agent

OmniSight inspects a website with Playwright, stores browser evidence in the backend evidence store, and analyzes those screenshots with a configured Vision-Language Model (VLM). The React dashboard displays the captured screenshot and structured findings.

## Current integrated flow

```text
React + Vite
   │  POST /api/scans
   ▼
FastAPI creates a scan (created)
   │  POST /api/v1/scans/{scan_id}/playwright-capture
   ▼
Playwright / Chromium
   ├─ Opens the registered target URL
   ├─ Captures configured viewports
   ├─ Collects bounded DOM geometry and browser diagnostics
   └─ Saves screenshots beneath backend/artifacts/playwright
   ▼
EvidenceStore
   ├─ Registers screenshot artifact IDs
   ├─ Persists observation/v1 metadata
   └─ Advances scan to evidence_collection
   │  POST /api/v1/scans/{scan_id}/detect
   ▼
VLM adapter (MOCK or GEMINI)
   ├─ Resolves artifact IDs to private screenshot files
   ├─ Analyzes screenshots
   └─ Persists validated findings and adapter provenance
   │  GET /api/v1/scans/{scan_id}/findings
   ▼
React dashboard
   ├─ Shows the Playwright screenshot
   ├─ Shows findings and severity/confidence
   └─ Labels MOCK results so they are not mistaken for real model inference
```

The current frontend integration covers **Playwright capture + VLM detection**. The Reason/Fix/Validate/Verify stages remain separate workflows and are not marked complete by this scan action.

## Project paths

```text
frontend/omni/frontend/        React + Vite application
backend/app/main.py            FastAPI application and router registration
backend/app/routers/playwright_captures.py
                               Playwright capture + evidence persistence
backend/app/routers/detection.py
                               VLM trigger and findings APIs
backend/app/evidence_storage.py
                               Private screenshot/evidence storage
backend/app/ai/adapters/       MOCK and Gemini VLM adapters
backend/artifacts/playwright/  Runtime screenshots (gitignored)
```

## 1. Backend setup (Windows PowerShell)

From the repository root:

```powershell
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

If PowerShell blocks activation, run the venv executables directly instead:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Open `backend/.env` and configure the VLM:

- For a quick offline smoke test, leave `OMNISIGHT_VLM_ADAPTER=mock`. Findings will be deterministic sample output and labelled **MOCK**.
- For real screenshot analysis, set `OMNISIGHT_VLM_ADAPTER=gemini` and add your key to `OMNISIGHT_GEMINI_API_KEY`. Keep the key only in `backend/.env`; never put it in a `VITE_*` frontend variable or commit it.

Example real-provider settings:

```dotenv
OMNISIGHT_VLM_ADAPTER=gemini
OMNISIGHT_GEMINI_API_KEY=YOUR_GEMINI_API_KEY
OMNISIGHT_GEMINI_MODEL=gemini-3.6-flash
OMNISIGHT_VLM_TIMEOUT_SECONDS=60
```

The repository's configured PostgreSQL database is the persistent source of truth. Set `OMNISIGHT_DATABASE_URL` to a reachable local PostgreSQL database and apply the repository migrations from `backend/` with `alembic upgrade head` before running persistent workflows. Do not use a production database for local experiments.

Start FastAPI from `backend/`:

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --reload --host 127.0.0.1 --port 8000
```

Check `http://127.0.0.1:8000/health` and API documentation at `http://127.0.0.1:8000/docs`.

## 2. Frontend setup

Open a second terminal from the repository root:

```powershell
cd frontend\omni\frontend
Copy-Item env.example .env
npm install
npm run dev
```

Open the Vite URL shown in the terminal (normally `http://127.0.0.1:5173`). The frontend `.env` should contain:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

If `npm install` fails with `ECONNRESET`, retry on a stable network and check `npm config get registry`; the expected registry is `https://registry.npmjs.org/`.

## 3. Run a scan

1. Start the backend and frontend.
2. Enter a reachable website URL in the OmniSight scan composer.
3. Select Deep Scan to capture mobile, tablet, and desktop viewports, or turn it off for a mobile capture.
4. Start the scan.
5. The backend captures screenshots, stores them as evidence, and triggers VLM detection.
6. The dashboard displays the first captured screenshot, findings, and the adapter label.

The target must be reachable from the machine running the backend. Only scan websites you own or are authorized to test. Do not expose this development backend to untrusted networks: browser automation can access network resources from the backend host.

## 4. API sequence

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/scans` | Create scan and return `scan_id` |
| `POST` | `/api/v1/scans/{scan_id}/playwright-capture` | Capture viewports and persist `observation/v1` evidence |
| `GET` | `/api/v1/scans/{scan_id}/evidence` | Inspect stored evidence metadata |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Retrieve a stored screenshot |
| `POST` | `/api/v1/scans/{scan_id}/detect` | Trigger configured VLM adapter |
| `GET` | `/api/v1/scans/{scan_id}/findings` | Poll detection status and validated findings |

## 5. Troubleshooting

- **Playwright says Chromium is missing:** from `backend/`, run `python -m playwright install chromium`.
- **Capture returns 502:** confirm the URL opens from the backend machine and check backend logs.
- **Capture returns 413:** the page screenshot exceeded `OMNISIGHT_ARTIFACT_MAX_IMAGE_BYTES`; raise the limit carefully or scan a smaller page.
- **VLM says no screenshot/evidence:** check `GET /api/v1/scans/{scan_id}/evidence`; the capture endpoint should return `evidence_count` greater than zero.
- **Results show MOCK:** set `OMNISIGHT_VLM_ADAPTER=gemini` and configure `OMNISIGHT_GEMINI_API_KEY` in the backend `.env`, then restart FastAPI.
- **Gemini authentication/configuration error:** verify the backend key and model access. Never paste the key into the frontend or a chat screenshot.
- **Frontend cannot reach backend:** check `VITE_API_BASE_URL`, backend CORS origins, and that both servers are running.

## 6. Backend tests

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

## 3. Run and test end-to-end

Backend (PowerShell, from `backend/`):

`powershell
.\.venv\Scripts\python.exe run_dev.py
`

Frontend (another terminal, from `frontend/omni/frontend/`):

`powershell
npm run dev
`

Open the printed Vite URL (http://127.0.0.1:5173 or 5174). Paste any URL and click **Start AI Scan**.

If the UI shows "Failed to fetch", the backend is not running on port 8000 — start `run_dev.py` first.

## Notes

- Windows: use `run_dev.py` (proactor event loop) instead of `uvicorn --reload` so Playwright capture works.
- Supabase: set `OMNISIGHT_DATABASE_URL` in `backend/.env` with `?sslmode=require`, then run `alembic upgrade head`.
- The VLM adapter defaults to MOCK; findings are labelled MOCK. Set `OMNISIGHT_VLM_ADAPTER=gemini` plus `OMNISIGHT_GEMINI_API_KEY` for real inference.
