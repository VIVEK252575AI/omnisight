# 👁️ OmniSight

## Multimodal UI Self-Healing & RPA Agent

OmniSight is an **AI-powered autonomous QA workflow** designed to detect visual UI defects, reason about their causes, generate targeted fixes, verify those fixes, and deliver reviewable changes through GitHub.

> **Core Loop:** Detect → Fix → Verify → Deliver

### Client demo

Open the [OmniSight client demo](client-demo/index.html) for a concise
walkthrough of the live Gemini + ShopLite sandbox result. To rerun that flow
locally, use the [client demo instructions](client-demo/README.md); Docker
Desktop and local Gemini / sandbox settings are required.

Traditional UI tests mainly inspect the DOM and depend on hardcoded selectors. However, users experience an application visually through pixels. A test may therefore pass functionally while problems such as clipping, overlap, poor contrast, and broken responsive layouts remain visible.

OmniSight combines **browser automation, visual AI, code reasoning, automated validation, and CI/CD** to create a self-healing UI testing workflow.

---

## 📌 The Problem

Traditional UI testing is not always enough.

Common problems include:

- Hardcoded DOM selectors can break when UI structure changes.
- Functional tests may pass while visual defects remain.
- Elements may exist in the DOM but still be outside the visible viewport.
- Responsive layouts can break on smaller screen sizes.
- Buttons can become clipped.
- UI elements can overlap.
- Text may have poor contrast.
- Manual visual testing across multiple devices takes time.

OmniSight addresses these problems by combining **DOM evidence with visual evidence**.

---

## 🎯 Project Objective

The goal of OmniSight is to create an automated QA system capable of:

1. Inspecting a running web application.
2. Collecting screenshots and DOM evidence.
3. Detecting visual UI anomalies using a Vision-Language Model.
4. Generating a minimal source-code patch.
5. Applying the patch safely in an isolated environment.
6. Re-testing the modified application.
7. Verifying that the original visual defect has disappeared.
8. Creating a reviewable GitHub Pull Request.
9. Running CI/CD checks.
10. Keeping a human approval step before merge.

---

## 🔄 Complete OmniSight Pipeline

```text
Target URL
    │
    ▼
Playwright
    │
    ▼
Screenshot + DOM
    │
    ▼
VLM
    │
    ▼
Bug Detection
    │
    ▼
LLM / Fix Engine
    │
    ▼
Minimal Code Patch
    │
    ▼
Validation Sandbox
    │
    ▼
Self-Healing Verification
    │
    ▼
GitHub Branch + Commit + PR
    │
    ▼
CI/CD
    │
    ▼
Human Review
```

The major stages are:

**Input → Collect Evidence → Detect → Reason → Fix → Verify → Deliver**

---

## 🧪 Backend Implementation Status

> This section reflects the **actual repository state**. The rest of this
> document describes the target design.

The Week 1 and Week 2 backend (FastAPI) is implemented and tested (`backend/`):

| Area | Status |
|---|---|
| Scan API + lifecycle state machine | Tested against the local application |
| Simulated CI/CD webhook receiver (optional HMAC) | Tested against the local application |
| Evidence API (`observation/v1`) + safe artifact serving | Tested against the local application |
| Provider-neutral VLM adapter boundary (W2.2) | Tested with mocks |
| Structured visual-finding schema + strict parser (W2.3) | Tested with mocks |
| VLM detection workflow (W2.4) | Tested with mocks |
| Safe action engine / patch proposals (W2.5) | Tested with mocks |
| Gemini visual analyzer and safe patch generator | Implemented; provider calls are covered by mocked tests. Live API verification requires a configured key and available quota |
| Code-response extraction & rejection (W2.6) | Tested with mocks |
| Week 2 test matrix: valid / malformed / missing / unsupported / failed (W2.7) | Tested with mocks |
| Local Playwright checkout capture and scan runner | Tested with the local demo and backend |
| ShopLite-only Docker patch verification | Implemented behind explicit local approval; live sandbox smoke is configured in GitHub Actions |
| GitHub App authentication and ShopLite draft PR publisher | Local loopback-only owner flow with PostgreSQL-backed project, validation, idempotent operation, event, and draft PR records. Requires explicit confirmation and remains disabled by default ([setup and workflow](docs/week4_github_app_connection.md)) |
| Local interactive dashboard | Main scan, persisted before/after screenshots, run history, findings, patch review, sandbox approval, and optional GitHub publishing use the fixed ShopLite backend workflow |
| General repository patching, PR merge/deploy, production dashboard | Not implemented; publishing is limited to the fixed ShopLite CSS demo and draft PRs |

VLM detection and patch generation default to deterministic offline **MOCK**
adapters. The dashboard exposes the fixed ShopLite workflow and uses its
server-configured providers for browser capture → finding → safe proposal.
Raw model output is validated and never executed or applied automatically.
Arbitrary URL scanning is not exposed as a working dashboard feature yet.

