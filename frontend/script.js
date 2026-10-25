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
   Model configuration (VLM = vision-language model, LLM = language model)
   mode 'simulated' returns built-in sample output. mode 'live' POSTs each
   stage to the endpoints below (see README for the request/response contract).
   ------------------------------------------------------------------------- */
const Models = {
  mode: 'simulated',
  vlm: { name: 'Backend VLM Adapter', endpoint: 'http://127.0.0.1:8000' },
  llm: { name: 'OmniCoder-LLM 70B', endpoint: '/api/llm' },
  vlmOptions: ['Backend VLM Adapter', 'OmniVision-VL 7B', 'OmniVision-VL 34B'],
  llmOptions: ['OmniCoder-LLM 70B', 'OmniCoder-LLM 8B']
};

{ const m = LS.get('models'); if (m) { Models.mode = m.mode; Models.vlm = m.vlm; Models.llm = m.llm;
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
  if (Models.mode === 'live' && st.key === 'detect') {
    const data = await runBackendVlm(scan);
    scan.findings = data.findings;
    output = JSON.stringify(data.raw, null, 2);
  } else {
    await new Promise(r => setTimeout(r, 1000 + Math.random() * 500));
  }
  scan.trace[st.key] = { model: cfg ? cfg.name : 'Sandbox', kind: st.kind, prompt: sim.prompt, output, ms: Math.round(performance.now() - t0) };
}


function apiBase() { return (Models.vlm.endpoint || 'http://127.0.0.1:8000').replace(/\/$/, ''); }
async function apiJson(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const res = await fetch(apiBase() + path, { ...options, signal: controller.signal });
    let body = null;
    try { body = await res.json(); } catch (_) {}
    if (!res.ok) {
      const message = body?.error?.message || body?.detail || `Backend returned HTTP ${res.status}`;
      throw new Error(message);
    }
    return body;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Backend request timed out after 20 seconds');
    if (error instanceof TypeError) throw new Error('Cannot reach the OmniSight backend. Check the API URL and confirm the server is running.');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function setScanProgress(scan, message) {
  scan.progressMessage = message;
  refresh(scan);
}
function fileAsDataUrl(file) { return new Promise((resolve, reject) => { const r = new FileReader(); r.onload=()=>resolve(r.result); r.onerror=reject; r.readAsDataURL(file); }); }
function mapBackendFinding(record, i) {
  const f = record.finding || record;
  const r = f.region;
  return {
    id: f.finding_id || `vlm-${i+1}`,
    title: f.observation || String(f.category || 'Visual finding').replaceAll('_',' '),
    type: f.category || 'other', severity: f.severity || 'low',
    confidence: f.confidence == null ? '—' : `${Math.round(f.confidence * 100)}%`,
    selector: f.target_candidate || '(visual region)',
    box: r ? [r.x, r.y, r.width, r.height] : [20, 20 + i*48, 120, 40],
    cause: (f.potential_causes && f.potential_causes.length) ? f.potential_causes.join('; ') : (f.evidence || f.observation || 'VLM visual finding')
  };
}
async function runBackendVlm(scan) {
  if (!scan.screenshotFile) throw new Error('Live VLM mode needs a PNG/JPEG screenshot. Choose one next to the URL field.');
  if (!['image/png', 'image/jpeg'].includes(scan.screenshotFile.type)) throw new Error('Screenshot must be a PNG or JPEG image.');
  if (scan.screenshotFile.size > 8 * 1024 * 1024) throw new Error('Screenshot is too large. Please upload an image smaller than 8 MB.');

  const viewports = Store.deep ? [{width:390,height:844},{width:768,height:1024},{width:1440,height:900}] : [{width:390,height:844}];

  setScanProgress(scan, 'Creating backend scan…');
  const created = await apiJson('/api/scans', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({target_url:scan.url, viewports})
  });
  scan.backendScanId = created.scan_id;

  setScanProgress(scan, 'Uploading screenshot evidence…');
  const dataUrl = await fileAsDataUrl(scan.screenshotFile);
  await apiJson(`/api/v1/scans/${created.scan_id}/browser-evidence`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      image_base64:dataUrl,
      media_type:scan.screenshotFile.type || 'image/png',
      viewport:viewports[0]
    })
  });

  setScanProgress(scan, 'Starting VLM detection…');
  await apiJson(`/api/v1/scans/${created.scan_id}/detect`, {method:'POST'});

  let result;
  for (let i = 0; i < 60; i++) {
    setScanProgress(scan, `Analysing screenshot with VLM… ${Math.min(99, Math.round((i + 1) / 60 * 100))}%`);
    await new Promise(r => setTimeout(r, 500));
    result = await apiJson(`/api/v1/scans/${created.scan_id}/findings`);
    if (['detected','detection_failed','needs_inspection'].includes(result.status)) break;
  }

  if (!result || !['detected','detection_failed','needs_inspection'].includes(result.status)) {
    throw new Error('VLM analysis is taking longer than expected. Please retry the scan.');
  }
  if (result.status === 'detection_failed') throw new Error(result.detection?.reason_message || 'VLM detection failed');

  setScanProgress(scan, 'VLM findings received.');
  return {findings:(result.findings || []).map(mapBackendFinding), raw:result};
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

function normalizeUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  try {
    const candidate = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
    const parsed = new URL(candidate);
    return parsed.hostname.includes('.') || parsed.hostname === 'localhost' ? parsed.href : null;
  } catch (_) {
    return null;
  }
}

function previewScreenshot(input) {
  const file = input.files?.[0];
  const preview = document.getElementById('screenshot-preview');
  const meta = document.getElementById('screenshot-meta');
  if (!preview || !meta) return;
  if (!file) {
    preview.hidden = true;
    preview.removeAttribute('src');
    meta.textContent = 'No screenshot selected';
    return;
  }
  if (!['image/png', 'image/jpeg'].includes(file.type)) {
    input.value = '';
    preview.hidden = true;
    meta.textContent = 'Choose a PNG or JPEG image';
    toast('Only PNG and JPEG screenshots are supported');
    return;
  }
  const url = URL.createObjectURL(file);
  preview.src = url;
  preview.hidden = false;
  preview.onload = () => URL.revokeObjectURL(url);
  meta.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
}

function removeScreenshot() {
  const input = document.getElementById('vlm-screenshot');
  const preview = document.getElementById('screenshot-preview');
  const meta = document.getElementById('screenshot-meta');

  if (input) input.value = '';
  if (preview) {
    preview.hidden = true;
    preview.removeAttribute('src');
  }
  if (meta) meta.textContent = 'No screenshot selected';

  toast('Screenshot removed');
}

function retryScan() {
  const previous = Store.scan;
  if (!previous) return;
  Store.route = 'landing';
  render();
  const input = document.getElementById('target-url-input');
  if (input) input.value = previous.url;
  toast('Ready to retry. Re-select the screenshot if you are using Live mode.');
}

