# 👁️ OmniSight

## Multimodal UI Self-Healing & RPA Agent

OmniSight is an AI-powered UI testing and self-healing system designed to automatically inspect websites, detect visual UI problems, and support AI-generated fixes.

The updated OmniSight architecture uses **React** for the frontend, **FastAPI** for backend APIs, and **Playwright** for browser automation and screenshot capture. The captured webpage evidence is then analyzed by a **Vision-Language Model (VLM)** to identify visual UI issues. An **LLM workflow** can subsequently reason about detected problems and generate possible code fixes.

---

## 🚀 Project Workflow

```text
React Frontend
      ↓
Website URL
      ↓
FastAPI Backend
      ↓
Playwright Browser Automation
      ↓
Open Target Website
      ↓
Capture Full-Page Screenshot
      ↓
Screenshot Evidence
      ↓
VLM Analysis
      ↓
UI Findings
      ↓
React Dashboard
      ↓
LLM Reasoning / Patch Proposal
      ↓
Review → Validate → Deliver
```

### Role of Each Component

**React**
- Provides the OmniSight user interface.
- Accepts the target website URL.
- Displays scan progress.
- Shows Playwright capture information.
- Displays VLM findings.

**FastAPI**
- Connects the frontend with backend services.
- Creates and manages scans.
- Provides API endpoints for Playwright, VLM, and LLM workflows.

**Playwright**
- Runs before VLM analysis.
- Launches a headless Chromium browser.
- Opens the target website.
- Captures a full-page screenshot.
- Provides visual evidence for AI analysis.

**VLM**
- Analyzes the screenshot captured by Playwright.
- Detects visual UI problems.
- Generates structured findings about detected issues.

**LLM**
- Uses the detected findings for reasoning.
- Can generate possible code fixes or patch proposals.
- Supports the self-healing workflow.

---

# 🏗️ Project Structure

```text
OmniSight/
│
├── frontend/
│   │
│   ├── index.html
│   ├── package.json
│   ├── vite.config.js
│   │
│   └── src/
│       ├── App.jsx
│       ├── main.jsx
│       └── styles.css
│
├── backend/
│   │
│   └── app/
│       ├── main.py
│       │
│       └── routers/
│           └── playwright_capture.py
│
├── artifacts/
│   └── playwright/
│       └── <scan_id>.png
│
└── README.md
```

The backend may contain additional routers, models, services, database files, migrations, VLM adapters, patch generators, tests, and configuration files depending on the current OmniSight backend version.

---

# 💻 Frontend

The OmniSight frontend has been migrated from plain HTML/CSS/JavaScript to **React with Vite**.

Main frontend files:

```text
frontend/
├── index.html
├── package.json
├── vite.config.js
└── src/
    ├── App.jsx
    ├── main.jsx
    └── styles.css
```

### `App.jsx`

Contains the main OmniSight interface and scan workflow.

The current frontend executes:

```text
Create Scan
    ↓
Playwright Capture
    ↓
VLM Detection
    ↓
Retrieve Findings
   
