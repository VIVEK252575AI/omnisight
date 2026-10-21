/* OmniSight front-end logic: state, VLM/LLM pipeline, rendering. */
/* ============================================================================
   OmniSight — Full Application Engine & Preserved Functionality
   ========================================================================== */
'use strict';

// Retain all existing models & mock generators from original omnisight.html
const Shots = (() => {
  const uri = (svg) => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  const F = 'font-family="Inter,sans-serif"';
  return {
    checkoutBefore: uri(`<svg viewBox="0 0 390 320" width="390" height="320" xmlns="http://www.w3.org/2000/svg"><rect width="390" height="320" fill="#f8fafc"/><text x="20" y="40" ${F} font-size="16" font-weight="700">ShopLite Checkout</text><rect x="20" y="80" width="350" height="120" rx="10" fill="#fff" stroke="#e2e8f0"/><rect x="270" y="240" width="160" height="44" rx="8" fill="#111"/><text x="350" y="267" ${F} fill="#fff" text-anchor="middle" font-size="14">Checkout</text></svg>`),
    checkoutAfter: uri(`<svg viewBox="0 0 390 320" width="390" height="320" xmlns="http://www.w3.org/2000/svg"><rect width="390" height="320" fill="#f8fafc"/><text x="20" y="40" ${F} font-size="16" font-weight="700">ShopLite Checkout</text><rect x="20" y="80" width="350" height="120" rx="10" fill="#fff" stroke="#e2e8f0"/><rect x="20" y="240" width="350" height="44" rx="8" fill="#111"/><text x="195" y="267" ${F} fill="#fff" text-anchor="middle" font-size="14">Checkout · $140.61</text></svg>`)
  };
})();

const Store = {
  route: 'landing', // 'landing' | 'scan' | 'models'
  deep: true,
  sev: 'all',
  traceTab: null,
  scan: null,
  activeFindingId: 'f-1',
  githubValidation: null,
  githubIdempotencyKey: null,
  demoRunId: null,
  demoRun: null,
  demoError: null,
  githubToken: null,
  githubConnection: null,
  githubValidation: null,
  githubIdempotencyKey: null,
  githubOperation: null,
  githubError: null,
  history: [
    { id: '1041', url: 'https://shoplite.local', status: 'completed', findings: 3, fixed: 3, pr: 127 },
    { id: '1040', url: 'https://staging.acme.io/dashboard', status: 'failed', findings: 2, fixed: 0, pr: null },
    { id: '1039', url: 'https://acme.io/marketing', status: 'completed', findings: 4, fixed: 4, pr: 124 }
  ]
};

