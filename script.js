/* OmniSight front-end: persisted ShopLite workflows, review actions, and rendering. */
/* ============================================================================
   OmniSight — Local ShopLite QA Workflow
   ========================================================================== */
'use strict';

const Store = {
  route: 'landing', // 'landing' | 'projects' | 'models' | 'live-demo' | 'history'
  demoRunId: null,
  demoRun: null,
  demoError: null,
  patchPreview: null,
  patchPreviewError: null,
  demoRuns: [],
  demoHistoryError: null,
  projects: [],
  projectsError: null,
  projectsLoading: false,
  projectsRequestId: 0,
  projectSaving: false,
  projectFormName: '',
  projectFormTargetUrl: '',
  projectFormEnvironment: 'staging',
  projectFormResetPending: false,
  selectedProjectId: null,
  targetAuthorizationEvents: [],
  targetAuthorizationEventsRequestId: 0,
  workspace: null,
  workspaceError: null,
  workspaceLoading: false,
  workspaceSaving: false,
  workspaceSaved: false,
  githubToken: null,
  githubConnection: null,
  githubValidation: null,
  githubIdempotencyKey: null,
  githubOperation: null,
  githubOperations: [],
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

async function localApiRequest(path, options = {}) {
  if (window.location.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(window.location.hostname) || window.location.port !== '8000') {
    throw new Error('Open the dashboard at http://127.0.0.1:8000/ to use the local OmniSight backend.');
  }
  const response = await fetch(`${window.location.origin}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || `Backend returned HTTP ${response.status}`);
  return body;
}

async function liveDemoRequest(path, options = {}) {
  return localApiRequest(`/api/v1/demo/shoplite${path}`, options);
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

function openSettings() {
  Store.route = 'models';
  render();
  loadShopLiteWorkspace();
}

function openProjects(projectId = null) {
  Store.route = 'projects';
  if (Store.selectedProjectId !== projectId) {
    Store.targetAuthorizationEvents = [];
    Store.targetAuthorizationEventsRequestId += 1;
  }
  Store.selectedProjectId = projectId;
  Store.projectsError = null;
  render();
  loadProjects();
  if (projectId) loadTargetAuthorizationEvents(projectId);
}

async function loadProjects() {
  const requestId = ++Store.projectsRequestId;
  Store.projectsLoading = true;
  Store.projectsError = null;
  render();
  try {
    const projects = await localApiRequest('/api/v1/projects');
    if (requestId !== Store.projectsRequestId) return;
    Store.projects = projects;
    if (Store.selectedProjectId && !Store.projects.some(project => project.id === Store.selectedProjectId)) {
      Store.selectedProjectId = null;
    }
  } catch (error) {
    if (requestId !== Store.projectsRequestId) return;
    Store.projectsError = error.message;
  } finally {
    if (requestId === Store.projectsRequestId) {
      Store.projectsLoading = false;
      if (Store.route === 'projects') render();
    }
  }
}

async function createProject(event) {
  event?.preventDefault();
  if (Store.projectSaving) return;
  const nameInput = document.getElementById('project-name');
  const targetUrlInput = document.getElementById('project-target-url');
  const environmentInput = document.getElementById('project-environment-kind');
  if (nameInput) Store.projectFormName = nameInput.value;
  if (targetUrlInput) Store.projectFormTargetUrl = targetUrlInput.value;
  if (environmentInput) Store.projectFormEnvironment = environmentInput.value;
  const name = Store.projectFormName.trim();
  const targetUrl = Store.projectFormTargetUrl.trim();
  const environmentKind = Store.projectFormEnvironment;
  Store.projectSaving = true;
  Store.projectsError = null;
  render();
  try {
    const project = await localApiRequest('/api/v1/projects', {
      method: 'POST',
      body: JSON.stringify({ name, target_url: targetUrl, environment_kind: environmentKind })
    });
    // Ignore any list request that started before this successful create.
    Store.projectsRequestId += 1;
    Store.projectsLoading = false;
    Store.projects = [project, ...Store.projects.filter(item => item.id !== project.id)];
    Store.selectedProjectId = project.id;
    Store.projectFormName = '';
    Store.projectFormTargetUrl = '';
    Store.projectFormEnvironment = 'staging';
    Store.projectFormResetPending = true;
  } catch (error) {
    Store.projectsError = error.message;
  } finally {
    Store.projectSaving = false;
    if (Store.route === 'projects') render();
  }
}

function updateProjectForm(field, value) {
  const formFields = {
    name: 'projectFormName',
    target_url: 'projectFormTargetUrl',
    environment_kind: 'projectFormEnvironment'
  };
  const stateField = formFields[field];
  if (stateField) Store[stateField] = value;
}

async function loadTargetAuthorizationEvents(projectId) {
  const requestId = ++Store.targetAuthorizationEventsRequestId;
  const project = Store.projects.find(item => item.id === projectId);
  const target = project?.targets?.[0];
  if (!target) {
    Store.targetAuthorizationEvents = [];
    return;
  }
  try {
    const events = await localApiRequest(
      `/api/v1/projects/${encodeURIComponent(projectId)}/targets/${encodeURIComponent(target.id)}/authorization-events`
    );
    if (requestId === Store.targetAuthorizationEventsRequestId) Store.targetAuthorizationEvents = events;
  } catch {
    if (requestId === Store.targetAuthorizationEventsRequestId) Store.targetAuthorizationEvents = [];
  }
  if (requestId === Store.targetAuthorizationEventsRequestId && Store.route === 'projects' && Store.selectedProjectId === projectId) render();
}

async function authorizeProjectTarget(event, projectId, targetId) {
  event?.preventDefault();
  if (Store.projectSaving) return;
  const checkbox = document.getElementById(`authorize-target-${targetId}`);
  if (!checkbox?.checked) {
    Store.projectsError = 'Confirm that you own this staging target or have permission to test it.';
    render();
    return;
  }
  Store.projectSaving = true;
  Store.projectsError = null;
  render();
  try {
    const project = await localApiRequest(
      `/api/v1/projects/${encodeURIComponent(projectId)}/targets/${encodeURIComponent(targetId)}/authorize`,
      { method: 'POST', body: JSON.stringify({ confirm_authorization: true }) }
    );
    Store.projects = Store.projects.map(item => item.id === project.id ? project : item);
    Store.selectedProjectId = project.id;
    await loadTargetAuthorizationEvents(project.id);
  } catch (error) {
    Store.projectsError = error.message;
  } finally {
    Store.projectSaving = false;
    if (Store.route === 'projects') render();
  }
}

async function revokeProjectTarget(projectId, targetId) {
  if (Store.projectSaving) return;
  Store.projectSaving = true;
  Store.projectsError = null;
  render();
  try {
    const project = await localApiRequest(
      `/api/v1/projects/${encodeURIComponent(projectId)}/targets/${encodeURIComponent(targetId)}/revoke`,
      { method: 'POST', body: '{}' }
    );
    Store.projects = Store.projects.map(item => item.id === project.id ? project : item);
    Store.selectedProjectId = project.id;
    await loadTargetAuthorizationEvents(project.id);
  } catch (error) {
    Store.projectsError = error.message;
  } finally {
    Store.projectSaving = false;
    if (Store.route === 'projects') render();
  }
}

async function loadShopLiteWorkspace() {
  if (!isLocalDashboard()) return;
  Store.workspaceLoading = true;
  Store.workspaceError = null;
  render();
  try {
    Store.workspace = await liveDemoRequest('/workspace');
  } catch (error) {
    Store.workspaceError = error.message;
  } finally {
    Store.workspaceLoading = false;
    if (Store.route === 'models') render();
  }
}

async function publishShopLiteFlowVersion() {
  const flow = Store.workspace?.test_flow;
  if (!flow || Store.workspaceSaving) return;
  const viewports = [
    { width: Number(document.getElementById('flow-mobile-width')?.value), height: Number(document.getElementById('flow-mobile-height')?.value) },
    { width: Number(document.getElementById('flow-desktop-width')?.value), height: Number(document.getElementById('flow-desktop-height')?.value) }
  ];
  Store.workspaceSaving = true;
  Store.workspaceSaved = false;
  Store.workspaceError = null;
  render();
  try {
    Store.workspace = await liveDemoRequest('/workspace/test-flow', {
      method: 'PUT',
      body: JSON.stringify({ expected_version: flow.version, viewports })
    });
    Store.workspaceSaved = true;
  } catch (error) {
    Store.workspaceError = error.message;
  } finally {
    Store.workspaceSaving = false;
    if (Store.route === 'models') render();
  }
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
      <button class="nav-item ${Store.route==='projects'?'nav-item--active':''}" onclick="openProjects()">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="2" y="2" width="12" height="12" rx="2"/><path d="M5 5h6M5 8h6M5 11h3"/></svg>
        Projects
      </button>
      <button class="nav-item ${Store.route==='live-demo'?'nav-item--active':''}" onclick="Store.route='live-demo'; render();">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="8" r="6"/><path d="M3.5 8h9M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12"/></svg>
        Live ShopLite Demo
      </button>
      <button class="nav-item ${Store.route==='history'?'nav-item--active':''}" onclick="openDemoHistory()">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 13h12M4 10l3-3 2 2 3-5"/></svg>
        History
      </button>
      <button class="nav-item ${Store.route==='models'?'nav-item--active':''}" onclick="openSettings()">
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

function renderProjects() {
  const project = Store.projects.find(item => item.id === Store.selectedProjectId) || null;
  const targetControl = (target) => target.authorization_status === 'approved'
    ? `<p class="panel__sub">Allowed host: <code>${(target.allowed_hosts || []).map(esc).join('</code>, <code>')}</code></p><button class="btn btn--ghost" type="button" onclick="revokeProjectTarget('${esc(project.id)}','${esc(target.id)}')" ${Store.projectSaving ? 'disabled' : ''}>Revoke target authorization</button>`
    : `<form class="target-authorization-form" onsubmit="authorizeProjectTarget(event,'${esc(project.id)}','${esc(target.id)}')"><label class="target-authorization-confirm" for="authorize-target-${esc(target.id)}"><input id="authorize-target-${esc(target.id)}" type="checkbox" required ${Store.projectSaving ? 'disabled' : ''}><span>I own this staging target or have permission to test it.</span></label><button class="btn btn--primary" type="submit" ${Store.projectSaving ? 'disabled' : ''}>${Store.projectSaving ? 'Checking…' : 'Authorize target for QA'}</button></form>`;
  return `<div class="models-view projects-view">
    <section class="panel">
      <div class="panel__head"><div class="panel__title">Projects</div><span class="panel__sub">${Store.projects.length} saved ${Store.projects.length === 1 ? 'project' : 'projects'}</span></div>
      <div class="panel__body">
        ${Store.projectsError ? `<p class="error-note" role="alert">${esc(Store.projectsError)}</p>` : ''}
        ${Store.projectsLoading ? '<p class="panel__sub" role="status">Loading projects from PostgreSQL…</p>' : ''}
        ${Store.projects.length ? Store.projects.map(item => `<button class="project-list-row ${item.id === Store.selectedProjectId ? 'project-list-row--active' : ''}" onclick="openProjects('${esc(item.id)}')"><span><strong>${esc(item.name)}</strong><small>${esc(item.targets?.[0]?.base_url || 'No target configured')}</small></span><span class="badge badge--${item.targets?.[0]?.authorization_status === 'approved' ? 'success' : 'info'}">${esc(item.targets?.[0]?.authorization_status || 'target missing')}</span></button>`).join('') : !Store.projectsLoading && !Store.projectsError ? '<div class="empty">No projects yet. Add a project and its intended test URL to begin setup.</div>' : ''}
      </div>
    </section>
    ${project ? `<section class="panel project-detail"><div class="panel__head"><div class="panel__title">${esc(project.name)}</div><span class="badge badge--info">${esc(project.status)}</span></div><div class="panel__body"><p class="panel__sub">Project ID <code>${esc(project.id)}</code> · slug <code>${esc(project.slug)}</code></p><h3>Test targets</h3>${project.targets?.length ? project.targets.map(target => `<article class="project-target"><div><strong>${esc(target.name)}</strong><div><code>${esc(target.base_url)}</code></div><span class="panel__sub">${esc(target.environment_kind)} environment · target version ${esc(target.version)}</span>${targetControl(target)}<p class="panel__sub">Authorization checks HTTPS and public DNS only; it does not load the website. Target scanning remains unavailable until the browser worker enforces destination and redirect policy at connection time.</p></div><span class="badge badge--${target.authorization_status === 'approved' ? 'success' : target.authorization_status === 'revoked' ? 'warning' : 'info'}">Authorization ${esc(target.authorization_status)}</span></article>`).join('') : '<div class="empty">No target has been added.</div>'}<h3>Authorization history</h3>${Store.targetAuthorizationEvents.length ? `<ul class="target-auth-history">${Store.targetAuthorizationEvents.map(item => `<li><strong>${esc(item.action)}</strong> · ${new Date(item.created_at).toLocaleString()} · ${esc(item.actor_id)} · <code>${esc(item.allowed_hosts_snapshot.join(', ') || 'no allowed hosts')}</code></li>`).join('')}</ul>` : '<p class="panel__sub">No authorization changes recorded.</p>'}<h3>Source repository</h3><p class="panel__sub">Not connected · patch generation and pull requests are unavailable for this project.</p></div></section>` : ''}
    <section class="panel project-create"><div class="panel__head"><div class="panel__title">Add a project</div><span class="panel__sub">Saved locally under the current owner</span></div><form class="panel__body" onsubmit="createProject(event)"><div class="field"><label for="project-name">Project name</label><input id="project-name" name="name" value="${esc(Store.projectFormName)}" oninput="updateProjectForm('name',this.value)" maxlength="120" required placeholder="My web application" ${Store.projectSaving ? 'disabled' : ''}></div><div class="field"><label for="project-target-url">Intended test URL</label><input id="project-target-url" name="target_url" type="url" value="${esc(Store.projectFormTargetUrl)}" oninput="updateProjectForm('target_url',this.value)" maxlength="2048" required placeholder="https://staging.example.com" ${Store.projectSaving ? 'disabled' : ''}></div><div class="field"><label for="project-environment-kind">Environment</label><select id="project-environment-kind" name="environment_kind" onchange="updateProjectForm('environment_kind',this.value)" ${Store.projectSaving ? 'disabled' : ''}><option value="staging" ${Store.projectFormEnvironment === 'staging' ? 'selected' : ''}>Staging</option><option value="development" ${Store.projectFormEnvironment === 'development' ? 'selected' : ''}>Development</option><option value="other" ${Store.projectFormEnvironment === 'other' ? 'selected' : ''}>Other</option></select></div><button class="btn btn--primary" type="submit" ${Store.projectSaving ? 'disabled' : ''}>${Store.projectSaving ? 'Saving…' : 'Save project and target'}</button></form></section>
  </div>`;
}

function renderModels() {
  const result = Store.demoRun?.result || {};
  const workspace = Store.workspace;
  const flow = workspace?.test_flow;
  const definition = flow?.definition;
  return `
  <div class="models-view">
    <div class="panel">
      <div class="panel__head"><div class="panel__title">Project and test flow</div><span class="panel__sub">${workspace ? 'Persisted backend workspace · read-only' : 'Fixed ShopLite demo'}</span></div>
      <div class="panel__body">
        ${Store.workspaceError ? `<p class="error-note" role="alert">${esc(Store.workspaceError)}</p>` : ''}
        ${Store.workspaceLoading ? '<p class="panel__sub" role="status">Loading the saved ShopLite workspace…</p>' : ''}
        ${workspace ? `<div class="live-metrics"><div><span>Project</span><strong>${esc(workspace.project.name)}</strong></div><div><span>Target</span><strong>${esc(workspace.target.name)} · ${esc(workspace.target.environment_kind)}</strong></div><div><span>Test flow</span><strong>${esc(flow.name)} · v${esc(flow.version)}</strong></div></div>
        <p class="panel__sub">Target URL: <code>${esc(workspace.target.base_url)}</code> · authorization ${esc(workspace.target.authorization_status)} · flow ${esc(flow.status)} / ${esc(flow.version_status)}</p>
        <p>${esc(flow.description || '')}</p>
        <div class="live-metrics">${(definition?.viewports || []).map((viewport, index) => `<div><span>${index === 0 ? 'Mobile viewport' : 'Desktop viewport'}</span><strong>${esc(viewport.width)} × ${esc(viewport.height)}</strong></div>`).join('')}</div>
        <p class="panel__sub">Checks: ${(definition?.assertions || []).map(assertion => `<code>${esc(assertion)}</code>`).join(' · ')}</p>
        <p class="panel__sub">Definition SHA-256: <code>${esc(flow.definition_hash)}</code></p>` : !Store.workspaceLoading && !Store.workspaceError ? '<p class="panel__sub">Open this dashboard at <code>http://127.0.0.1:8000/</code> to load the local workspace.</p>' : ''}
        ${workspace ? `<div class="field-grid">
          <div class="field"><label for="flow-mobile-width">Mobile width</label><input id="flow-mobile-width" type="number" min="320" max="600" value="${esc(definition?.viewports?.[0]?.width ?? 390)}" ${Store.workspaceSaving ? 'disabled' : ''}></div>
          <div class="field"><label for="flow-mobile-height">Mobile height</label><input id="flow-mobile-height" type="number" min="480" max="2160" value="${esc(definition?.viewports?.[0]?.height ?? 844)}" ${Store.workspaceSaving ? 'disabled' : ''}></div>
          <div class="field"><label for="flow-desktop-width">Desktop width</label><input id="flow-desktop-width" type="number" min="800" max="1920" value="${esc(definition?.viewports?.[1]?.width ?? 1440)}" ${Store.workspaceSaving ? 'disabled' : ''}></div>
          <div class="field"><label for="flow-desktop-height">Desktop height</label><input id="flow-desktop-height" type="number" min="480" max="2160" value="${esc(definition?.viewports?.[1]?.height ?? 900)}" ${Store.workspaceSaving ? 'disabled' : ''}></div>
        </div>
        <p class="panel__sub">Saving publishes a new immutable flow version. New runs use its saved viewport sizes; previous runs keep their original version.</p>
        ${Store.workspaceSaved ? '<p class="success-note" role="status">New test-flow version published.</p>' : ''}
        <button class="btn btn--primary" onclick="publishShopLiteFlowVersion()" ${Store.workspaceSaving ? 'disabled' : ''}>${Store.workspaceSaving ? 'Publishing…' : 'Publish test-flow version'}</button>` : ''}
        <button class="btn" onclick="loadShopLiteWorkspace()">Refresh workspace</button>
      </div>
    </div>
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
      <button class="btn btn--ghost github-restore" onclick="loadLocalGithubState()">Load saved GitHub publishing state</button>
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
            <button class="btn btn--primary" ${['running','succeeded','failed','canceled'].includes(operation?.status) ? 'disabled' : ''} onclick="createLocalDraftPR()">${operation?.status === 'succeeded' ? 'Draft PR already created' : operation?.status === 'remote_outcome_unknown' ? 'Retry or reconcile draft PR' : operation?.status === 'running' ? 'Draft PR request running' : 'Create draft pull request'}</button>` : '<p class="error-note">This report did not pass. Run validation again after correcting the proposal.</p>'}
        </div>
      </section>` : ''}
      ${Store.githubOperations.length ? `<section class="github-operation-history"><div class="panel__title">Saved draft PR operations</div>${Store.githubOperations.slice(0, 5).map(item => `<div class="github-operation-row"><span class="badge badge--${item.status === 'succeeded' ? 'success' : ['failed','canceled'].includes(item.status) ? 'error' : 'info'}">${esc(item.status)}</span><span class="mono">${esc(item.proposal_id)}</span>${item.pull_request_url ? `<a href="${esc(item.pull_request_url)}" target="_blank" rel="noopener noreferrer">PR #${esc(item.pull_request_number)} ↗</a>` : `<span class="panel__sub">${esc(item.safe_error_code || item.operation_id)}</span>`}</div>`).join('')}</section>` : ''}
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

async function loadLocalGithubState() {
  Store.githubError = null;
  try {
    const state = await localGithubRequest('/api/v1/local/github/state');
    Store.githubConnection = state.connection || null;
    Store.githubOperations = state.operations || [];
    Store.githubOperation = Store.githubOperations[0] || null;
    Store.githubValidation = Store.githubOperation
      ? state.validations.find(item => item.report_id === Store.githubOperation.validation_report_id) || null
      : state.validations[0] || null;
    Store.githubIdempotencyKey = Store.githubOperation?.idempotency_key || null;
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
    Store.githubIdempotencyKey = Store.githubOperation.idempotency_key;
    Store.githubOperations = [Store.githubOperation, ...Store.githubOperations.filter(item => item.operation_id !== Store.githubOperation.operation_id)];
  } catch (error) { Store.githubError = error.message; }
  render();
}

function render() {
  if (Store.route === 'projects') {
    if (!Store.projectFormResetPending) {
      const nameInput = document.getElementById('project-name');
      const targetUrlInput = document.getElementById('project-target-url');
      const environmentInput = document.getElementById('project-environment-kind');
      if (nameInput) Store.projectFormName = nameInput.value;
      if (targetUrlInput) Store.projectFormTargetUrl = targetUrlInput.value;
      if (environmentInput) Store.projectFormEnvironment = environmentInput.value;
    }
    Store.projectFormResetPending = false;
  }
  const root = document.getElementById('app');
  const crumb = Store.route === 'models' ? 'Settings' : Store.route === 'projects' ? 'Projects' : Store.route === 'history' ? 'History' : Store.route === 'live-demo' ? 'Live ShopLite Demo' : 'Overview';
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
            ${canPublish ? `<button class="btn btn--primary" onclick="openSettings();document.querySelector('.panel__title')?.scrollIntoView({behavior:'smooth'})">Open GitHub publishing</button>` : ''}
          </div>
        </header>
        <div class="main-content">
          ${Store.route === 'projects' ? renderProjects() : Store.route === 'models' ? renderModels() : Store.route === 'history' ? renderHistory() : Store.route === 'live-demo' ? renderLiveDemo() : renderLanding()}
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

