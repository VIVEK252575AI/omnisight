👁️ OmniSight
Multimodal UI Self-Healing & RPA Agent
OmniSight is an AI-assisted web UI quality-inspection project. It combines browser automation, visual analysis, and language-model reasoning to help identify and understand interface defects.

The integrated scan workflow uses React + Vite for the dashboard, FastAPI for backend APIs, Playwright for browser capture, a Vision-Language Model (VLM) for visual defect detection, and Gemini for reasoning over validated findings.
Current integrated workflow: Capture → Detect → Reason
Project direction: Detect → Reason → Fix → Verify → Deliver

Features
- Automated browser capture: Playwright opens the target website and captures configured viewports.
- Evidence collection: Screenshots and bounded browser/DOM observations are registered by the backend.
- Visual defect detection: The configured VLM adapter analyses screenshot evidence and returns structured findings.
- LLM reasoning: Gemini analyses validated VLM findings and produces a structured explanation and recommendations.
- Finding prioritisation: Issues are grouped and prioritised by impact, severity, confidence, and evidence quality.
- Root-cause hypotheses: Gemini separates observed facts, possible causes, and unknown information.
- Repair guidance: The dashboard displays recommended investigation/repair steps and suggested verification checks.
- Graceful failure handling: If Gemini reasoning is unavailable, successful Playwright capture and VLM findings remain available in the dashboard.
- Review-only operation: The reasoning stage does not edit source files, execute commands, apply patches, or trigger verification.
Architecture and workflow
React + Vite Dashboard
        │
        │ POST /api/scans
        ▼
FastAPI Scan API
        │
        │ POST /api/v1/scans/{scan_id}/playwright-capture
        ▼
Playwright / Chromium
        ├── Opens the target website
        ├── Captures configured viewports
        ├── Collects bounded DOM geometry and browser diagnostics
        └── Saves screenshot evidence
        │
        ▼
Backend Evidence Store
        │
        │ POST /api/v1/scans/{scan_id}/detect
        ▼
VLM Adapter (MOCK or GEMINI)
        ├── Analyses screenshot evidence
        └── Returns validated visual findings
        │
        │ GET /api/v1/scans/{scan_id}/findings
        ▼
Gemini LLM Reasoning API
        │ POST /api/v1/scans/{scan_id}/reason
        ├── Summarises the scan
        ├── Prioritises findings
        ├── Groups related findings
        ├── Identifies facts, hypotheses, and unknowns
        ├── Suggests investigation/repair steps
        └── Recommends verification checks
        │
        ▼
React Dashboard
   Screenshots + Findings + Gemini Reasoning
Gemini reasoning response
The reasoning API returns a structured response containing:
- summary — overall explanation of the scan findings.
- priorities — priority level, score, rationale, and associated finding IDs.
- related_findings — findings that may describe the same or related issue.
- observed_facts — information directly supported by the supplied findings.
- root_cause_hypotheses — possible causes, confidence, evidence references, and whether source inspection is required.
- unknowns — information that cannot be established from the available evidence.
- repair_plan — recommended investigation or repair steps, not executable patches.
- verification_checks — suggested checks to perform after a future fix.
The backend validates Gemini's JSON response with Pydantic and checks that referenced finding IDs and evidence references belong to the current scan. Website-derived text is treated as untrusted input, not as instructions to the model.
Repository structure
omnisight-main/
├── backend/
│   ├── app/
│   │   ├── ai/adapters/             # VLM and Gemini provider adapters
│   │   ├── routers/
│   │   │   ├── detection.py         # VLM detection and findings APIs
│   │   │   ├── playwright_captures.py # Browser capture API
│   │   │   └── reasoning.py         # Gemini LLM reasoning API
│   │   ├── reasoning_schemas.py     # Pydantic reasoning response models
│   │   └── main.py                  # FastAPI application and router setup
│   ├── artifacts/playwright/        # Runtime browser screenshots (gitignored)
│   ├── tests/
│   │   └── test_reasoning_schemas.py
│   ├── .env.example
│   └── requirements.txt
├── frontend/
│   └── omni/frontend/
│       └── src/
│           ├── services/api.js      # Frontend API and scan workflow
│           └── App.jsx              # Dashboard and reasoning panel
├── LLM_GEMINI_INTEGRATION.md
└── README.md
Requirements
- Windows, macOS, or Linux
- Python 3.13 recommended for the supplied Windows setup
- Node.js and npm
- Chromium installed through Playwright
- PostgreSQL configured for repository workflows that require persistent storage
- A Gemini API key for live Gemini VLM analysis and LLM reasoning
1. Backend setup
Open PowerShell at the repository root:
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
If PowerShell blocks virtual-environment activation, run the venv executables directly:
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Configure environment variables
Open backend/.env. Configure the Gemini key and model on the backend only:
OMNISIGHT_GEMINI_API_KEY=YOUR_GEMINI_API_KEY
OMNISIGHT_GEMINI_MODEL=gemini-3.6-flash
OMNISIGHT_GEMINI_TIMEOUT_SECONDS=30
OMNISIGHT_GEMINI_MAX_OUTPUT_TOKENS=4096
To use Gemini for visual detection as well, set:
OMNISIGHT_VLM_ADAPTER=gemini
For an offline/demo detection run, keep or set:
OMNISIGHT_VLM_ADAPTER=mock
With the mock VLM adapter, the detection findings are deterministic sample results. The LLM reasoning stage still requires a configured Gemini key to generate live reasoning.