function toast(msg) {
  const root = document.getElementById('toast-root');
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `<span>✨</span><span>${msg}</span>`;
  root.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

/* ---- Persistence (localStorage, safe if blocked) and theme ---- */
const LS = {
  get(k, d) { try { const v = localStorage.getItem('omnisight.' + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('omnisight.' + k, JSON.stringify(v)); } catch (e) {} }
};
function applyTheme(t) { document.documentElement.dataset.theme = t; LS.set('theme', t); }
function toggleTheme() { applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'); render(); }
applyTheme(LS.get('theme', matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
Store.history = LS.get('history', Store.history);

/* ---------------------------------------------------------------------------
   The legacy dashboard walkthrough is intentionally simulated. Real model
   requests use the backend-only fixed ShopLite workflow below.
   ------------------------------------------------------------------------- */
const Models = {
  mode: 'simulated',
  vlm: { name: 'OmniVision-VL 7B', endpoint: '/api/vlm' },
  llm: { name: 'OmniCoder-LLM 70B', endpoint: '/api/llm' },
  vlmOptions: ['OmniVision-VL 7B', 'OmniVision-VL 34B'],
  llmOptions: ['OmniCoder-LLM 70B', 'OmniCoder-LLM 8B']
};

{ const m = LS.get('models'); if (m) { Models.mode = 'simulated'; Models.vlm = m.vlm; Models.llm = m.llm;
  if (!Models.vlmOptions.includes(m.vlm.name)) Models.vlmOptions.push(m.vlm.name);
  if (!Models.llmOptions.includes(m.llm.name)) Models.llmOptions.push(m.llm.name); } }

const STAGES = [
  { key: 'detect',   label: 'Detect',        kind: 'vlm', role: 'VLM reads screenshots and locates defects' },
  { key: 'reason',   label: 'Reason',        kind: 'llm', role: 'LLM finds the root cause from DOM + CSS' },
  { key: 'fix',      label: 'Fix',           kind: 'llm', role: 'LLM writes the smallest patch' },
  { key: 'validate', label: 'Validate',      kind: null,  role: 'Sandbox applies patch, runs lint and tests' },
  { key: 'verify',   label: 'Verify & Ship', kind: 'vlm', role: 'VLM compares before/after, then opens the PR' }
];

const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const PATCH = `.checkout .cta-button {
-   min-width: 320px;
+   width: 100%;
+   max-width: 100%;
    padding: 14px 24px;
}`;

const SAMPLE_FINDINGS = [
  { id: 'f-1', title: 'Checkout CTA clipped at 390px viewport', type: 'clipped_element', severity: 'high', confidence: '94%', selector: '#checkout .cta-button', box: [270, 240, 120, 44],
    cause: 'The button has <strong>min-width: 320px</strong> and sits in a 350px container with 20px offset, so it overflows the 390px viewport by 40px.' },
  { id: 'f-2', title: 'Sticky site header overlaps hero title', type: 'overlap', severity: 'medium', confidence: '88%', selector: '.site-header', box: [20, 18, 350, 32],
    cause: 'The header is <strong>position: sticky</strong> with no offset on the hero, so the title renders underneath it after scroll.' },
  { id: 'f-3', title: 'Muted price text fails AA contrast', type: 'low_contrast', severity: 'low', confidence: '81%', selector: '.price--muted', box: [20, 150, 110, 24],
    cause: 'Text colour <strong>#a1a5b3</strong> on white has a contrast ratio of about 2.5:1, below the 4.5:1 AA minimum.' }
];

/* Each stage's prompt and sample output. Used for the trace viewer and as the simulated response. */
function simulate(key, scan) {
  const f = scan.findings;
  const vp = Store.deep ? '390, 768 and 1440px' : '390px';
  return {
    detect: { prompt: `Inspect the screenshot of ${scan.url} at ${vp}. Return JSON defects with type, severity, confidence and bbox.`,
      output: JSON.stringify({ defects: f.map(d => ({ id: d.id, type: d.type, severity: d.severity, confidence: d.confidence, bbox: d.box })) }, null, 2) },
    reason: { prompt: `Given the VLM defects and the DOM/CSS for ${f.map(d => d.selector).join(', ')}, explain each root cause.`,
      output: f.map(d => `${d.id}: ${String(d.cause).replace(/<[^>]+>/g, '')}`).join('\n\n') },
    fix: { prompt: 'Write the smallest CSS/DOM patch for the highest-severity defect. Return a unified diff.', output: PATCH },
    validate: { prompt: 'No model. Apply the patch in a sandbox, then run lint and visual tests.',
      output: '✓ patch applies cleanly\n✓ stylelint: 0 errors\n✓ visual tests: 24/24 passed' },
    verify: { prompt: 'Compare before.png and after.png. Report cleared defects and any regressions.',
      output: JSON.stringify({ cleared: f.length, regressions: 0, similarity: 0.994 }, null, 2) }
  }[key];
}

async function callStage(st, scan) {
  const sim = simulate(st.key, scan);
  const cfg = st.kind ? Models[st.kind] : null;
  const t0 = performance.now();
  let output = sim.output;
  await new Promise(r => setTimeout(r, 1000 + Math.random() * 500));
  scan.trace[st.key] = { model: cfg ? cfg.name : 'Sandbox', kind: st.kind, prompt: sim.prompt, output, ms: Math.round(performance.now() - t0) };
}

const refresh = scan => { if (Store.scan === scan && Store.route === 'scan') render(); };

async function runPipeline(scan) {
  try {
    for (let i = 0; i < STAGES.length; i++) {
      scan.stage = i; refresh(scan);
      await callStage(STAGES[i], scan);
    }
    scan.stage = STAGES.length; scan.status = 'completed'; recordScan(scan); burst();
    toast('Scan complete: ' + scan.findings.length + ' findings verified');
  } catch (e) {
    scan.status = 'failed'; scan.error = e.message; recordScan(scan);
    toast('Scan failed: ' + e.message);
  }
  refresh(scan);
}

function recordScan(s) {
  const ok = s.status === 'completed';
  Store.history.unshift({ id: s.id, url: s.url, status: s.status, findings: s.findings.length, fixed: ok ? s.findings.length : 0, pr: null, at: Date.now() });
  Store.history = Store.history.slice(0, 50);
  LS.set('history', Store.history);
}
const recentScans = () => [...new Map(Store.history.map(x => [x.url, x])).values()].slice(0, 5);
function filterScans(q) {
  q = q.trim().toLowerCase();
  document.querySelectorAll('.recent-scan-btn, .hist-row').forEach(el => { el.hidden = !!q && !el.textContent.toLowerCase().includes(q); });
}
function downloadReport() {
  const s = Store.scan;
  const report = { url: s.url, status: s.status, mode: Models.mode, models: { vlm: Models.vlm.name, llm: Models.llm.name }, findings: s.findings, patch: PATCH, trace: s.trace, generatedAt: new Date().toISOString() };
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
  a.download = 'omnisight-report-' + s.id + '.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function downloadLiveReport() {
  if (!Store.demoRun?.result) return;
  const report = { product: 'OmniSight ShopLite demo', ...Store.demoRun, exportedAt: new Date().toISOString() };
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
  a.download = `omnisight-${Store.demoRun.run_id}.json`; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function copyPatch() {
  (navigator.clipboard ? navigator.clipboard.writeText(PATCH) : Promise.reject()).then(() => toast('Patch copied'), () => toast('Copy failed. Select the diff and copy it manually.'));
}

function burst() {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const cols = ['#8b5cf6', '#a78bfa', '#c4b5fd', '#10b981', '#f59e0b'];
  for (let i = 0; i < 28; i++) {
    const e = document.createElement('i'); e.className = 'confetti';
    e.style.cssText = `left:${42 + Math.random() * 16}%;background:${cols[i % 5]};--dx:${((Math.random() - .5) * 640).toFixed(0)}px;--dy:${(-180 - Math.random() * 320).toFixed(0)}px;--r:${(Math.random() * 720).toFixed(0)}deg`;
    document.body.appendChild(e); setTimeout(() => e.remove(), 1600);
  }
}

function toggleDeep(btn) {
  Store.deep = !Store.deep;
  btn.setAttribute('aria-pressed', Store.deep);
}

function startScan(url) {
  if (!url || !url.includes('.')) { toast('Enter a valid URL (e.g. https://shoplite.local)'); return; }
  const v = document.getElementById('sel-vlm'), l = document.getElementById('sel-llm');
  if (v) Models.vlm.name = v.value;
  if (l) Models.llm.name = l.value;
  const scan = {
    id: String(1042 + Store.history.length), url, status: 'running', stage: 0, trace: {}, error: null,
    findings: Models.mode === 'live' ? [] : SAMPLE_FINDINGS.map(f => ({ ...f }))
  };
  Store.route = 'scan'; Store.scan = scan; Store.activeFindingId = 'f-1'; Store.traceTab = null;
  render();
  toast('Scan started for ' + url);
  runPipeline(scan);
}

function renderLiveDemo() {
  const run = Store.demoRun;
  const result = run?.result || {};
  const capture = result.capture || {};
  const findings = Array.isArray(result.findings) ? result.findings : [];
  const proposal = result.proposal || null;
  const sandbox = result.sandbox || null;
  const checks = sandbox?.checks || {};
  const finishedStages = {
    queued: 0, running: 0, awaiting_sandbox_approval: 3, validating: 3,
    verified: 4, validation_failed: 3, patch_rejected: 2, needs_inspection: 1, failed: 0
  }[run?.status] || 0;
  const currentStage = run?.status === 'running' ? 0 : run?.status === 'validating' ? 3 : -1;
  const stages = [
    'Capture browser evidence',
    `Detect with ${result.detection_adapter || 'configured vision model'}`,
    'Generate safe patch proposal',
    'Validate in Docker sandbox'
  ].map((label, index) => ({ label, done: index < finishedStages, current: index === currentStage }));
  const stateLabel = {
    queued: 'Queued', running: 'Running browser capture and AI analysis',
    awaiting_sandbox_approval: 'Patch proposal ready for your review',
    validating: 'Running isolated Docker verification', verified: 'Verified',
    patch_rejected: 'Patch rejected by safety checks', needs_inspection: 'Needs human inspection',
    validation_failed: 'Sandbox checks did not pass', failed: 'Run failed'
  }[run?.status] || 'Ready to run';
  const viewport = capture.viewports?.mobile;
  const overflowBefore = checks.baseline_mobile_overflow_px ?? viewport?.button_overflow_px;
  const overflowAfter = checks.patched_mobile_overflow_px;
  const canApprove = run?.status === 'awaiting_sandbox_approval' && proposal?.status === 'proposed';
  return `<div class="live-demo-view">
    <div class="panel live-demo-intro">
      <div><div class="eyebrow">Connected backend workflow</div><h1>ShopLite live demo</h1>
      <p>Run the fixed, local checkout through Playwright capture → Gemini detection → safe patch proposal. Sandbox validation waits for your explicit approval.</p></div>
      <button class="btn btn--primary" ${['queued','running','awaiting_sandbox_approval','validating'].includes(run?.status) ? 'disabled' : ''} onclick="startLiveShopLiteRun()">${run ? 'Run again' : 'Start real scan'}</button>
    </div>
    ${Store.demoError ? `<div class="error-note" role="alert">${esc(Store.demoError)}</div>` : ''}
    ${run ? `<div class="panel live-run-status"><div><span class="badge badge--${run.status === 'verified' ? 'success' : run.status === 'failed' || run.status === 'validation_failed' || run.status === 'patch_rejected' ? 'error' : 'info'}">${esc(stateLabel)}</span><span class="panel__sub">Run ${esc(run.run_id)}</span></div>${run.error ? `<p class="error-note">${esc(run.error)}</p>` : ''}</div>
      <div class="live-stage-grid">${stages.map(({label, done, current}) => `<div class="live-stage ${done ? 'live-stage--done' : current ? 'live-stage--current' : ''}"><span>${done ? '✓' : current ? '…' : '·'}</span>${esc(label)}</div>`).join('')}</div>
      ${findings.length ? `<div class="panel"><div class="panel__head"><div class="panel__title">Validated visual findings (${findings.length})</div><span class="panel__sub">${result.detection_is_mock ? 'MOCK adapter' : esc(result.detection_adapter || 'Gemini')}</span></div><div class="panel__body">${findings.map(f => `<article class="live-finding"><div><span class="badge badge--high">${esc(f.category || 'visual issue')}</span><span class="mono muted">${Math.round((f.confidence || 0) * 100)}% confidence</span></div><h3>${esc(f.category === 'clipped_element' ? 'Checkout button clipped on mobile' : f.category || 'Visual issue')}</h3><p>Target: <code>${esc(f.target_candidate || 'Needs inspection')}</code></p><p>Capture: mobile ${esc(viewport?.button_overflow_px ?? '—')}px overflow · desktop ${esc(capture.viewports?.desktop?.button_overflow_px ?? '—')}px</p></article>`).join('')}</div></div>` : ''}
      ${proposal ? `<div class="panel"><div class="panel__head"><div class="panel__title">Patch proposal</div><span class="badge badge--${proposal.status === 'proposed' ? 'success' : 'error'}">${esc(proposal.status)}</span></div><div class="panel__body">${(proposal.files || []).map(file => `<div class="live-file"><code>${esc(file.path)}</code><span>${esc(file.edits)} edit${file.edits === 1 ? '' : 's'} · ${esc(file.bytes)} bytes</span></div>`).join('')}${proposal.rejection_reasons?.length ? `<p class="error-note">Safety checks: ${proposal.rejection_reasons.map(esc).join(', ')}</p>` : ''}<p class="panel__sub">Generator: ${esc(proposal.generator_label || 'unknown')}${proposal.is_mock ? ' · simulated provider output' : ' · live provider response'}</p></div></div>` : ''}
      ${canApprove ? `<div class="panel live-approval"><div class="panel__head"><div class="panel__title">Review before sandbox validation</div><span class="panel__sub">Fixed ShopLite demo only</span></div><div class="panel__body"><p>This runs the proposed CSS edit in a disposable container with network access disabled. It does not change the source checkout.</p><div class="field"><label for="live-demo-confirm">Type <code>approve-demo-only</code> to approve this sandbox run</label><input id="live-demo-confirm" autocomplete="off" placeholder="approve-demo-only"></div><button class="btn btn--primary" onclick="validateLiveShopLiteRun()">Approve and validate</button></div></div>` : ''}
      ${sandbox ? `<div class="panel"><div class="panel__head"><div class="panel__title">Sandbox verification</div><span class="badge badge--${sandbox.status === 'verified' ? 'success' : 'error'}">${esc(sandbox.status)}</span></div><div class="panel__body"><div class="live-metrics"><div><span>Mobile overflow</span><strong>${esc(checks.baseline_mobile_overflow_px)}px → ${esc(checks.patched_mobile_overflow_px)}px</strong></div><div><span>Desktop overflow</span><strong>${esc(checks.patched_desktop_overflow_px)}px</strong></div><div><span>Browser errors</span><strong>${esc(checks.browser_error_count)}</strong></div><div><span>External requests blocked</span><strong>${esc(checks.blocked_external_request_count)}</strong></div></div></div></div>` : ''}
      ${sandbox?.status === 'verified' ? renderLocalGithubPanel() : ''}
    ` : `<div class="panel live-empty"><div class="live-empty-icon">◎</div><h2>Ready for a real ShopLite scan</h2><p>The backend will capture the fixed checkout at desktop and mobile sizes, ask the configured vision model to identify defects, then create a safety-checked proposal.</p><p class="panel__sub">The API must be running at <code>http://127.0.0.1:8000</code>. Gemini and the local ShopLite allow-list must be configured in <code>backend/.env</code>.</p></div>`}
  </div>`;
}

async function liveDemoRequest(path, options = {}) {
  if (window.location.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(window.location.hostname) || window.location.port !== '8000') {
    throw new Error('Open the dashboard at http://127.0.0.1:8000/ to use the local ShopLite backend.');
  }
  const response = await fetch(`${window.location.origin}/api/v1/demo/shoplite${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || `Backend returned HTTP ${response.status}`);
  return body;
}

async function startLiveShopLiteRun() {
  Store.route = 'live-demo'; Store.demoRun = null; Store.demoError = null;
  Store.githubValidation = null; Store.githubIdempotencyKey = null; Store.githubOperation = null; Store.githubError = null;
  render();
  try {
    Store.demoRun = await liveDemoRequest('/runs', { method: 'POST', body: '{}' });
    Store.demoRunId = Store.demoRun.run_id;
    render();
    pollLiveShopLiteRun(Store.demoRunId);
  } catch (error) {
    Store.demoError = error.message;
    render();
  }
}

async function pollLiveShopLiteRun(runId) {
  if (Store.demoRunId !== runId) return;
  try {
    const run = await liveDemoRequest(`/runs/${encodeURIComponent(runId)}`);
    Store.demoRun = run;
    if (Store.route === 'live-demo') render();
    if (['queued', 'running', 'validating'].includes(run.status)) {
      setTimeout(() => pollLiveShopLiteRun(runId), 1200);
    }
  } catch (error) {
    Store.demoError = error.message;
    if (Store.route === 'live-demo') render();
  }
}

async function validateLiveShopLiteRun() {
  const confirmation = document.getElementById('live-demo-confirm')?.value.trim();
  if (confirmation !== 'approve-demo-only') { toast('Type approve-demo-only to approve this run'); return; }
  if (!window.confirm('Run this reviewed CSS proposal in the isolated, network-disabled ShopLite sandbox?')) return;
  const runId = Store.demoRunId;
  Store.demoRun = { ...Store.demoRun, status: 'validating' };
  render();
  try {
    Store.demoRun = await liveDemoRequest(`/runs/${encodeURIComponent(runId)}/validate`, {
      method: 'POST', body: JSON.stringify({ confirmation })
    });
  } catch (error) {
    Store.demoError = error.message;
    Store.demoRun = await liveDemoRequest(`/runs/${encodeURIComponent(runId)}`).catch(() => Store.demoRun);
  }
  if (Store.route === 'live-demo') render();
}

function saveModels() {
  const g = id => document.getElementById(id).value.trim();
  Models.mode = g('m-mode');
  Models.vlm = { name: g('m-vlm-name') || Models.vlm.name, endpoint: g('m-vlm-ep') || Models.vlm.endpoint };
  Models.llm = { name: g('m-llm-name') || Models.llm.name, endpoint: g('m-llm-ep') || Models.llm.endpoint };
  for (const [k, o] of [['vlm', 'vlmOptions'], ['llm', 'llmOptions']]) if (!Models[o].includes(Models[k].name)) Models[o].push(Models[k].name);
  LS.set('models', { mode: Models.mode, vlm: Models.vlm, llm: Models.llm });
  toast('Model settings saved (' + Models.mode + ' mode)');
  render();
}

function renderSidebar() {
  return `
  <aside class="sidebar">
    <button class="sidebar__brand" onclick="Store.route='landing'; render();">
      <div class="sidebar__logo">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="8" r="6"/><path d="M8 5v3l2 2"/></svg>
      </div>
      <div>
        <div class="sidebar__brand-name">OmniSight</div>
      </div>
    </button>

    <button class="btn-new-chat" onclick="Store.route='landing'; render();">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3v10M3 8h10"/></svg>
      New Scan
    </button>

    <div class="sidebar__search-wrap">
      <svg class="sidebar__search-icon" width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="7" cy="7" r="4.5"/><path d="m11 11 3 3"/></svg>
      <input type="text" class="sidebar__search" placeholder="Search scans...  ( / )" aria-label="Search scans" oninput="filterScans(this.value)" />
    </div>

    <div class="nav-group">
      <button class="nav-item ${Store.route==='landing'?'nav-item--active':''}" onclick="Store.route='landing'; render();">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>
        Overview
      </button>
      <button class="nav-item ${Store.route==='live-demo'?'nav-item--active':''}" onclick="Store.route='live-demo'; render();">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="6"/><path d="M3.5 8h9M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/></svg>
        Live ShopLite Demo
      </button>
      <button class="nav-item ${Store.route==='scan'?'nav-item--active':''}" onclick="if(Store.scan){Store.route='scan'; render();}">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="6"/><path d="M8 2v4"/></svg>
        Active Scan
      </button>
      <button class="nav-item ${Store.route==='history'?'nav-item--active':''}" onclick="Store.route='history'; render();">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 13h12M4 10l3-3 2 2 3-5"/></svg>
        History
      </button>
      <button class="nav-item ${Store.route==='models'?'nav-item--active':''}" onclick="Store.route='models'; render();">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="2"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6 13 13M3 13l1.4-1.4M11.6 4.4 13 3"/></svg>
        Models
      </button>
    </div>

    <div class="sidebar__section-title">Recent Scans</div>
    <div class="recent-scans-list">
      ${recentScans().map(h => `
        <button class="recent-scan-btn truncate" onclick="startScan('${h.url}')">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="5"/></svg>
          <span class="truncate">${h.url.replace(/^https?:\/\//,'')}</span>
        </button>
      `).join('')}
    </div>

    <div class="sidebar__footer">
      <div class="sidebar__profile-pic">OS</div>
      <div>
        <div class="sidebar__user-name">OmniSight Engine</div>
        <div class="sidebar__user-sub">Autonomous UI Bot</div>
      </div>
    </div>
  </aside>
  `;
}

function renderLanding() {
  const opts = (list, cur) => list.map(o => `<option ${o === cur ? 'selected' : ''}>${esc(o)}</option>`).join('');
  return `
  <div class="landing-view">
    <div class="ai-orb-wrap"><div class="ai-orb"></div></div>
    <div class="landing-greeting">Hello, Developer</div>
    <h1 class="landing-heading">What should we inspect today?</h1>
    <div class="stat-row"><span><i></i>A vision model finds defects</span><span><i></i>A language model writes fixes</span><span><i></i>You review the PR</span></div>

    <div class="live-cta-card"><div><div class="live-cta-kicker">REAL BACKEND · FIXED SHOPLITE TARGET</div><h2>Run a live scan and verify a safe patch</h2><p>Capture the checkout, detect a visual defect with the configured model, and review the patch before sandbox approval.</p></div><button class="btn btn--primary" onclick="startLiveShopLiteRun()">Run live ShopLite scan <span aria-hidden="true">→</span></button></div>

    <div class="scan-composer">
      <div class="sim-label">SIMULATED WALKTHROUGH · no backend or AI requests are made</div>
      <input type="text" id="target-url-input" onkeydown="if(event.key==='Enter')startScan(this.value)" class="scan-composer__input" aria-label="Website URL" placeholder="Enter website URL to inspect... (e.g. https://shoplite.local)" value="https://shoplite.local" />
      <div class="scan-composer__bottom">
        <div class="scan-composer__pills">
          <button class="deep-scan-btn" aria-pressed="${Store.deep}" onclick="toggleDeep(this)">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 8a6 6 0 1 1 12 0A6 6 0 0 1 2 8Z"/><circle cx="8" cy="8" r="2"/></svg>
            Deep scan
          </button>
          <select id="sel-vlm" class="model-select" aria-label="Vision model (VLM)" title="VLM: reads screenshots">${opts(Models.vlmOptions, Models.vlm.name)}</select>
          <select id="sel-llm" class="model-select" aria-label="Language model (LLM)" title="LLM: reasons and writes patches">${opts(Models.llmOptions, Models.llm.name)}</select>
        </div>
        <button class="btn-scan-submit" aria-label="Start scan" onclick="startScan(document.getElementById('target-url-input').value)">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M8 12V4M4 8l4-4 4 4"/></svg>
        </button>
      </div>
    </div>

    <div class="quick-cards">
      <div class="q-card" onclick="startScan('https://shoplite.local')">
        <div class="q-card__icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 4h12M2 8h8M2 12h5"/></svg></div>
        <div class="q-card__title">Detect with VLM</div>
        <div class="q-card__desc">A vision model reads page screenshots and boxes clipped, overlapping and low-contrast elements.</div>
      </div>
      <div class="q-card" onclick="startScan('https://shoplite.local')">
        <div class="q-card__icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m3 11 10-10M9 1h4v4"/></svg></div>
        <div class="q-card__title">Reason and fix with LLM</div>
        <div class="q-card__desc">A language model traces each defect to its DOM and CSS cause, then writes a minimal patch.</div>
      </div>
      <div class="q-card" onclick="startScan('https://shoplite.local')">
        <div class="q-card__icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="6"/><path d="m6 8 1.5 1.5L10 6"/></svg></div>
        <div class="q-card__title">Verify with VLM</div>
        <div class="q-card__desc">The vision model compares before and after captures to confirm the fix and catch regressions.</div>
      </div>
    </div>
  </div>`;
}

const kindTag = k => k ? `<span class="tag tag--${k}">${k.toUpperCase()}</span>` : `<span class="tag tag--box">SANDBOX</span>`;

function renderPipeline(s) {
  return `<div class="pipeline-card" style="--p:${Math.min(100, Math.round((s.stage + (s.status === 'running' ? .5 : 0)) / STAGES.length * 100))}%">` + STAGES.map((st, i) => {
    const cls = s.status === 'failed' && i === s.stage ? 'failed' : i < s.stage ? 'completed' : i === s.stage ? 'running' : '';
    const badge = cls === 'completed' ? '✓' : cls === 'failed' ? '!' : i + 1;
    return (i ? '<div class="pipe-divider"></div>' : '') +
      `<div class="pipe-step ${cls ? 'pipe-step--' + cls : ''}" title="${esc(st.role)}"><div class="pipe-step__row"><div class="pipe-step__badge">${badge}</div><span>${st.label}</span></div>${kindTag(st.kind)}</div>`;
  }).join('') + `</div>`;
}

function renderTrace(s) {
  const keys = Object.keys(s.trace);
  const tab = Store.traceTab && s.trace[Store.traceTab] ? Store.traceTab : keys[keys.length - 1];
  const t = s.trace[tab];
  return `
  <div class="panel">
    <div class="panel__head"><div class="panel__title">Model trace</div><span class="panel__sub">Simulated walkthrough · use Live ShopLite Demo for backend results</span></div>
    <div class="trace-tabs" role="tablist">
      ${STAGES.map(st => `<button role="tab" class="trace-tab ${st.key === tab ? 'trace-tab--active' : ''}" ${s.trace[st.key] ? '' : 'disabled'} onclick="Store.traceTab='${st.key}'; render();">${st.label}</button>`).join('')}
    </div>
    <div class="trace-body">
      ${t ? `
        <div><div class="trace-label">${kindTag(t.kind)} ${esc(t.model)} <span>· ${t.ms} ms</span></div></div>
        <div><div class="trace-label">Prompt</div><pre class="code-block code-block--light">${esc(t.prompt)}</pre></div>
        <div><div class="trace-label">Output</div><pre class="code-block">${esc(t.output)}</pre></div>` : `<div class="empty">The VLM is analysing screenshots. Its prompt and output will appear here.</div>`}
    </div>
  </div>`;
}

function renderScore(s) {
  const W = { high: 20, medium: 10, low: 5 };
  const known = !!s.trace.detect, fixed = !!s.trace.verify;
  const before = Math.max(0, 100 - s.findings.reduce((a, f) => a + (W[f.severity] || 5), 0));
  const val = fixed ? 100 : before, R = 34, C = +(2 * Math.PI * R).toFixed(1);
  return `<div class="panel score-card">
    <div class="score-ring"><svg viewBox="0 0 80 80" aria-hidden="true"><circle class="ring-bg" cx="40" cy="40" r="${R}"/>${known ? `<circle class="ring-fg" cx="40" cy="40" r="${R}" style="--c:${C};--v:${(C * (1 - val / 100)).toFixed(1)}"/>` : ''}</svg><b>${known ? val : '–'}</b></div>
    <div><div class="model-row__name">UI health score</div><div class="panel__sub">${fixed ? 'Up from ' + before + ' once every finding is cleared' : known ? 'Before the fix. Penalty: high 20, medium 10, low 5.' : 'Waiting for the VLM'}</div></div>
  </div>`;
}

function renderDashboard() {
  const s = Store.scan;
  const shown = s.findings.filter(f => Store.sev === 'all' || f.severity === Store.sev);
  const active = shown.find(f => f.id === Store.activeFindingId) || shown[0] || s.findings[0];
  const statusLabel = s.status === 'completed' ? 'completed' : s.status === 'failed' ? 'failed' : STAGES[s.stage].label.toLowerCase() + '…';
  const done = s.trace.detect;
  return `
  <div class="dashboard-view">
    ${renderPipeline(s)}
    ${s.error ? `<div class="error-note">${esc(s.error)}. Check the endpoints under Models, or switch to simulated mode.</div>` : ''}
    <div class="dash-split">
      <div class="dash-col">
        ${renderScore(s)}
        <div class="panel">
          <div class="panel__head">
            <div class="panel__title">Findings (${s.findings.length})</div>
            <span class="badge badge--${s.status === 'failed' ? 'error' : s.status === 'completed' ? 'success' : 'info'}">${statusLabel}</span>
          </div>
          <div class="panel__body">
            <div class="chips">${['all', 'high', 'medium', 'low'].map(v => `<button class="chip ${Store.sev === v ? 'chip--on' : ''}" onclick="Store.sev='${v}'; render();">${v}</button>`).join('')}</div>
            ${shown.length ? shown.map(f => `
              <div class="finding-item sev-${f.severity} ${f.id === active.id ? 'finding-item--selected' : ''}" onclick="Store.activeFindingId='${f.id}'; render();">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                  <span class="badge badge--${f.severity}">${f.severity}</span>
                  <span class="muted mono" style="font-size:11px;">${f.confidence}</span>
                </div>
                <div class="finding-item__title">${esc(f.title)}</div>
                <div class="finding-item__meta"><span class="mono">${esc(f.selector)}</span></div>
              </div>`).join('') : `<div class="empty">${s.findings.length ? 'No findings at this severity.' : 'No findings yet. The VLM is still inspecting.'}</div>`}
          </div>
        </div>
        <div class="panel">
          <div class="panel__head"><div class="panel__title">Models</div><span class="panel__sub">${Models.mode}</span></div>
          <div class="panel__body">
            ${['vlm', 'llm'].map(k => `<div class="model-row"><div><div class="model-row__name">${esc(Models[k].name)}</div><div class="panel__sub">${k === 'vlm' ? 'Detect and verify' : 'Reason and fix'}</div></div>${kindTag(k)}</div>`).join('')}
          </div>
        </div>
      </div>

      <div class="dash-col">
        <div class="panel">
          <div class="panel__head"><div class="panel__title">${kindTag('vlm')} Visual perception</div><span class="panel__sub">${Store.deep ? '390 · 768 · 1440px' : '390px viewport'}</span></div>
          <div class="panel__body">
            <div class="vis-frame">
              ${!s.trace.detect && s.status === 'running' ? '<div class="scan-line"></div>' : ''}
              <img src="${Shots.checkoutBefore}" alt="Screenshot of the checkout page with detected defects outlined" />
              ${done ? s.findings.map(f => `<div class="bbox ${f.id === active.id ? 'bbox--active' : ''}" style="left:${f.box[0] / 3.9}%;top:${f.box[1] / 3.2}%;width:${f.box[2] / 3.9}%;height:${f.box[3] / 3.2}%"><span>${f.id}</span></div>`).join('') : ''}
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel__head"><div class="panel__title">${kindTag('llm')} Reasoning</div><span class="panel__sub">${esc(active.selector)}</span></div>
          <div class="panel__body">
            ${s.trace.reason ? `<div class="cause"><strong>Root cause.</strong> ${active.cause}</div>` : `<div class="empty">The LLM will explain the cause of each finding after detection.</div>`}
          </div>
        </div>

        <div class="panel">
          <div class="panel__head"><div class="panel__title">${kindTag('llm')} Generated patch</div><span style="display:flex;gap:10px;align-items:center;"><span class="mono muted" style="font-size:11.5px;">CheckoutButton.css</span><button class="btn" onclick="copyPatch()">Copy</button></span></div>
          <div class="panel__body" style="padding:0;">
            ${s.trace.fix ? `<div class="diff-container">${PATCH.split('\n').map(l => `<div class="diff-line ${l[0] === '+' ? 'diff-line--add' : l[0] === '-' ? 'diff-line--del' : ''}">${esc(l)}</div>`).join('')}</div>` : `<div class="panel__body"><div class="empty">The patch appears once reasoning finishes.</div></div>`}
          </div>
        </div>

        <div class="panel">
          <div class="panel__head"><div class="panel__title">${kindTag('vlm')} Before / after verification</div></div>
          <div class="panel__body">
            ${s.trace.verify ? `<div class="compare" style="--pos:50%">
              <img src="${Shots.checkoutAfter}" alt="After the fix" />
              <img class="compare__before" src="${Shots.checkoutBefore}" alt="Before the fix" />
              <span class="compare__tag compare__tag--l">Before</span><span class="compare__tag compare__tag--r">After</span>
              <div class="compare__handle"></div>
              <input type="range" min="0" max="100" value="50" aria-label="Drag to compare before and after" oninput="this.parentNode.style.setProperty('--pos', this.value + '%')" />
            </div>` : `<div class="empty">The VLM compares captures once the patch passes validation.</div>`}
          </div>
        </div>

        ${renderTrace(s)}
      </div>
    </div>
  </div>`;
}

function renderHistory() {
  const rows = Store.history;
  return `
  <div class="models-view">
    <div class="panel">
      <div class="panel__head"><div class="panel__title">Scan history</div><span class="panel__sub">${rows.length} scans</span></div>
      <div class="panel__body">
        ${rows.length ? rows.map(x => `
          <div class="hist-row">
            <div style="min-width:0;"><div class="model-row__name truncate">${esc(x.url)}</div>
              <div class="panel__sub">#${x.id} · ${x.at ? new Date(x.at).toLocaleString() : 'earlier'} · ${x.findings} findings, ${x.fixed} fixed${x.pr ? ' · PR #' + x.pr : ''}</div></div>
            <span class="badge badge--${x.status === 'completed' ? 'success' : 'error'}">${x.status}</span>
            <button class="btn" onclick="startScan('${esc(x.url)}')">Re-run</button>
          </div>`).join('') : '<div class="empty">No scans yet. Start one from Overview.</div>'}
      </div>
    </div>
  </div>`;
}

function renderModels() {
  const f = (id, label, val) => `<div class="field"><label for="${id}">${label}</label><input id="${id}" value="${esc(val)}" /></div>`;
  return `
  <div class="models-view">
    <div class="panel">
      <div class="panel__head"><div class="panel__title">Model settings</div></div>
      <div class="panel__body">
        <div class="field"><label for="m-mode">Walkthrough mode</label>
          <select id="m-mode"><option value="simulated" selected>Simulated sample output</option><option value="live" disabled>Live models are available in the fixed ShopLite demo</option></select></div>
        <div class="field-grid">${f('m-vlm-name', 'VLM name', Models.vlm.name)}${f('m-vlm-ep', 'VLM endpoint', Models.vlm.endpoint)}</div>
        <div class="field-grid">${f('m-llm-name', 'LLM name', Models.llm.name)}${f('m-llm-ep', 'LLM endpoint', Models.llm.endpoint)}</div>
        <button class="btn btn--primary" onclick="saveModels()">Save settings</button>
        <div class="error-note" style="margin-top:14px;">Live provider calls are server-side and configured in <code>backend/.env</code>. Use <b>Live ShopLite Demo</b> for the real Gemini, patch-safety, and sandbox workflow. API keys are never entered in this page.</div>
      </div>
    </div>
    <div class="panel">
      <div class="panel__head"><div class="panel__title">How a scan runs</div></div>
      <div class="panel__body">
        <ul class="proc-list">${STAGES.map(st => `<li>${kindTag(st.kind)}<span><b>${st.label}.</b> ${st.role}.</span></li>`).join('')}</ul>
      </div>
    </div>
    ${renderLocalGithubPanel()}
  </div>`;
}

function renderLocalGithubPanel() {
  const report = Store.githubValidation;
  const proposalId = Store.demoRun?.result?.proposal?.proposal_id || report?.proposal_id || '';
  const operation = Store.githubOperation;
  return `<div class="panel">
    <div class="panel__head"><div class="panel__title">Publish a draft pull request</div><span class="panel__sub">GitHub App · fixed repository · draft only</span></div>
    <div class="panel__body">
      <p class="panel__sub">The backend uses its configured GitHub App and repository. The local owner token stays in this page’s memory and is sent only to this local API.</p>
      <div class="field"><label for="gh-local-token">Local owner token</label><input id="gh-local-token" type="password" autocomplete="off" value="${esc(Store.githubToken || '')}" placeholder="Paste local API token" /></div>
      <p class="panel__sub">Proposal: <code>${esc(proposalId || 'Run and verify the ShopLite demo first')}</code></p>
      <div class="field-grid">
        <button class="btn" onclick="verifyLocalGithub()">Verify configured repository</button>
        <button class="btn" ${!proposalId || !Store.githubConnection ? 'disabled' : ''} onclick="validateLocalProposal()">Validate proposal at current base</button>
      </div>
      ${Store.githubError ? `<p class="error-note" role="alert">${esc(Store.githubError)}</p>` : ''}
      ${Store.githubConnection ? `<p class="success-note" role="status">Verified <b>${esc(Store.githubConnection.repository)}</b> · ${esc(Store.githubConnection.base_branch)} · base <code>${esc(Store.githubConnection.base_sha)}</code>.</p>` : ''}
      ${report ? `<section class="panel" style="margin-top:18px" aria-labelledby="validation-summary-heading">
        <div class="panel__head"><div id="validation-summary-heading" class="panel__title">Validation ${esc(report.status)}</div><span class="panel__sub">${esc(report.repository)}</span></div>
        <div class="panel__body">
          <p><b>Base:</b> <code>${esc(report.base_revision)}</code></p>
          <p><b>Patch SHA-256:</b> <code>${esc(report.diff_hash)}</code></p>
          <p><b>Report:</b> <code>${esc(report.report_id)}</code></p>
          <p><b>Sandbox checks:</b> ${esc(JSON.stringify(report.checks))}</p>
          ${report.status === 'verified' ? `<div class="field"><label for="gh-confirmation">Type <code>create-draft-pr</code> to confirm opening this draft PR</label><input id="gh-confirmation" autocomplete="off" placeholder="create-draft-pr" /></div>
            <button class="btn btn--primary" ${operation?.status === 'running' ? 'disabled' : ''} onclick="createLocalDraftPR()">${operation?.status === 'remote_outcome_unknown' ? 'Retry or reconcile draft PR' : 'Create draft pull request'}</button>` : '<p class="error-note">This report did not pass. Run validation again after correcting the proposal.</p>'}
        </div>
      </section>` : ''}
      ${operation ? `<div class="panel" role="status" aria-live="polite"><div class="panel__head"><div class="panel__title">Draft PR operation: ${esc(operation.status)}</div></div><div class="panel__body">${operation.pull_request_url ? `<p>Draft PR #${esc(operation.pull_request_number)} created successfully.</p><a href="${esc(operation.pull_request_url)}" target="_blank" rel="noopener noreferrer">Open draft pull request ↗</a>` : `<p>${esc(operation.safe_error_code || 'The operation is still being reconciled.')}</p>`}<p><code>${esc(operation.operation_id)}</code></p></div></div>` : ''}
      <p class="panel__sub">Publishing is opt-in. The backend requires the local auth token, a verified GitHub App installation, PostgreSQL, and sandbox settings. It creates a draft PR and does not merge or deploy.</p>
    </div>
  </div>`;
}

function localGithubHeaders() {
  const token = document.getElementById('gh-local-token')?.value.trim() || Store.githubToken;
  if (!token) throw new Error('Enter the local owner token.');
  Store.githubToken = token;
  return { 'Content-Type': 'application/json', 'X-OmniSight-Local-Token': token };
}

async function localGithubRequest(path, options = {}) {
  if (window.location.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(window.location.hostname) || window.location.port !== '8000') {
    throw new Error('Open the dashboard at http://127.0.0.1:8000/ to use local GitHub publishing.');
  }
  const response = await fetch(`${window.location.origin}${path}`, { ...options, headers: { ...localGithubHeaders(), ...(options.headers || {}) } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || body.detail || `Backend returned HTTP ${response.status}`);
  return body;
}

async function verifyLocalGithub() {
  Store.githubError = null;
  try {
    Store.githubConnection = await localGithubRequest('/api/v1/local/github/connection/verify', { method: 'POST', body: '{}' });
    Store.githubValidation = null; Store.githubOperation = null; Store.githubIdempotencyKey = null;
  } catch (error) { Store.githubError = error.message; }
  render();
}

async function validateLocalProposal() {
  const proposalId = Store.demoRun?.result?.proposal?.proposal_id || Store.githubValidation?.proposal_id;
  if (!proposalId) { Store.githubError = 'Run and verify the ShopLite demo first.'; render(); return; }
  Store.githubError = null;
  Store.githubValidation = null; Store.githubIdempotencyKey = null; Store.githubOperation = null;
  try {
    Store.githubValidation = await localGithubRequest(`/api/v1/local/shoplite/proposals/${encodeURIComponent(proposalId)}/validate`, { method: 'POST', body: '{}' });
    Store.githubIdempotencyKey = crypto.randomUUID();
    Store.githubOperation = null;
  } catch (error) { Store.githubError = error.message; }
  render();
}

async function createLocalDraftPR() {
  const confirmation = document.getElementById('gh-confirmation')?.value.trim();
  if (confirmation !== 'create-draft-pr') { Store.githubError = 'Type create-draft-pr to confirm.'; render(); return; }
  if (!Store.githubValidation || !Store.githubIdempotencyKey) { Store.githubError = 'Validate the proposal first.'; render(); return; }
  if (!window.confirm(`Create a draft pull request for ${Store.githubValidation.repository}? OmniSight will not merge or deploy it.`)) return;
  Store.githubError = null;
  try {
    Store.githubOperation = await localGithubRequest('/api/v1/local/github/draft-prs', {
      method: 'POST', headers: { 'Idempotency-Key': Store.githubIdempotencyKey },
      body: JSON.stringify({ validation_report_id: Store.githubValidation.report_id, confirmation })
    });
  } catch (error) { Store.githubError = error.message; }
  render();
}

function render() {
  const root = document.getElementById('app');
  const crumb = Store.route === 'scan' ? esc(Store.scan.url) : Store.route === 'models' ? 'Models' : Store.route === 'history' ? 'History' : Store.route === 'live-demo' ? 'Live ShopLite Demo' : 'Overview';
  const canExport = (Store.route === 'scan' && Store.scan.status === 'completed') || (Store.route === 'live-demo' && Store.demoRun?.status === 'verified');
  const canPublish = Store.route === 'live-demo' && Store.demoRun?.status === 'verified';
  root.innerHTML = `
    <div class="app-shell">
      ${renderSidebar()}
      <div class="workspace">
        <header class="topbar">
          <div class="topbar__title">
            <span>OmniSight</span><span class="muted">/</span>
            <span style="font-weight:400; color:var(--text-2);">${crumb}</span>
          </div>
          <div class="topbar__actions">
            <button class="btn" onclick="toggleTheme()" aria-label="Toggle dark mode">${document.documentElement.dataset.theme === 'dark' ? '☀ Light' : '☾ Dark'}</button>
            ${canExport ? `<button class="btn" onclick="${Store.route === 'live-demo' ? 'downloadLiveReport()' : 'downloadReport()'}">Export report</button>` : ''}
            ${canPublish ? `<button class="btn btn--primary" onclick="Store.route='models';render();document.querySelector('.panel__title')?.scrollIntoView({behavior:'smooth'})">Open GitHub publishing</button>` : ''}
          </div>
        </header>
        <div class="main-content">
          ${Store.route === 'landing' ? renderLanding() : Store.route === 'models' ? renderModels() : Store.route === 'history' ? renderHistory() : Store.route === 'live-demo' ? renderLiveDemo() : renderDashboard()}
        </div>
      </div>
    </div>`;
}

document.addEventListener('DOMContentLoaded', render);
render();

/* ---------------------------------------------------------------------------
   Keyboard shortcuts (ignored while typing in a field)
   /  focus search    n  new scan    h  history    d  toggle dark mode
   Esc  leave the field you are typing in
   ------------------------------------------------------------------------- */
document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (typing) { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === '/') { e.preventDefault(); const q = document.querySelector('.sidebar__search'); if (q) q.focus(); }
  else if (e.key === 'n') { Store.route = 'landing'; render(); const u = document.getElementById('target-url-input'); if (u) u.focus(); }
  else if (e.key === 'h') { Store.route = 'history'; render(); }
  else if (e.key === 'd') toggleTheme();
});

