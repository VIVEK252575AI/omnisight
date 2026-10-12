# 👁️ OmniSight

## Multimodal UI Self-Healing & RPA Agent

OmniSight is an **AI-powered autonomous QA workflow** designed to detect visual UI defects, reason about their causes, generate targeted fixes, verify those fixes, and deliver reviewable changes through GitHub.

> **Core Loop:** Detect → Fix → Verify → Deliver

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
│   │   ├── main.py
│   │   ├── orchestrator.py
│   │   ├── schemas.py
│   │   ├── storage.py
│   │   └── config.py
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

> The exact folder structure can be adjusted during implementation while keeping the same modular architecture.

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
  "url": "https://staging.shop.example/checkout",
  "viewport": {
    "width": 390,
    "height": 844
  },
  "screenshot": "artifacts/checkout-390.png",
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
  "network_errors": []
}
```

In this example, the button exists in the DOM but lies outside the 390 px mobile viewport.

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

It returns a structured finding such as:

```json
{
  "type": "clipped_element",
  "target": "#checkout",
  "evidence": "box.x + w = 540 > viewport 390",
  "severity": "high",
  "confidence": 0.94
}
```

This lets OmniSight combine visual understanding with browser evidence instead of relying only on DOM state.

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
- Invalid or truncated model responses are treated as a first-class failure — never silently retried with fabricated values, never guessed into a finding.
- Each finding carries: `type`, `target`, `evidence`, `severity`, and `confidence`, matching the example above.
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