Endpoints currently served:

```text
GET      /health
GET      /api/scans
POST     /api/scans
GET      /api/scans/{scan_id}
POST     /api/scans/{scan_id}/transition
POST     /api/v1/webhooks/ci              # SIMULATED CI only
GET      /api/v1/webhooks/events
GET      /api/v1/webhooks/events/{event_id}
POST     /api/v1/scans/{scan_id}/evidence
GET      /api/v1/scans/{scan_id}/evidence
GET      /api/v1/artifacts/{artifact_id}
POST     /api/v1/scans/{scan_id}/detect           # VLM detection (MOCK or Gemini)
GET      /api/v1/scans/{scan_id}/findings         # validated findings + run
POST     /api/v1/demo/shoplite/runs               # local fixed-target real scan
GET      /api/v1/demo/shoplite/runs               # list current local demo runs
GET      /api/v1/demo/shoplite/runs/{run_id}      # poll run and proposal status
GET      /api/v1/demo/shoplite/runs/{run_id}/patch-preview # exact source diff + base hash
POST     /api/v1/demo/shoplite/runs/{run_id}/validate # explicit confirmation + Docker sandbox
POST     /api/v1/demo/shoplite/runs/{run_id}/reject-proposal # record human rejection + reason
GET      /api/v1/findings/{finding_id}
POST     /api/v1/findings/{finding_id}/patch-proposals   # propose only, no writes
GET      /api/v1/findings/{finding_id}/patch-proposals
POST     /api/v1/demo/shoplite/patch-proposals/{proposal_id}/validate  # approved, fixed-target Docker check
POST     /api/v1/local/github/connection/verify                        # local owner; server-configured repo only
GET      /api/v1/local/github/state                                    # restore connection, validation, and PR operations
POST     /api/v1/local/shoplite/proposals/{proposal_id}/validate        # remote base SHA + Docker report
POST     /api/v1/local/github/draft-prs                                 # explicit confirmation + idempotency key
GET      /api/v1/local/github/operations/{operation_id}
```

Run the application (from `backend/`) and open the dashboard at
`http://127.0.0.1:8000/`:

```powershell
$env:OMNISIGHT_DATABASE_URL = "postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/omnisight"
.\.venv\Scripts\alembic.exe upgrade head             # create/update PostgreSQL schema
.\.venv\Scripts\uvicorn.exe app.main:app --reload     # serve
.\.venv\Scripts\python.exe -m pytest tests\ -q        # test
```

Use **Live ShopLite Demo** from the dashboard for the backend workflow. It uses
the configured provider (MOCK by default; Gemini when explicitly configured).
Review the exact source diff, then either reject the proposal with a recorded
reason or type `approve-demo-only` and confirm before the API starts the
restricted Docker sandbox. A rejected proposal cannot be sandbox-validated or
published. This UI and its API only accept the fixed local
ShopLite target; arbitrary URL scans are not exposed as supported UI actions.
History and Recent Scans show real backend ShopLite runs. Run records, step
summaries, and validated patch proposals are stored in PostgreSQL and survive
backend restarts. Apply the Alembic migrations before starting the workflow.

After a successful sandbox run, the same page can verify the server-configured
GitHub App repository, validate the proposal again against the current remote
base in Docker, and create a draft pull request after you type
`create-draft-pr`. This publishing path is disabled by default and requires
PostgreSQL plus the local owner and GitHub App configuration in
[`docs/week4_github_app_connection.md`](docs/week4_github_app_connection.md).
After a browser refresh, enter the local owner token and select **Load saved
GitHub publishing state** to recover the verified connection, recent validation
report, draft PR outcome, and idempotency key for safe retry/reconciliation.

Test suite: **341 passed, 1 skipped** (offline; the skip is a Windows
`symlink` test).

Design docs live under `docs/`:

- `docs/week1_integration_audit.md`
- `docs/week2_finding_schema.md`
- `docs/week2_detection_api.md`
- `docs/week2_action_engine.md`
- `docs/week2_code_extraction.md`
- `docs/week2_testing_note.md`
- `docs/week3_shoplite_sandbox.md`

The generic CI webhook endpoint accepts synthetic events. Separately, the
GitHub Actions workflow runs backend tests, the capture-to-sandbox pipeline,
and the real restricted Docker smoke test on pushes to `vivek`; it does not
send workflow events to a running OmniSight server. Patch proposals are never
applied to the source repository. The opt-in ShopLite endpoint copies one
reviewed CSS proposal into a temporary staging tree and verifies it in a
restricted Docker container; see
[`docs/week3_shoplite_sandbox.md`](docs/week3_shoplite_sandbox.md). It is a
local demo milestone, not a general repository sandbox or production security
boundary.

---

## 🏗️ System Architecture

OmniSight follows a modular architecture with stable JSON/API contracts between components.

