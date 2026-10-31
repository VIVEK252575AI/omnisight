const BASE = (import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(BASE + path, {
      headers: {"Content-Type":"application/json", ...(options.headers || {})},
      ...options, signal: controller.signal
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.message || `HTTP ${response.status}`);
    return data;
  } finally { clearTimeout(timer); }
}

export async function runOmniSightScan(url, deep = true, onProgress = () => {}) {
  const viewports = deep
    ? [{width:390,height:844},{width:768,height:1024},{width:1440,height:900}]
    : [{width:390,height:844}];

  onProgress("Creating backend scan…");
  const created = await request("/api/scans", {
    method:"POST", body:JSON.stringify({target_url:url, viewports})
  });
  const scanId = created.scan_id || created.id || created.scan?.id;
  if (!scanId) throw new Error("Backend did not return a scan ID.");

  onProgress("Playwright is opening the page and capturing evidence…");
  const capture = await request(`/api/v1/scans/${encodeURIComponent(scanId)}/playwright-capture`, {
    method:"POST", body:JSON.stringify({url})
  });

  onProgress("Starting VLM detection…");
  await request(`/api/v1/scans/${encodeURIComponent(scanId)}/detect`, {method:"POST"});

  let result = null;
  for (let i=0; i<60; i++) {
    onProgress(`Analysing Playwright screenshot with VLM… ${Math.min(99, Math.round((i+1)/60*100))}%`);
    await new Promise(r => setTimeout(r, 500));
    result = await request(`/api/v1/scans/${encodeURIComponent(scanId)}/findings`);
    if (["detected","detection_failed","needs_inspection"].includes(result?.status)) break;
  }
  if (result?.status === "detection_failed") throw new Error(result?.detection?.reason_message || "VLM detection failed.");
  return {scanId, capture, result: result || {}};
}
