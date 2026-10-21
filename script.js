/* OmniSight front-end: persisted ShopLite workflows, review actions, and rendering. */
/* ============================================================================
   OmniSight — Local ShopLite QA Workflow
   ========================================================================== */
'use strict';

const Store = {
  route: 'landing', // 'landing' | 'models' | 'live-demo' | 'history'
  demoRunId: null,
  demoRun: null,
  demoError: null,
  patchPreview: null,
  patchPreviewError: null,
  demoRuns: [],
  demoHistoryError: null,
  githubToken: null,
  githubConnection: null,
  githubValidation: null,
  githubIdempotencyKey: null,
  githubOperation: null,
  githubError: null
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

const esc = s => String(s).replace(/[&<>\"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '\"': '&quot;' }[c]));

const recentScans = () => Store.demoRuns.slice(0, 5);
function filterScans(q) {
  q = q.trim().toLowerCase();
  document.querySelectorAll('.recent-scan-btn, .hist-row').forEach(el => { el.hidden = !!q && !el.textContent.toLowerCase().includes(q); });
}

function renderLiveDemo() {
  const run = Store.demoRun;
  const result = run?.result || {};
  const capture = result.capture || {};
  const findings = Array.isArray(result.findings) ? result.findings : [];
  const proposal = result.proposal || null;
  const patchPreview = Store.patchPreview?.proposal_id === proposal?.proposal_id ? Store.patchPreview : null;
  const sandbox = result.sandbox || null;
  const checks = sandbox?.checks || {};
  const detectionIssue = result.detection_reason_message;
  const finishedStages = {
    queued: 0, running: 0, awaiting_sandbox_approval: 3, proposal_rejected: 3, validating: 3, completed: 3,
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
    patch_rejected: 'Patch rejected by safety checks', proposal_rejected: 'Proposal rejected by reviewer', needs_inspection: 'Needs human inspection',
    validation_failed: 'Sandbox checks did not pass', completed: 'Completed', failed: 'Run failed'
  }[run?.status] || 'Ready to run';
  const viewport = capture.viewports?.mobile;
  const overflowBefore = checks.baseline_mobile_overflow_px ?? viewport?.button_overflow_px;
  const overflowAfter = checks.patched_mobile_overflow_px;
  const canApprove = run?.status === 'awaiting_sandbox_approval' && proposal?.status === 'proposed' && !!patchPreview;
  const canReject = canApprove;
  return `<div class="live-demo-view">
    <div class="panel live-demo-intro">
      <div><div class="eyebrow">Connected backend workflow</div><h1>ShopLite live demo</h1>
      <p>Run the fixed, local checkout through Playwright capture → Gemini detection → safe patch proposal. Sandbox validation waits for your explicit approval.</p></div>
      <button class="btn btn--primary" ${['queued','running','awaiting_sandbox_approval','validating'].includes(run?.status) ? 'disabled' : ''} onclick="startLiveShopLiteRun()">${run ? 'Run again' : 'Start real scan'}</button>
    </div>
    ${Store.demoError ? `<div class="error-note" role="alert">${esc(Store.demoError)}</div>` : ''}
    ${detectionIssue ? `<div class="error-note" role="alert"><strong>AI analysis did not complete:</strong> ${esc(detectionIssue)}${result.detection_reason_code === 'rate_limited' ? ' Check the Gemini project quota in Google AI Studio, wait for the limit to reset, or configure a model enabled for this API key.' : ''}</div>` : ''}
    ${run ? `<div class="panel live-run-status"><div><span class="badge badge--${run.status === 'verified' ? 'success' : run.status === 'failed' || run.status === 'validation_failed' || run.status === 'patch_rejected' || run.status === 'proposal_rejected' ? 'error' : 'info'}">${esc(stateLabel)}</span><span class="panel__sub">Run ${esc(run.run_id)}</span></div>${run.error ? `<p class="error-note">${esc(run.error)}</p>` : ''}</div>
      <div class="live-stage-grid">${stages.map(({label, done, current}) => `<div class="live-stage ${done ? 'live-stage--done' : current ? 'live-stage--current' : ''}"><span>${done ? '✓' : current ? '…' : '·'}</span>${esc(label)}</div>`).join('')}</div>
      ${capture.screenshot_artifact_id ? `<div class="panel live-evidence-shot"><div class="panel__head"><div class="panel__title">Persisted mobile screenshot evidence</div><span class="panel__sub">${esc(viewport?.button_overflow_px ?? '—')}px horizontal overflow</span></div><div class="panel__body"><img src="/api/v1/artifacts/${encodeURIComponent(capture.screenshot_artifact_id)}" alt="Captured ShopLite checkout at the mobile viewport" loading="lazy" /><p class="panel__sub">Artifact ${esc(capture.screenshot_artifact_id)} · ${esc(viewport?.viewport?.width ?? 390)} × ${esc(viewport?.viewport?.height ?? 844)}</p></div></div>` : ''}
      ${findings.length ? `<div class="panel"><div class="panel__head"><div class="panel__title">Validated visual findings (${findings.length})</div><span class="panel__sub">${result.detection_is_mock ? 'MOCK adapter' : esc(result.detection_adapter || 'Gemini')}</span></div><div class="panel__body">${findings.map(f => `<article class="live-finding"><div><span class="badge badge--high">${esc(f.category || 'visual issue')}</span><span class="mono muted">${Math.round((f.confidence || 0) * 100)}% confidence</span></div><h3>${esc(f.category === 'clipped_element' ? 'Checkout button clipped on mobile' : f.category || 'Visual issue')}</h3><p>Target: <code>${esc(f.target_candidate || 'Needs inspection')}</code></p><p>Capture: mobile ${esc(viewport?.button_overflow_px ?? '—')}px overflow · desktop ${esc(capture.viewports?.desktop?.button_overflow_px ?? '—')}px</p></article>`).join('')}</div></div>` : ''}
      ${proposal ? `<div class="panel"><div class="panel__head"><div class="panel__title">Patch proposal</div><span class="badge badge--${proposal.status === 'proposed' ? 'success' : 'error'}">${esc(proposal.status)}</span></div><div class="panel__body">${(proposal.files || []).map(file => `<div class="live-file"><code>${esc(file.path)}</code><span>${esc(file.edits)} edit${file.edits === 1 ? '' : 's'} · ${esc(file.bytes)} bytes</span></div>`).join('')}${proposal.rejection_reasons?.length ? `<p class="error-note">Review: ${proposal.rejection_reasons.map(esc).join(', ')}${result.proposal_review?.reason ? ` · ${esc(result.proposal_review.reason)}` : ''}</p>` : ''}<p class="panel__sub">Generator: ${esc(proposal.generator_label || 'unknown')}${proposal.is_mock ? ' · simulated provider output' : ' · live provider response'}</p>${run?.status === 'awaiting_sandbox_approval' ? patchPreview ? `<section class="patch-preview"><div class="patch-preview__meta"><strong>Review exact source diff</strong><span>Base ${esc(patchPreview.base_sha256.slice(0, 16))}…</span></div><p class="panel__sub">This bounded diff is what the sandbox will validate. Approval is rejected if the source changes.</p><pre aria-label="Proposed unified CSS diff"><code>${esc(patchPreview.diff)}</code></pre></section>` : `<div class="panel__sub" role="status">${Store.patchPreviewError ? `<span class="error-note">${esc(Store.patchPreviewError)}</span> <button class="btn btn--ghost" onclick="loadShopLitePatchPreview('${esc(run.run_id)}')">Reload diff</button>` : 'Loading the exact source diff for review…'}</div>` : ''}</div></div>` : ''}
      ${canApprove ? `<div class="panel live-approval"><div class="panel__head"><div class="panel__title">Review before sandbox validation</div><span class="panel__sub">Fixed ShopLite demo only</span></div><div class="panel__body"><p>This runs the proposed CSS edit in a disposable container with network access disabled. It does not change the source checkout.</p><div class="field"><label for="live-demo-confirm">Type <code>approve-demo-only</code> to approve this sandbox run</label><input id="live-demo-confirm" autocomplete="off" placeholder="approve-demo-only"></div><button class="btn btn--primary" onclick="validateLiveShopLiteRun()">Approve and validate</button>${canReject ? `<div class="field live-rejection-field"><label for="live-demo-reject-reason">Or reject the proposal with a reason</label><textarea id="live-demo-reject-reason" maxlength="500" rows="2" placeholder="Explain what needs to change"></textarea></div><button class="btn btn--ghost" onclick="rejectLiveShopLiteProposal()">Reject proposal</button>` : ''}</div></div>` : ''}
      ${sandbox ? `<div class="panel"><div class="panel__head"><div class="panel__title">Sandbox verification</div><span class="badge badge--${sandbox.status === 'verified' ? 'success' : 'error'}">${esc(sandbox.status)}</span></div><div class="panel__body"><div class="live-metrics"><div><span>Mobile overflow</span><strong>${esc(checks.baseline_mobile_overflow_px)}px → ${esc(checks.patched_mobile_overflow_px)}px</strong></div><div><span>Desktop overflow</span><strong>${esc(checks.patched_desktop_overflow_px)}px</strong></div><div><span>Browser errors</span><strong>${esc(checks.browser_error_count)}</strong></div><div><span>External requests blocked</span><strong>${esc(checks.blocked_external_request_count)}</strong></div></div>${sandbox.baseline_screenshot_artifact_id && sandbox.candidate_screenshot_artifact_id ? `<div class="sandbox-evidence-grid"><figure><figcaption>Before · mobile</figcaption><img src="/api/v1/artifacts/${encodeURIComponent(sandbox.baseline_screenshot_artifact_id)}" alt="ShopLite before the proposed fix" loading="lazy" /></figure><figure><figcaption>After · mobile</figcaption><img src="/api/v1/artifacts/${encodeURIComponent(sandbox.candidate_screenshot_artifact_id)}" alt="ShopLite after the proposed fix" loading="lazy" /></figure></div><p class="panel__sub">Both screenshots are saved as evidence for this run.</p>` : ''}</div></div>` : ''}
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

function isLocalDashboard() {
  return window.location.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(window.location.hostname) && window.location.port === '8000';
}

function rememberDemoRun(run) {
  if (!run?.run_id) return;
  Store.demoRuns = [run, ...Store.demoRuns.filter(item => item.run_id !== run.run_id)]
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
}

async function loadDemoRuns() {
  if (!isLocalDashboard()) return;
  try {
    Store.demoRuns = await liveDemoRequest('/runs');
    Store.demoHistoryError = null;
    if (Store.route === 'history') render();
  } catch (error) {
    Store.demoHistoryError = error.message;
    if (Store.route === 'history') render();
  }
}

function openDemoRun(runId) {
  Store.route = 'live-demo';
  Store.demoError = null;
  Store.demoRunId = runId;
  Store.patchPreview = null;
  Store.patchPreviewError = null;
  render();
  pollLiveShopLiteRun(runId);
}

function openDemoHistory() {
  Store.route = 'history';
  render();
  loadDemoRuns();
}

async function startLiveShopLiteRun() {
  Store.route = 'live-demo'; Store.demoRun = null; Store.demoError = null;
  Store.patchPreview = null; Store.patchPreviewError = null;
  Store.githubValidation = null; Store.githubIdempotencyKey = null; Store.githubOperation = null; Store.githubError = null;
  render();
  try {
    Store.demoRun = await liveDemoRequest('/runs', { method: 'POST', body: '{}' });
    Store.demoRunId = Store.demoRun.run_id;
    rememberDemoRun(Store.demoRun);
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
    rememberDemoRun(run);
    if (Store.route === 'live-demo') render();
    if (run.status === 'awaiting_sandbox_approval' && Store.patchPreview?.run_id !== runId && !Store.patchPreviewError) {
      loadShopLitePatchPreview(runId);
    }
    if (['queued', 'running', 'validating'].includes(run.status)) {
      setTimeout(() => pollLiveShopLiteRun(runId), 1200);
    }
  } catch (error) {
    Store.demoError = error.message;
    if (Store.route === 'live-demo') render();
  }
}

async function loadShopLitePatchPreview(runId) {
  Store.patchPreview = null;
  Store.patchPreviewError = null;
  if (Store.route === 'live-demo') render();
  try {
    const preview = await liveDemoRequest(`/runs/${encodeURIComponent(runId)}/patch-preview`);
    Store.patchPreview = { ...preview, run_id: runId };
  } catch (error) {
    Store.patchPreviewError = error.message;
  }
  if (Store.route === 'live-demo' && Store.demoRunId === runId) render();
}

async function validateLiveShopLiteRun() {
  const confirmation = document.getElementById('live-demo-confirm')?.value.trim();
  if (confirmation !== 'approve-demo-only') { toast('Type approve-demo-only to approve this run'); return; }
  if (!Store.patchPreview || Store.patchPreview.run_id !== Store.demoRunId) { toast('Load and review the patch diff before approval'); return; }
  if (!window.confirm('Run this reviewed CSS proposal in the isolated, network-disabled ShopLite sandbox?')) return;
  const runId = Store.demoRunId;
  Store.demoRun = { ...Store.demoRun, status: 'validating' };
  render();
  try {
    Store.demoRun = await liveDemoRequest(`/runs/${encodeURIComponent(runId)}/validate`, {
      method: 'POST', body: JSON.stringify({ confirmation, base_sha256: Store.patchPreview.base_sha256 })
    });
  } catch (error) {
    Store.demoError = error.message;
    Store.demoRun = await liveDemoRequest(`/runs/${encodeURIComponent(runId)}`).catch(() => Store.demoRun);
  }
  if (Store.route === 'live-demo') render();
}

async function rejectLiveShopLiteProposal() {
  const reason = document.getElementById('live-demo-reject-reason')?.value.trim();
  if (!reason) { toast('Add a reason for rejecting this proposal'); return; }
  if (!Store.patchPreview || Store.patchPreview.run_id !== Store.demoRunId) { toast('Load and review the patch diff before deciding'); return; }
  if (!window.confirm('Reject this proposal? It will no longer be eligible for sandbox validation or draft PR publishing.')) return;
  const runId = Store.demoRunId;
  try {
    Store.demoRun = await liveDemoRequest(`/runs/${encodeURIComponent(runId)}/reject-proposal`, {
      method: 'POST', body: JSON.stringify({ reason })
    });
    Store.demoError = null;
    Store.patchPreview = null;
    rememberDemoRun(Store.demoRun);
  } catch (error) {
    Store.demoError = error.message;
    Store.demoRun = await liveDemoRequest(`/runs/${encodeURIComponent(runId)}`).catch(() => Store.demoRun);
  }
  if (Store.route === 'live-demo') render();
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
      <button class="nav-item ${Store.route==='history'?'nav-item--active':''}" onclick="openDemoHistory()">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 13h12M4 10l3-3 2 2 3-5"/></svg>
        History
      </button>
      <button class="nav-item ${Store.route==='models'?'nav-item--active':''}" onclick="Store.route='models'; render();">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="2"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6 13 13M3 13l1.4-1.4M11.6 4.4 13 3"/></svg>
        Settings
      </button>
    </div>

    <div class="sidebar__section-title">Recent Scans</div>
    <div class="recent-scans-list">
      ${recentScans().map(h => `
        <button class="recent-scan-btn truncate" onclick="openDemoRun('${esc(h.run_id)}')">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="5"/></svg>
          <span class="truncate">ShopLite · ${esc(h.status)}</span>
        </button>
      `).join('')}
      ${Store.demoRuns.length ? '' : '<span class="panel__sub">No real runs yet</span>'}
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
  return `
  <div class="landing-view">
    <div class="ai-orb-wrap"><div class="ai-orb"></div></div>
    <div class="landing-greeting">Hello, Developer</div>
    <h1 class="landing-heading">Run a real visual QA workflow</h1>
    <div class="stat-row"><span><i></i>Playwright captures the fixed checkout</span><span><i></i>Configured AI analyzes and proposes a patch</span><span><i></i>You review before sandbox validation</span></div>

    <div class="live-cta-card"><div><div class="live-cta-kicker">CONNECTED BACKEND · FIXED SHOPLITE TARGET</div><h2>Inspect, review, and verify a checkout fix</h2><p>OmniSight captures the local ShopLite checkout, records evidence and findings, then prepares a safety-checked patch. The Docker sandbox runs only after your explicit approval.</p></div><button class="btn btn--primary" onclick="startLiveShopLiteRun()">Start ShopLite scan <span aria-hidden="true">→</span></button></div>

    <div class="quick-cards">
      <article class="q-card"><div class="q-card__title">1 · Capture evidence</div><div class="q-card__desc">Playwright checks the mobile and desktop checkout and stores observation metadata with screenshot integrity hashes.</div></article>
      <article class="q-card"><div class="q-card__title">2 · Review the proposal</div><div class="q-card__desc">The configured vision and patch providers produce validated findings and a restricted CSS proposal.</div></article>
      <article class="q-card"><div class="q-card__title">3 · Verify safely</div><div class="q-card__desc">After your approval, a disposable network-disabled container checks the proposed fix.</div></article>
    </div>
  </div>`;
}

function renderHistory() {
  const rows = Store.demoRuns;
  return `
  <div class="models-view">
    <div class="panel">
      <div class="panel__head"><div class="panel__title">ShopLite run history</div><span class="panel__sub">${rows.length} backend ${rows.length === 1 ? 'run' : 'runs'}</span></div>
      <div class="panel__body">
        ${Store.demoHistoryError ? `<p class="error-note" role="alert">${esc(Store.demoHistoryError)}</p>` : ''}
        ${rows.length ? rows.map(x => {
          const findings = x.result?.findings?.length || 0;
          const sandboxStatus = x.result?.sandbox?.status;
          const badgeType = x.status === 'verified' ? 'success' : ['failed','validation_failed','patch_rejected'].includes(x.status) ? 'error' : 'info';
          return `
          <div class="hist-row">
            <div style="min-width:0;"><div class="model-row__name truncate">ShopLite checkout</div>
              <div class="panel__sub">${esc(x.run_id)} · ${new Date(x.created_at).toLocaleString()} · ${findings} findings${sandboxStatus ? ` · sandbox ${esc(sandboxStatus)}` : ''}</div></div>
            <span class="badge badge--${badgeType}">${esc(x.status)}</span>
            <button class="btn" onclick="openDemoRun('${esc(x.run_id)}')">View run</button>
          </div>`;
        }).join('') : '<div class="empty">No backend ShopLite runs yet. Start one from Live ShopLite Demo.</div>'}
      </div>
    </div>
  </div>`;
}

function renderModels() {
  const result = Store.demoRun?.result || {};
  return `
  <div class="models-view">
    <div class="panel">
      <div class="panel__head"><div class="panel__title">Backend workflow configuration</div><span class="panel__sub">Credentials stay server-side</span></div>
      <div class="panel__body">
        <p>OmniSight uses the providers configured in <code>backend/.env</code>. This dashboard does not accept model names or API keys.</p>
        ${Store.demoRun ? `<div class="live-metrics"><div><span>Vision analysis</span><strong>${esc(result.detection_adapter || 'Not reported')}${result.detection_is_mock ? ' · mock' : ''}</strong></div><div><span>Patch generator</span><strong>${esc(result.proposal?.generator_label || 'Not reported')}${result.proposal?.is_mock ? ' · mock' : ''}</strong></div><div><span>Run status</span><strong>${esc(Store.demoRun.status)}</strong></div></div>` : '<p class="panel__sub">Run the ShopLite workflow to see provider and status information returned by the backend.</p>'}
        <button class="btn btn--primary" onclick="Store.route='live-demo'; render();">Open live ShopLite workflow</button>
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
  const crumb = Store.route === 'models' ? 'Settings' : Store.route === 'history' ? 'History' : Store.route === 'live-demo' ? 'Live ShopLite Demo' : 'Overview';
  const canExport = Store.route === 'live-demo' && Store.demoRun?.status === 'verified';
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
            ${canExport ? `<button class="btn" onclick="downloadLiveReport()">Export report</button>` : ''}
            ${canPublish ? `<button class="btn btn--primary" onclick="Store.route='models';render();document.querySelector('.panel__title')?.scrollIntoView({behavior:'smooth'})">Open GitHub publishing</button>` : ''}
          </div>
        </header>
        <div class="main-content">
          ${Store.route === 'models' ? renderModels() : Store.route === 'history' ? renderHistory() : Store.route === 'live-demo' ? renderLiveDemo() : renderLanding()}
        </div>
      </div>
    </div>`;
}

render();
loadDemoRuns();

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
  else if (e.key === 'n') { Store.route = 'landing'; render(); document.querySelector('.live-cta-card .btn')?.focus(); }
  else if (e.key === 'h') openDemoHistory();
  else if (e.key === 'd') toggleTheme();
});