```text
                         ┌──────────────────────┐
                         │   React Dashboard    │
                         │    Human Control     │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ FastAPI Orchestrator │
                         │   Workflow + State   │
                         └──────────┬───────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          │                         │                         │
          ▼                         ▼                         ▼
┌──────────────────┐     ┌──────────────────┐      ┌──────────────────┐
│ Playwright Worker│     │   VLM Service    │      │ LLM/Patch Engine │
│ Browser/Evidence │     │ Visual Detection │      │ Code Reasoning   │
└────────┬─────────┘     └─────────┬────────┘      └─────────┬────────┘
         │                         │                         │
         └─────────────────────────┼─────────────────────────┘
                                   │
                                   ▼
                         ┌──────────────────────┐
                         │ Validation Sandbox   │
                         │ Build + Test + Safety│
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │    GitHub + CI/CD    │
                         │ Branch · Commit · PR │
                         └──────────────────────┘

                         ┌──────────────────────┐
                         │       Storage        │
                         │ Artifacts · DB · Logs│
                         └──────────────────────┘
```

The architecture is designed to be **provider-agnostic**, allowing AI models to be replaced without changing the complete workflow.

---

## 🧰 Technology Stack

### Frontend

- React
- JavaScript
- HTML5
- CSS3
- Vite

### Backend / Orchestration

- Python
- FastAPI
- REST APIs
- JSON-based communication

### Browser Automation / RPA

- Playwright

### AI

**Vision-Language Model (VLM)** is used for screenshot understanding, visual defect detection, spatial reasoning, and verification of visual fixes.

**Large Language Model (LLM)** is used for reasoning about detected bugs, mapping defects to source code, generating minimal patches, and suggesting targeted fixes.

### Storage

Used for:

- Scan information
- Screenshots
- DOM snapshots
- Bug reports
- Logs
- Validation results

### DevOps

- Git
- GitHub
- GitHub Pull Requests
- CI/CD pipelines

---

## 📁 Suggested Project Structure

```text
OmniSight/
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── Sidebar.jsx
│   │   │   ├── ScanForm.jsx
│   │   │   ├── Findings.jsx
│   │   │   ├── FindingCard.jsx
│   │   │   ├── BeforeAfter.jsx
│   │   │   ├── PullRequests.jsx
│   │   │   ├── Logs.jsx
│   │   │   └── ApprovalControls.jsx
│   │   ├── services/
│   │   │   └── api.js
│   │   ├── App.jsx
│   │   ├── main.jsx
│   │   └── styles.css
│   ├── package.json
│   └── vite.config.js
│
├── backend/
│   ├── app/
│   │   ├── main.py                 # app factory + router wiring
│   │   ├── config.py               # env-driven Settings
│   │   ├── schemas.py              # scan + observation/v1 contracts
│   │   ├── errors.py               # single error envelope
│   │   ├── storage.py              # PostgreSQL QA run repository
│   │   ├── event_storage.py        # CI event store
│   │   ├── evidence_storage.py     # PostgreSQL evidence metadata + private file refs
│   │   ├── artifacts.py            # safe artifact resolution
│   │   ├── detection_*.py          # VLM detection + persistent finding repository
│   │   ├── action_engine.py        # safe patch-proposal engine (W2.5)
│   │   ├── patch_*.py              # schemas, paths, validation, extraction
│   │   ├── source_context.py       # read-only approved-source context
│   │   ├── ai/                     # adapter boundary, gateway, parser, prompts
│   │   └── routers/                # scans, evidence, detection, patches, ci
│   ├── tests/                      # pytest suite + FIXTURE corpus
│   └── requirements.txt
│
├── playwright_worker/
│   ├── scanner.py
│   ├── collector.py
│   └── viewports.py
│
├── vlm_service/
│   ├── detector.py
│   └── prompts.py
│
├── patch_engine/
│   ├── generator.py
│   └── source_locator.py
│
├── validation/
│   ├── sandbox.py
│   ├── verifier.py
│   └── rollback.py
│
├── artifacts/
│   ├── screenshots/
│   ├── observations/
│   └── logs/
│
├── .github/
│   └── workflows/
│
├── README.md
└── .gitignore
```

> The exact folder structure can be adjusted during implementation while keeping the same modular architecture. In the current repository only `backend/` and `docs/` exist; the `frontend/`, `playwright_worker/`, `vlm_service/`, `patch_engine/` and `validation/` directories are **target modules** — their Week 1–2 backend logic currently lives under `backend/app/` (see `ai/`, `detection_*`, `patch_*`).

---

## 🖥️ Target Application — ShopLite

The first proof of concept uses a controlled React e-commerce application called **ShopLite**.

Starting with a controlled application allows the project to use known defects and verify that OmniSight can reliably detect and repair them before testing arbitrary staging websites.

### Deliberately Injected Defects

