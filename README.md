OmniSight — Autonomous UI Engineer
==================================

Files
-----
- code.html   : Page markup (loads styles.css and script.js).
- script.js   : All JavaScript (state, VLM/LLM pipeline, rendering, shortcuts).
- styles.css  : All styling (design system, layout, VLM/LLM components).
- screen.png  : Landing page screenshot.
- screen-scan.png / screen-dark.png : Scan dashboard screenshots (light and dark).
- README.txt  : This file.

Keep code.html, styles.css and script.js in the same folder.

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

Enhancements (latest version)
-----------------------------
  - Dark mode: toggle in the top bar. Follows your system setting on first
    visit, then remembers your choice.
  - History page: now works. Every finished or failed scan is recorded
    (URL, time, findings, fixed count, status) with a Re-run button.
  - Sidebar search: filters Recent scans and the History list as you type.
  - Severity filter: All / High / Medium / Low chips above the findings list.
  - Copy patch: copies the generated diff to the clipboard.
  - Export report: downloads the scan as JSON (URL, models, findings, patch,
    full model trace).
  - Enter key in the URL box starts a scan.
  - Saved in your browser (localStorage): theme, model settings and the last
    50 scans. Clearing site data resets them.
  - Colors: the original purple theme. To change the brand color, edit the
    --purple* variables in the :root block of styles.css.

Design and motion (latest version)
----------------------------------
  - Landing: drifting aurora glow, pulsing orb rings, gradient heading,
    three info chips, staggered entrance, glowing composer on focus, and
    cards that lift with a tilted icon on hover.
  - Pipeline: a purple progress bar fills along the bottom, and the running
    step pulses.
  - Visual perception: a laser line sweeps the screenshot while the VLM is
    detecting; bounding boxes pop in once defects are found.
  - UI health score: an animated ring. Score = 100 minus a penalty per
    finding (high 20, medium 10, low 5). It shows 100 after verification,
    because every finding is then cleared.
  - Findings: colored stripe per severity (red, amber, purple).
  - Before/after: drag the slider to compare the two captures.
  - Confetti burst when a scan completes.
  - All motion switches off if your system has "reduce motion" turned on.

Keyboard shortcuts (when not typing in a field)
-----------------------------------------------
  /  focus search     n  new scan     h  history     d  toggle dark mode
  Esc  leave the field you are typing in

Notes
-----
- The Fix, patch and verify panels still display the sample CheckoutButton.css
  patch and sample before/after images, even in live mode. Wire these to your
  backend's output when you connect it.
- Live mode from a file:// page needs endpoints that allow CORS.
- Settings and history are saved in this browser's localStorage.
- This is a front-end prototype. Real scanning, model inference, patch
  application and pull-request creation require a backend.
