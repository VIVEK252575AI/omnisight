# 👁️ OmniSight

## Multimodal UI Self-Healing & RPA Agent — Frontend
OmniSight — Autonomous UI Engineer
==================================

Files
-----
- code.html   : Front-end markup and JavaScript.
- styles.css  : All styling (design system, layout, VLM/LLM components).
- screen.png  : UI design reference screenshot.
- README.txt  : This file.

Keep code.html and styles.css in the same folder.

How to run
----------
1. Extract this folder.
2. Open code.html in a modern browser (Chrome, Edge, or Firefox).
3. No build step is required.

What's new in this version
--------------------------
The UI now shows which model does what in a scan.

VLM (vision-language model) - looks at screenshots
  - Detect: reads the page screenshot (390px, or 390/768/1440px with Deep scan)
    and returns defects with type, severity, confidence and a bounding box.
  - Verify & Ship: compares the before and after captures, reports cleared
    defects and regressions.

LLM (language model) - reads and writes code
  - Reason: takes the VLM's defects plus the DOM/CSS for each selector and
    explains the root cause.
  - Fix: writes the smallest CSS/DOM patch as a unified diff.

Sandbox (no model)
  - Validate: applies the patch, then runs lint and visual tests.

UI changes
  - Composer: pick the VLM and the LLM before scanning; Deep scan is now a
    real on/off toggle.
  - Pipeline stepper: every stage carries a VLM, LLM or SANDBOX tag and updates
    live as the scan advances. A failed stage is shown in red.
  - Visual perception panel: screenshot with the VLM's bounding boxes. Select a
    finding to highlight its box.
  - Reasoning panel: the LLM's root cause for the selected finding.
  - Generated patch panel: the LLM's diff.
  - Before / after verification panel: side-by-side captures.
  - Model trace panel: per-stage tabs showing model name, latency, prompt and
    output.
  - Models page (sidebar): switch between simulated and live mode and set the
    name and endpoint of each model.
  - Landing cards, the Ship PR button and the focus ring were also updated.

Simulated vs live mode
----------------------
Simulated (default): no network calls. Each stage waits about a second and
returns built-in sample output. Nothing is scanned, no real model runs, and
"Ship PR" only shows a message.

Live: each model stage sends a POST to the endpoint set on the Models page
(defaults /api/vlm and /api/llm). The stages Validate and the PR step are not
called; they still need your backend.

Request body (JSON)
  { "stage": "detect | reason | fix | verify",
    "model": "<model name>",
    "url": "<page being scanned>",
    "deep": true | false,
    "prompt": "<prompt text>" }

Response body (JSON)
  { "output": "<text or JSON shown in the Model trace panel>",
    "findings": [ ... ] }       <- Detect stage only

Each finding needs: id, title, type, severity (high | medium | low),
confidence (e.g. "94%"), selector, box [x, y, width, height] in a 390x320
coordinate space, and cause (short root-cause text, HTML allowed).

Notes
-----
- The Fix, patch and verify panels still display the sample CheckoutButton.css
  patch and sample before/after images, even in live mode. Wire these to your
  backend's output when you connect it.
- Live mode from a file:// page needs endpoints that allow CORS.
- Settings are held in memory and reset on reload.
- This is a front-end prototype. Real scanning, model inference, patch
  application and pull-request creation require a backend.

OmniSight is an AI-powered autonomous QA workflow that detects visual UI defects, proposes targeted fixes, verifies the result, and prepares reviewable changes through GitHub.

This repository's **frontend** is a React + Vite dashboard for operating the workflow and reviewing its evidence.

> **Core loop:** Detect → Fix → Verify → Deliver

## Frontend Features

The dashboard should provide the following screens and user actions:

- **Scans:** submit a target URL, choose viewports, start a scan, and track its status.
- **Findings:** review visual defects such as clipped controls, overlapping elements, low-contrast text, and responsive-layout failures.
- **Finding details:** show severity, confidence, target selector, evidence, and remediation status.
- **Before / After:** compare screenshots from the original and patched UI.
- **Pull Requests:** display branch, commit, PR status, and links to reviewable changes.
- **Logs:** show scan events, validation output, retries, and errors.
- **Approval controls:** approve, reject, or retry proposed fixes. A human approval step is required before merge.

The dashboard should communicate that evidence is collected by Playwright, defects are interpreted by a Vision-Language Model (VLM), patches are proposed by an LLM/fix engine, and changes are validated in an isolated workspace.

## Frontend Technology Stack

- React
- JavaScript
- HTML5
- CSS3
- Vite
- REST/JSON API integration with the FastAPI backend

## Suggested Frontend Structure

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

## Dashboard Workflow

1. The user submits a target URL from the dashboard.
2. The backend starts a Playwright run and captures screenshots, DOM information, console errors, and network evidence.
3. The VLM returns structured findings.
4. The fix engine proposes a minimal code patch.
5. The backend applies the patch in an isolated workspace and runs build/tests.
6. Playwright captures the patched UI and the VLM verifies the original defect is gone.
7. The dashboard displays before/after evidence and validation status.
8. A reviewable GitHub PR is created only after successful verification.
9. A human reviews and approves the change before merge.

## Local Development

### Prerequisites

- Node.js and npm
- The OmniSight FastAPI backend, if you want live API data

### Install and run

From the frontend directory:

```bash
npm install
npm run dev
```

Vite prints the local development URL in the terminal.

### Backend integration

Configure the frontend API base URL to point to your FastAPI orchestrator. Keep API calls in `src/services/api.js` rather than embedding fetch logic throughout UI components. Use validated JSON contracts for scan status, findings, screenshots, validation results, and pull-request information.

If the backend is not running, the frontend can use clearly labelled mock/demo data for presentation, but must not imply that real scans, AI fixes, or GitHub PRs have occurred.

## UX and Safety Principles

- **Evidence first:** show screenshots and relevant DOM context for each finding.
- **Clear states:** distinguish queued, running, failed, fixed, verified, and awaiting review.
- **Minimal patches:** show what changed and why.
- **Safe execution:** backend changes are validated in an isolated environment.
- **Bounded retries:** failed patches are rolled back; repeated failures are escalated.
- **Human approval:** automation proposes changes; people approve before merge.
- **Accessible responsive UI:** maintain readable contrast, keyboard access, and mobile-friendly layouts.

## Full System Context

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

## MVP Demo

Demonstrate one known visual defect end to end:

1. Submit the ShopLite checkout URL.
2. Capture the mobile viewport (for example, 390 × 844).
3. Detect the clipped Checkout button.
4. Propose and apply a minimal CSS fix.
5. Re-capture and verify the UI.
6. Show the before/after evidence and validation status.
7. Display the resulting reviewable PR and wait for human review.

> **Demo success:** Detect → Fix → Verify → Reviewable PR.

## Project Information

- **Project:** OmniSight — Multimodal UI Self-Healing & RPA Agent
- **Frontend:** React, JavaScript, HTML5, CSS3, Vite
- **Backend:** Python, FastAPI, REST/JSON
- **Browser automation:** Playwright
- **AI:** Vision-Language Model and Large Language Model
- **Domain:** Generative AI, RPA, Quality Assurance, Web Development, DevOps
- **Team size:** 4 members
- **Planned implementation duration:** 1 month