- **Clipped CTA:** Checkout button cut off on mobile.
- **Overlap:** Elements stack over one another.
- **Low Contrast:** Text fails readability requirements.
- **Responsive Break:** Layout collapses at narrow widths.

After the proof of concept, the same pipeline can accept a staging URL supplied through the dashboard.

---

## 🤖 Playwright Browser Automation

Playwright is the first technical module in the OmniSight workflow. It converts a running webpage into structured evidence through five stages:

1. **Navigate** — Open the supplied URL and relevant application states.
2. **Interact** — Click and type to reach the required UI state.
3. **Capture** — Take screenshots at controlled viewport sizes.
4. **Inspect** — Extract DOM/HTML, metadata, console errors, and network evidence.
5. **Package** — Store observations as structured JSON for the AI pipeline.

Example fixed viewports:

```text
Mobile:  390 × 844
Desktop: 1440 × 900
```

Fixed viewports make before-and-after comparisons repeatable.

### Browser Artifacts

```text
screenshots/*.png
dom_snapshot.json
console_errors.log
network_trace.json
observation.json
```

---

## 📄 Observation JSON

Browser evidence is converted into machine-readable context.

```json
{
  "schema_version": "observation/v1",
  "url": "https://shoplite.local/checkout",
  "route": "/checkout",
  "state": "cart-filled",
  "viewport": {
    "width": 390,
    "height": 844
  },
  "screenshot": "screenshots/checkout-390.png",
  "elements": [
    {
      "selector": "#checkout",
      "tag": "button",
      "text": "Checkout",
      "visible": true,
      "in_viewport": false,
      "box": {
        "x": 420,
        "y": 690,
        "w": 120,
        "h": 44
      }
    }
  ],
  "console_errors": [],
  "network_errors": [],
  "captured_at": "2026-01-01T00:00:00Z"
}
```

In this example, the button exists in the DOM but lies outside the 390 px mobile viewport. This exact shape is exercised by `backend/tests/fixtures/observation_fixture.json`.

> **Evidence is not the bug report.** Playwright collects evidence and context. The VLM interprets that evidence and returns a structured finding.

---

## 👁️ VLM — Visual Bug Detection

The Vision-Language Model receives:

```text
Screenshot
    +
Relevant DOM / HTML
    +
Viewport Metadata
    +
Console / Network Information
```

It returns a validated response (not a bare finding):

```json
{
  "schema_version": 1,
  "outcome": "defects_found",
  "summary": "Checkout button is clipped at 390px width.",
  "findings": [
    {
      "finding_id": "<backend-assigned>",
      "category": "clipped_element",
      "target_candidate": "#checkout",
      "target_unverified": false,
      "observation": "The Checkout button extends past the right viewport edge.",
      "evidence": "geometry: button.x=420 width=120 viewport.width=390",
      "potential_causes": ["fixed pixel width"],
      "severity": "high",
      "confidence": 0.94,
      "status": "detected"
    }
  ]
}
```

`outcome` is one of `defects_found`, `no_defects`, or `needs_inspection`. The
`category` is drawn from a closed set: `clipped_element`,
`overlapping_elements`, `low_contrast_text`, `responsive_layout_failure`,
`hidden_or_unreachable_element`, `needs_inspection`.

`finding_id`, `status` and `target_unverified` are **assigned by the backend** —
a model-supplied value is ignored. This lets OmniSight combine visual
understanding with browser evidence instead of relying only on DOM state.

### VLM Responsibilities in the Pipeline

- **Detect** visual defects: clipped elements, overlapping UI, low-contrast text, and broken responsive layouts.
- **Explain** why something is a defect, in plain language a developer can act on.
- **Localize** the problem to a specific target (e.g. `#checkout`) so downstream stages know what to fix.
- **Verify** after a patch is applied: re-examine fresh screenshots and confirm the **original** defect is actually gone — build success alone is not proof.
- **Score** severity and confidence so orchestrator routing, retry budgets, and human-review priorities stay deterministic.

### How the VLM Combines Evidence

The VLM never works from the screenshot alone. Every detection request includes:

| Input | What it contributes |
|---|---|
| Screenshot (at fixed viewports: 390×844, 1440×900) | Ground truth pixels — what users actually see |
| DOM / element geometry (`box`, `in_viewport`, `visible`) | Mechanical measurements the model can cite as evidence |
| Console / network errors | Function context — a broken render or failed request frequently produces a visual defect |
| Viewport metadata | Frame of reference for clipping and responsive judgments |

Cross-checking structured evidence against visual perception is what makes findings reliable enough to trigger a patch.

### Finding Contract Guarantees

- Raw model output is **always validated against a strict JSON schema** before any downstream stage consumes it.
- Invalid, truncated, or contradictory responses become a recorded failure. A repairable shape failure may trigger **one bounded repair re-ask**; values are never fabricated or guessed into a finding.
- Each finding carries `category`, `target_candidate`, `observation`, `evidence`, `potential_causes`, `severity`, and `confidence`, plus the backend-assigned `finding_id`, `target_unverified`, and `status`.
- `severity` and `confidence` are read by the orchestrator to gate whether a check proceeds to patch generation.

