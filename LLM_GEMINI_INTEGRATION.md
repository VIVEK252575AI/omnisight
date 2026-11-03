# OmniSight Gemini LLM Reasoning Integration

## What was connected

The React scan flow now runs Gemini reasoning after Playwright capture and VLM detection. The backend exposes:

`POST /api/v1/scans/{scan_id}/reason`

The endpoint reads validated findings from the existing detection store, sends a bounded evidence package to the existing `GeminiTextAdapter`, validates the JSON response with Pydantic, and rejects finding/evidence references that were not supplied.

## Configure Gemini

In `backend/.env`, set:

```env
OMNISIGHT_GEMINI_API_KEY=your_gemini_api_key
OMNISIGHT_GEMINI_MODEL=gemini-3.6-flash
OMNISIGHT_GEMINI_TIMEOUT_SECONDS=30
OMNISIGHT_GEMINI_MAX_OUTPUT_TOKENS=4096
```

Keep this key in the backend `.env` only. Never add it to `frontend/.env` or commit a real key.

## Run

From the backend folder:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

From the React frontend folder, in another terminal:

```powershell
npm run dev
```

Start a scan in the dashboard. After VLM detection completes, the UI displays the Gemini summary, priorities, related findings, facts, hypotheses, unknowns, repair plan, and verification checks.

## Behaviour and safety

- Gemini receives only validated VLM findings in a size-bounded request.
- Webpage-derived text is treated as untrusted data, not instructions.
- The API validates Gemini's response before returning it.
- A Gemini failure does not discard successful Playwright/VLM results.
- The reasoning result is review-only. It does not edit files, apply patches, execute commands, or trigger verification.
- The endpoint currently returns the result to the requesting frontend; durable reasoning-result persistence is a follow-up task.

## API check

After a completed VLM scan, call:

```http
POST /api/v1/scans/{scan_id}/reason
```

The scan ID is the UUID returned by `POST /api/scans`.