If the project is configured to use PostgreSQL, set OMNISIGHT_DATABASE_URL in backend/.env to your local development database and apply migrations from the backend/ directory:
alembic upgrade head
Do not use a production database for local experiments.
Start the backend
From backend/, on Windows use the repository's development runner:
python run_dev.py
Alternatively, if your environment supports the standard Uvicorn development command:
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
Backend health endpoint: http://127.0.0.1:8000/health
Interactive API documentation: http://127.0.0.1:8000/docs
2. Frontend setup
Open a second terminal from the repository root:
cd frontend\omni\frontend
Copy-Item env.example .env
npm install
npm run dev
Set the frontend API base URL in frontend/omni/frontend/.env:
VITE_API_BASE_URL=http://127.0.0.1:8000
Open the Vite URL printed in the terminal (normally http://127.0.0.1:5173).

If npm install fails with ECONNRESET, retry on a stable network and check that the npm registry is set to https://registry.npmjs.org/.
3. Run an end-to-end scan
1. Start the backend and frontend in separate terminals.
2. Open the Vite frontend URL.
3. Enter a website URL that is reachable from the backend machine.
4. Choose the scan depth/viewports in the dashboard.
5. Start the scan.
6. OmniSight asks Playwright to capture the website and collect evidence.
7. The backend runs VLM detection and stores validated findings.
8. The frontend requests Gemini reasoning for the completed detection results.
9. Review the screenshots, findings, priority scores, related findings, facts, hypotheses, unknowns, repair recommendations, and verification checks in the dashboard.
Only scan websites that you own or are authorised to test. Browser automation runs from the backend host and may access network resources available to that machine. Do not expose the development backend to untrusted networks.
4. API endpoints
Method	Endpoint	Purpose
POST	/api/scans	Create a scan and return its scan_id.
POST	/api/v1/scans/{scan_id}/playwright-capture	Run Playwright capture and register browser evidence.
GET	/api/v1/scans/{scan_id}/evidence	Retrieve stored evidence metadata.
GET	/api/v1/artifacts/{artifact_id}	Retrieve a stored screenshot artifact.
POST	/api/v1/scans/{scan_id}/detect	Run the configured VLM detection adapter.
GET	/api/v1/scans/{scan_id}/findings	Retrieve detection status and validated findings.
POST	/api/v1/scans/{scan_id}/reason	Run Gemini reasoning over validated findings.


The frontend scan service calls the reasoning endpoint after VLM detection. The reasoning endpoint can also be called directly after a scan has validated findings. It returns 409 if no validated findings are available.
5. Testing
Run backend tests from backend/:
.\.venv\Scripts\python.exe -m pytest tests\ -q
Run the focused reasoning schema tests:
.\.venv\Scripts\python.exe -m pytest tests\test_reasoning_schemas.py -q
The reasoning schema tests do not require a live Gemini API call. Live end-to-end reasoning requires a valid backend Gemini key and available provider quota.
6. Troubleshooting
- Playwright reports that Chromium is missing: run python -m playwright install chromium from backend/.
- Capture fails or returns a gateway error: confirm the target URL is reachable from the backend machine and inspect backend logs.
- No screenshot/evidence is available: check GET /api/v1/scans/{scan_id}/evidence and confirm the capture response reports evidence.
- Findings are labelled MOCK: set OMNISIGHT_VLM_ADAPTER=gemini if you want live visual analysis, and configure the Gemini key in backend/.env.
- Gemini reasoning is unavailable: confirm OMNISIGHT_GEMINI_API_KEY, model access, network connectivity, timeout, and provider quota. Restart the backend after changing .env.
- Reasoning returns 409: run VLM detection successfully first; the reasoning endpoint requires validated findings.
- Frontend shows “Failed to fetch”: confirm the backend is running on port 8000 and VITE_API_BASE_URL points to it.
Never place the Gemini API key in frontend code, a VITE_* variable, screenshots, or Git commits. Keep the real backend/.env out of source control.
7. Current scope and limitations
The integrated milestone completes the Playwright capture → VLM detection → Gemini reasoning workflow. Gemini's output is advisory and review-only.

The following are separate project stages and are not triggered by the LLM reasoning endpoint:
- Generating executable source-code patches
- Applying patches to a project
- Running sandbox validation
- Re-capturing the UI to verify a fix
- Creating GitHub branches, commits, or pull requests
- Running CI/CD delivery workflows
Reasoning results are returned to the frontend for the current request. Durable persistence of the reasoning response is a follow-up task. A Gemini failure does not erase the successful capture or VLM findings.
Additional documentation
- [`LLM_GEMINI_INTEGRATION.md`](LLM_GEMINI_INTEGRATION.md) — Gemini reasoning integration details and API usage.
- [`OmniSight_LLM_Reasoning_Engine_Workflow.md`](OmniSight_LLM_Reasoning_Engine_Workflow.md) — LLM reasoning workflow specification.