### Why a VLM Instead of DOM-Only Checks

- An element can **exist in the DOM** yet be invisible or outside the viewport — DOM checks report it as healthy.
- Functional tests pass while a button is clipped, text is unreadable, or a layout has collapsed.
- Hardcoded DOM selectors break the moment UI structure changes; pixels don't lie about what users experience.
- Responsive regressions only show up when the page is actually **rendered** at a narrow viewport.

The VLM's job is precisely the part traditional DOM-based testing cannot see: what the rendered page _looks like_.

---

## 🧠 LLM / Fix Engine

After a bug is detected, the LLM/Fix Engine attempts to produce the **smallest possible source-code change** required to resolve it.

The engine receives:

```text
Bug Report + DOM Context + CSS / React Source
```

and produces a minimal patch.

Example:

```diff
.checkout-actions {
-    width: 540px;
+    width: 100%;
+    max-width: 100%;
+    box-sizing: border-box;
}
```

The goal is not to rewrite large parts of the application. The goal is to make the smallest targeted fix possible.

### Patch Guardrails

- Approved files only
- Minimal patch
- Syntax validation
- Build and tests
- Human approval

---

## 🔒 Safe Patching Workflow

Every automated change happens in isolation and remains reversible.

```text
Original Baseline
       │
       ▼
Isolated Branch / Workspace
       │
       ▼
Apply Minimal Patch
       │
       ▼
Build + Tests
       │
       ▼
   Checks Pass?
     /      \
   FAIL     PASS
    │         │
    ▼         ▼
Rollback   Re-Screenshot
    │
    └── Bounded Retry
```

Rules include:

- Never modify `main` directly.
- Modify only approved source files.
- Maximum three repair attempts.
- Log every attempt.
- Roll back unsuccessful patches.

---

## ♻️ Self-Healing Loop

The self-healing loop is the defining feature of OmniSight.

```text
             BUG FOUND
                 │
                 ▼
          Generate Fix
                 │
                 ▼
           Apply Patch
                 │
                 ▼
          Re-Screenshot
                 │
                 ▼
           VLM Verify
            /        \
         FAIL        PASS
          │            │
          ▼            ▼
      Rollback     Fix Verified
          │            │
          └─ Retry     ▼
                     Open PR
```

Verification checks whether the **original defect is actually gone**, not merely whether the application builds successfully.

### Loop Rules

- Bounded retry: at most 2–3 attempts per finding.
- Every failed attempt is rolled back to baseline.
- Finding, patch, tests, and screenshots are logged for every attempt.
- After the final failure, the issue is escalated to a human.

---

## 🐙 GitHub Integration

Once a fix is verified, OmniSight converts it into a reviewable software change.

```text
Branch → Commit → Push → Pull Request → Human Review
```

Example automated branch:

```text
omnisight/auto-fix-checkout
```

Example commit:

```text
fix(ui): prevent clipped Checkout button on mobile
```

### Pull Request Evidence

A generated PR can contain:

- Original screenshot
- Fixed screenshot
- Structured bug report
- VLM confidence
- Validation result
- Build result
- Playwright result
- VLM re-check result

**Automation proposes; people approve.**

---

## ⚙️ CI/CD Pipeline

After a Pull Request is created, the delivery pipeline validates the change.

```text
Git Push / PR
      │
      ▼
Install Dependencies
      │
      ▼
Lint + Type Checks
      │
      ▼
Build
      │
      ▼
Unit Tests
      │
      ▼
Playwright UI Tests
      │
      ▼
   PASS / FAIL
```

On failure, merge is blocked and the PR remains open. On success, the change can be deployed to staging and proceed to a human approval gate before production.

FastAPI can receive CI/CD webhooks and update the React dashboard with live project state.

---

## 📊 Control Dashboard

The React dashboard provides human visibility and approval throughout the workflow.

Main dashboard sections can include:

- Scans
- Findings
- Pull Requests
- Logs
- Settings

For an individual scan, the dashboard can show:

```text
3 Bugs     2 Fixed     1 PR
```

A finding can display:

```text
UI BUG
Checkout button clipped
VLM confidence: 0.94
Status: FIX VERIFIED
```

The dashboard also displays before/after evidence and actions such as:

- Approve
- Reject
- Retry

Nothing merges without a person.

---

## ⚛️ Frontend Architecture

The frontend is built using React and communicates with the FastAPI orchestrator.

```text
React Dashboard
      │
      ├── Scans
      ├── Findings
      ├── Pull Requests
      ├── Logs
      └── Settings
      │
      ▼
API Service
      │
      ▼
FastAPI Orchestrator
```

The frontend is responsible for:

- Submitting target URLs
- Starting scans
- Displaying scan status
- Showing detected bugs
- Displaying VLM confidence
- Showing before/after screenshots
- Showing fix status
- Showing GitHub PR status
- Showing CI/CD results
- Providing approval controls
- Displaying logs and retries

---

## 🚨 Engineering Challenges and Controls

| Risk | Engineering Control |
|---|---|
| Visual ambiguity | Deterministic test cases + deliberately injected MVP bugs |
| DOM vs visual truth | Focused screenshots/crops + relevant DOM context |
| Code localization | Approved files and minimal patches only |
| Safe code execution | Build, test and Playwright checks before acceptance |
| Verification | Re-screenshot + VLM check, bounded retries and rollback |
| Context + latency | Crops and selected DOM instead of full-page dumps |
| Non-determinism | Strict JSON schemas + validation and detailed logging |

The final safety gate is **human approval before any automated PR is merged**.

---

## 👥 Team Plan

The project is designed for a four-member team.

### Team Lead — Architecture + Orchestration

- Agent orchestration
- LLM / VLM integration
- Integration and reviews
- Roadmap

### Member 2 — Playwright + RPA

- DOM / screenshot collector
- Test scenarios
- Observation JSON

### Member 3 — React Dashboard

- Frontend UX
- API integration
- Approval workflow

### Member 4 — GitHub + CI/CD

- GitHub API
- Validation and testing
- Deployment workflow

> Everyone should be able to explain the full pipeline end to end.

---

## 📅 Four-Week Implementation Plan

### Week 1 — Foundation

- React target app
- FastAPI backend
- Playwright worker
- Screenshots + DOM to JSON
- GitHub repository

**Milestone:** Screenshots + JSON

### Week 2 — AI Detection

- VLM integration
- Structured bug report
- Deliberate UI bug demo
- Fix extraction

**Milestone:** First bug detected

### Week 3 — Self-Healing

- LLM patch generation
- Safe apply
- Re-screenshot
- VLM verification
- GitHub PR

**Milestone:** Verified fix + PR

### Week 4 — Productize

- Dashboard
- CI/CD
- Image crops
- Logs + retries
- Documentation + demo

**Milestone:** Demo ready

---

## 🎬 MVP Demo Strategy

> **One bug, end to end, beats ten half-built features.**

The MVP demonstration focuses on one known defect and proves the complete workflow.

1. User pastes the mock-store URL.
2. Playwright opens Checkout at a mobile viewport.
3. Screenshot + DOM are collected.
4. VLM detects the clipped Checkout button.
5. LLM generates a minimal CSS patch.
6. Patch is applied in an isolated branch/workspace.
7. Playwright captures the fixed state.
8. VLM confirms the original issue is resolved.
9. GitHub branch, commit, and PR are created.
10. Dashboard shows before/after evidence.

### Demo Success

```text
DETECT → FIX → VERIFY → REVIEWABLE PR
```

---

## 🧪 Installation

### Prerequisites

Install:

- Git
- Python
- Node.js
- npm
- Playwright-compatible browser
- AI model/API required by your implementation

Clone the project:

```bash
git clone <repository-url>
cd OmniSight
```

### Backend Setup

```bash
cd backend
python -m venv venv
```

Windows:

```bash
venv\Scripts\activate
```

Linux/macOS:

```bash
source venv/bin/activate
```