function startScan(url) {
  const normalizedUrl = normalizeUrl(url);
  if (!normalizedUrl) { toast('Enter a valid URL (e.g. https://shoplite.local)'); return; }
  url = normalizedUrl;
  const v = document.getElementById('sel-vlm'), l = document.getElementById('sel-llm');
  if (v) Models.vlm.name = v.value;
  if (l) Models.llm.name = l.value;
  const scan = {
    id: String(1042 + Store.history.length), url, status: 'running', stage: 0, trace: {}, error: null,
    progressMessage: 'Preparing scan…',
    findings: Models.mode === 'live' ? [] : SAMPLE_FINDINGS.map(f => ({ ...f })),
    screenshotFile: document.getElementById('vlm-screenshot')?.files?.[0] || null
  };
  Store.route = 'scan'; Store.scan = scan; Store.activeFindingId = 'f-1'; Store.traceTab = null;
  render();
  toast('Scan started for ' + url);
  runPipeline(scan);
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

    <div class="scan-composer">
      <input type="text" id="target-url-input" onkeydown="if(event.key==='Enter')startScan(this.value)" class="scan-composer__input" aria-label="Website URL" placeholder="Enter website URL to inspect... (e.g. https://shoplite.local)" value="https://shoplite.local" />
      <div class="evidence-picker">
        <input type="file" id="vlm-screenshot" accept="image/png,image/jpeg" aria-label="Screenshot for live VLM scan" title="Required in Live mode: upload the screenshot the VLM should inspect" onchange="previewScreenshot(this)" />
        <div class="evidence-preview">
          <img id="screenshot-preview" hidden alt="Preview of screenshot selected for VLM analysis" />
          <span id="screenshot-meta">No screenshot selected</span>
          <button type="button" id="remove-screenshot-btn" class="btn-remove-screenshot" onclick="removeScreenshot()" aria-label="Remove selected screenshot">Remove</button>
        </div>
      </div>
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
    <div class="panel__head"><div class="panel__title">Model trace</div><span class="panel__sub">${Models.mode === 'live' ? 'Live responses' : 'Simulated responses'}</span></div>
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
  const active = shown.find(f => f.id === Store.activeFindingId) || shown[0] || s.findings[0] || {
    id: 'none', title: 'No finding selected', severity: 'low', confidence: '—',
    selector: '(none)', box: [0, 0, 0, 0], cause: 'No visual issue was returned by the VLM.'
  };
  const statusLabel = s.status === 'completed' ? 'completed' : s.status === 'failed' ? 'failed' : STAGES[s.stage].label.toLowerCase() + '…';
  const done = s.trace.detect;
  return `
  <div class="dashboard-view">
    ${renderPipeline(s)}
    ${s.status === 'running' ? `<div class="scan-progress" role="status" aria-live="polite"><span class="scan-progress__spinner"></span><span>${esc(s.progressMessage || 'Running scan…')}</span></div>` : ''}
    ${s.error ? `<div class="error-note"><div><strong>Scan failed.</strong> ${esc(s.error)}</div><button class="btn" onclick="retryScan()">Retry scan</button></div>` : ''}
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
        <div class="field"><label for="m-mode">Mode</label>
          <select id="m-mode"><option value="simulated" ${Models.mode === 'simulated' ? 'selected' : ''}>Simulated (built-in sample output)</option><option value="live" ${Models.mode === 'live' ? 'selected' : ''}>Live (call the endpoints below)</option></select></div>
        <div class="field-grid">${f('m-vlm-name', 'VLM name', Models.vlm.name)}${f('m-vlm-ep', 'Backend API base URL', Models.vlm.endpoint)}</div>
        <div class="field-grid">${f('m-llm-name', 'LLM name', Models.llm.name)}${f('m-llm-ep', 'LLM endpoint', Models.llm.endpoint)}</div>
        <button class="btn btn--primary" onclick="saveModels()">Save settings</button>
      </div>
    </div>
    <div class="panel">
      <div class="panel__head"><div class="panel__title">How a scan runs</div></div>
      <div class="panel__body">
        <ul class="proc-list">${STAGES.map(st => `<li>${kindTag(st.kind)}<span><b>${st.label}.</b> ${st.role}.</span></li>`).join('')}</ul>
      </div>
    </div>
  </div>`;
}

function render() {
  const root = document.getElementById('app');
  const crumb = Store.route === 'scan' ? esc(Store.scan.url) : Store.route === 'models' ? 'Models' : Store.route === 'history' ? 'History' : 'Overview';
  const canShip = Store.route === 'scan' && Store.scan.status === 'completed';
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
            ${canShip ? `<button class="btn" onclick="downloadReport()">Export report</button>` : ''}
            ${canShip ? `<button class="btn btn--primary" onclick="toast('PR #128 drafted (simulated). Connect a backend to open real pull requests.')">Ship PR #128</button>` : ''}
          </div>
        </header>
        <div class="main-content">
          ${Store.route === 'landing' ? renderLanding() : Store.route === 'models' ? renderModels() : Store.route === 'history' ? renderHistory() : renderDashboard()}
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