Install dependencies and start FastAPI:

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload
```

### Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

The Vite development server will display the local application URL in the terminal.

---

## 🧠 Design Principles

### Evidence First

Collect reliable browser evidence before asking AI to reason about a defect.

### Structured Communication

Components communicate through validated JSON/API contracts.

### Minimal Changes

AI-generated patches should change only what is necessary.

### Safe Execution

Generated code must be validated inside an isolated environment.

### Verification Before Delivery

A successful build does not automatically mean the visual bug is fixed. OmniSight verifies the original defect after the patch.

### Human Approval

**Automation proposes. Humans approve.**

---

## 🔮 Future Improvements

After the deterministic MVP works reliably, OmniSight can be expanded with:

- Additional visual bug categories
- Accessibility testing
- Cross-browser testing
- Multi-page autonomous navigation
- Improved source-code localization
- More advanced VLM models
- Multiple LLM providers
- Automated regression testing
- Screenshot comparison
- Intelligent element cropping
- Historical analytics
- Production monitoring
- Advanced RPA workflows
- Multi-repository support

---

## 🎓 Final Takeaway

OmniSight is **not just a VLM demo**. It is an autonomous QA workflow.

```text
Playwright → Collects Evidence
VLM        → Detects
LLM        → Proposes
Self-Healing Loop → Verifies
GitHub + CI/CD     → Creates a reviewable engineering workflow
```

> **Build the deterministic MVP first; then make the agent smarter.**

---

## 📋 Project Information

**Project:** OmniSight — Multimodal UI Self-Healing & RPA Agent  
**Core Loop:** Detect · Fix · Verify  
**Team Size:** 4 Members  
**Implementation Duration:** 1 Month  
**Domain:** Generative AI · RPA · Quality Assurance · Web Development · DevOps  
**Frontend:** React, JavaScript, HTML5, CSS3, Vite  
**Backend:** Python, FastAPI, REST/JSON  
**Browser automation:** Playwright  
**AI:** Vision-Language Model and Large Language Model

---

## 🖥️ Frontend Dashboard (React + Vite)

> Contributed by the frontend team. The dashboard operates the workflow and
> reviews its evidence.

### Frontend Features

The dashboard should provide the following screens and user actions:

- **Scans:** submit a target URL, choose viewports, start a scan, and track its status.
- **Findings:** review visual defects such as clipped controls, overlapping elements, low-contrast text, and responsive-layout failures.
- **Finding details:** show severity, confidence, target selector, evidence, and remediation status.
- **Before / After:** compare screenshots from the original and patched UI.
- **Pull Requests:** display branch, commit, PR status, and links to reviewable changes.
- **Logs:** show scan events, validation output, retries, and errors.
- **Approval controls:** approve, reject, or retry proposed fixes. A human approval step is required before merge.

The dashboard should communicate that evidence is collected by Playwright, defects are interpreted by a Vision-Language Model (VLM), patches are proposed by an LLM/fix engine, and changes are validated in an isolated workspace.

### Frontend Technology Stack

- React
- JavaScript
- HTML5
- CSS3
- Vite
- REST/JSON API integration with the FastAPI backend

### Suggested Frontend Structure

```text
frontend/
├── src/
│   ├── components/
│   │   ├── Sidebar.jsx
│   │   ├── ScanForm.jsx
│   │   ├── Findings.jsx
│   │   ├── FindingCard.jsx
│   │   ├── BeforeAfter.jsx
│   │   ├── PullRequests.jsx
│   │   ├── Logs.jsx
│   │   └── ApprovalControls.jsx
│   ├── services/
│   │   └── api.js
│   ├── App.jsx
│   ├── main.jsx
│   └── styles.css
├── index.html
├── package.json
└── vite.config.js
```

Adjust component names to match the actual implementation, while keeping the UI organized by responsibility.

### Dashboard Workflow

1. The user submits a target URL from the dashboard.
2. The backend starts a Playwright run and captures screenshots, DOM information, console errors, and network evidence.
3. The VLM returns structured findings.
4. The fix engine proposes a minimal code patch.
5. The backend applies the patch in an isolated workspace and runs build/tests.
6. Playwright captures the patched UI and the VLM verifies the original defect is gone.
7. The dashboard displays before/after evidence and validation status.
8. A reviewable GitHub PR is created only after successful verification.
9. A human reviews and approves the change before merge.

### Local Development

#### Prerequisites

- Node.js and npm
- The OmniSight FastAPI backend, if you want live API data

#### Install and run

From the frontend directory:

```bash
npm install
npm run dev
```

Vite prints the local development URL in the terminal.

#### Backend integration

Configure the frontend API base URL to point to your FastAPI orchestrator. Keep API calls in `src/services/api.js` rather than embedding fetch logic throughout UI components. Use validated JSON contracts for scan status, findings, screenshots, validation results, and pull-request information.

If the backend is not running, the frontend can use clearly labelled mock/demo data for presentation, but must not imply that real scans, AI fixes, or GitHub PRs have occurred.

### UX and Safety Principles

- **Evidence first:** show screenshots and relevant DOM context for each finding.
- **Clear states:** distinguish queued, running, failed, fixed, verified, and awaiting review.
- **Minimal patches:** show what changed and why.
- **Safe execution:** backend changes are validated in an isolated environment.
- **Bounded retries:** failed patches are rolled back; repeated failures are escalated.
- **Human approval:** automation proposes changes; people approve before merge.
- **Accessible responsive UI:** maintain readable contrast, keyboard access, and mobile-friendly layouts.

### Frontend System Context

The frontend is one part of a modular system:

```text
React Dashboard
      │
      ▼
FastAPI Orchestrator
      ├── Playwright Worker ── Screenshots + DOM
      ├── VLM Service ──────── Visual defect findings
      ├── LLM / Patch Engine ─ Minimal source-code patch
      ├── Validation Sandbox ─ Build + tests + re-check
      └── GitHub + CI/CD ───── Branch, commit, PR, checks
```

The backend and worker services are separate from the frontend and need their own implementation and configuration.

### MVP Demo

Demonstrate one known visual defect through the supported local workflow:

1. Start the fixed ShopLite checkout scan.
2. Capture the mobile viewport (for example, 390 × 844).
3. Detect the clipped Checkout button.
4. Review the restricted CSS patch proposal.
5. Explicitly approve isolated Docker verification.
6. Compare the persisted before/after mobile screenshots, browser checks, and run history.
7. Optionally validate and publish a draft PR when GitHub App configuration is enabled.

> **Demo success:** Detect → Review proposal → Verify in the sandbox → Optionally publish a draft PR.

### Local checkout browser fixture

`demo-app/` is a deterministic, local-only ShopLite checkout used to exercise
the browser evidence step. Its mobile Place Order button is intentionally too
wide so the runner has a known defect to find. The order action is simulated;
it does not collect payment or send order data.

From the repository root, install the backend dependencies and Chromium once:

```powershell
cd backend
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
..\.venv\Scripts\python.exe -m playwright install chromium
```

In one PowerShell window, serve the fixture from the repository root:

```powershell
cd demo-app
python -m http.server 4173 --bind 127.0.0.1
```

In another PowerShell window, capture desktop and mobile evidence:

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\capture_demo_checkout.py
```

The fixed flow checks the checkout heading and summary, exercises the simulated
order action, records console errors, measures the button, and writes
`observation/v1` JSON plus screenshots under `backend/artifacts/demo-checkout/`.
By default it expects the deliberate mobile overflow and exits successfully
when it detects it. After fixing the CSS, run
`scripts\capture_demo_checkout.py --expected no-overflow`; it succeeds only
when the button fits the mobile viewport. The runner only accepts HTTP loopback
URLs and does not execute supplied flow data or modify application files.

To capture the page and also upload the mobile evidence to a running backend,
trigger detection, and wait for the result, run this from `backend/`:

```powershell
.\.venv\Scripts\python.exe scripts\run_demo_detection.py
```

The output labels the analyzer as `MOCK` or `GEMINI`. The default mock uses the
captured element bounds, so it can demonstrate both the broken and fixed state
without a provider key.

To run the complete fixed-demo flow through detection, safe patch proposal, and
the isolated Docker browser check, see
[`docs/week3_shoplite_sandbox.md`](docs/week3_shoplite_sandbox.md#run-the-complete-gemini-to-browser-verification-demo).
The runner requires `--approve-sandbox` and reports success only when the
candidate passes the mobile/desktop Playwright checks.

## Gemini patch suggestions (backend)

The backend exposes `POST /api/ai/patch-suggestions` for a direct, review-only Gemini proposal. It also supports Gemini through the standard finding workflow: set `OMNISIGHT_PATCH_GENERATOR=gemini`, then call `POST /api/v1/findings/{finding_id}/patch-proposals`. That workflow loads only backend-approved source files and runs the existing extractor, path allowlist, edit applicability, and code checks before recording a proposal. Neither endpoint writes or executes code.

Copy `backend/.env.example` to `backend/.env`, set `OMNISIGHT_GEMINI_API_KEY`, and configure `OMNISIGHT_PATCH_SOURCE_ROOT` plus `OMNISIGHT_APPROVED_PATCH_PATHS` for the source checkout being tested. The key remains on the backend and is sent to Google in the `x-goog-api-key` header. Keep it out of frontend code and source control. Set `OMNISIGHT_GEMINI_MODEL` if a different model is enabled for your key. The default generator remains `mock` until you explicitly switch it to `gemini`.

### Gemini visual detection

The visual analyzer can also use Gemini. Set `OMNISIGHT_VLM_ADAPTER=gemini` in
`backend/.env`; it uses the same server-side Gemini key and model setting as
patch generation. The adapter sends the stored screenshot and bounded page
measurements to Gemini, then the existing AI gateway validates the returned
JSON before it can become a finding. The default remains the offline `mock`
adapter. Google documents `generateContent` multimodal input and the
`x-goog-api-key` header in its [Gemini API docs](https://ai.google.dev/gemini-api/docs/generate-content).

With the local browser fixture and backend running, use PowerShell from
`backend/` to upload its mobile observation and request real visual analysis:

```powershell
$scan = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/scans `
  -ContentType 'application/json' `
  -Body '{"target_url":"http://127.0.0.1:4173/checkout","viewports":[{"width":390,"height":844}],"routes":["/checkout"]}'

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/scans/$($scan.scan_id)/evidence" `
  -ContentType 'application/json' `
  -InFile 'artifacts/demo-checkout/mobile/observation.json'

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/scans/$($scan.scan_id)/detect"

Invoke-RestMethod -Method Get `
  -Uri "http://127.0.0.1:8000/api/v1/scans/$($scan.scan_id)/findings"
```

The detection response records `GEMINI` provenance when the live adapter ran.
If the key has no quota, the model is not enabled, or the provider is
unavailable, the run records a failure reason instead of claiming a finding.
Patch generation stays a separate explicit step, and proposals still require
review; no model output is applied automatically.

See the API docs at `http://127.0.0.1:8000/docs` after starting the backend. Returned edits are proposals; review them before applying anything. A 429 response indicates a provider quota/rate limit, while 502–504 responses indicate provider, configuration, or timeout failures.
