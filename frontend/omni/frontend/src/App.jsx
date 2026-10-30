import { useState } from "react";

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export default function App() {
  const [targetUrl, setTargetUrl] = useState("");
  const [status, setStatus] = useState("Ready to scan");
  const [currentStage, setCurrentStage] = useState("ready");
  const [findings, setFindings] = useState([]);
  const [captureInfo, setCaptureInfo] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // Backend API helper
  async function apiRequest(path, options = {}) {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      ...options,
    });

    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(
        data.detail ||
          data.message ||
          `Request failed with status ${response.status}`
      );
    }

    return data;
  }

  // Get scan ID from backend response
  function getScanId(scan) {
    return scan.scan_id || scan.id || scan.scan?.id;
  }

  // Main OmniSight workflow
  async function startScan(event) {
    event.preventDefault();

    if (!targetUrl.trim()) {
      setError("Please enter a website URL.");
      return;
    }

    setLoading(true);
    setError("");
    setFindings([]);
    setCaptureInfo(null);

    try {
      // STEP 1: Create scan
      setCurrentStage("frontend");
      setStatus("Creating OmniSight scan...");

      const scan = await apiRequest("/api/scans", {
        method: "POST",
        body: JSON.stringify({
          url: targetUrl.trim(),
        }),
      });

      const scanId = getScanId(scan);

      if (!scanId) {
        throw new Error(
          "Backend created the scan but did not return a scan ID."
        );
      }

      // STEP 2: Playwright captures website
      setCurrentStage("playwright");
      setStatus("Playwright is capturing the webpage...");

      const capture = await apiRequest(
        `/api/v1/scans/${encodeURIComponent(
          scanId
        )}/playwright-capture`,
        {
          method: "POST",
          body: JSON.stringify({
            url: targetUrl.trim(),
          }),
        }
      );

      setCaptureInfo(capture);

      // STEP 3: VLM analyzes screenshot
      setCurrentStage("vlm");
      setStatus("Screenshot captured. Starting VLM analysis...");

      await apiRequest(
        `/api/v1/scans/${encodeURIComponent(scanId)}/detect`,
        {
          method: "POST",
        }
      );

      // STEP 4: Get findings
      setCurrentStage("findings");
      setStatus("Loading detected UI issues...");

      const result = await apiRequest(
        `/api/v1/scans/${encodeURIComponent(scanId)}/findings`
      );

      let detectedFindings = [];

      if (Array.isArray(result)) {
        detectedFindings = result;
      } else if (Array.isArray(result.findings)) {
        detectedFindings = result.findings;
      } else if (Array.isArray(result.items)) {
        detectedFindings = result.items;
      }

      setFindings(detectedFindings);
      setCurrentStage("complete");

      setStatus(
        `Scan completed — ${detectedFindings.length} issue(s) detected.`
      );
    } catch (err) {
      console.error("OmniSight scan failed:", err);

      setCurrentStage("error");
      setError(err.message || "Something went wrong.");
      setStatus("Scan failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app">
      {/* Header */}
      <header className="header">
        <div>
          <p className="subtitle">
            MULTIMODAL UI SELF-HEALING & RPA AGENT
          </p>
          <h1>OmniSight</h1>
        </div>

        <div className="status-badge">{status}</div>
      </header>

      {/* Hero */}
      <section className="hero">
        <h2>AI-Powered Visual UI Testing</h2>

        <p>
          Enter a website URL. Playwright will automatically
          open the webpage and capture visual evidence before
          the VLM analyzes the interface.
        </p>
      </section>

      {/* Pipeline */}
      <section className="pipeline">
        <PipelineStep
          number="1"
          title="React"
          active={[
            "frontend",
            "playwright",
            "vlm",
            "findings",
            "complete",
          ].includes(currentStage)}
        />

        <PipelineStep
          number="2"
          title="Playwright"
          active={[
            "playwright",
            "vlm",
            "findings",
            "complete",
          ].includes(currentStage)}
        />

        <PipelineStep
          number="3"
          title="VLM"
          active={[
            "vlm",
            "findings",
            "complete",
          ].includes(currentStage)}
        />

        <PipelineStep
          number="4"
          title="Findings"
          active={["findings", "complete"].includes(currentStage)}
        />
      </section>

      {/* Scan form */}
      <section className="scan-card">
        <h2>Start UI Scan</h2>

        <form onSubmit={startScan}>
          <label htmlFor="targetUrl">Website URL</label>

          <div className="url-input-group">
            <input
              id="targetUrl"
              type="url"
              placeholder="https://example.com"
              value={targetUrl}
              onChange={(event) =>
                setTargetUrl(event.target.value)
              }
              disabled={loading}
              required
            />

            <button type="submit" disabled={loading}>
              {loading ? "Scanning..." : "Start AI Scan"}
            </button>
          </div>
        </form>

        <p className="help-text">
          Flow: React → FastAPI → Playwright → Screenshot →
          VLM → Findings
        </p>
      </section>

      {/* Playwright result */}
      {captureInfo && (
        <section className="result-card">
          <h2>Playwright Evidence</h2>

          <p>
            <strong>Page Title:</strong>{" "}
            {captureInfo.page_title || "Unknown"}
          </p>

          <p>
            <strong>Final URL:</strong>{" "}
            {captureInfo.final_url ||
              captureInfo.target_url ||
              targetUrl}
          </p>

          <p>
            <strong>Screenshot:</strong>{" "}
            {captureInfo.screenshot_path ||
              "Screenshot captured"}
          </p>

          {captureInfo.http_status && (
            <p>
              <strong>HTTP Status:</strong>{" "}
              {captureInfo.http_status}
            </p>
          )}
        </section>
      )}

      {/* Error */}
      {error && (
        <section className="error-message">
          <strong>Error:</strong> {error}
        </section>
      )}

      {/* VLM findings */}
      {findings.length > 0 && (
        <section className="findings-section">
          <h2>VLM Findings</h2>

          <div className="findings-grid">
            {findings.map((finding, index) => (
              <article
                className="finding-card"
                key={
                  finding.finding_id ||
                  finding.id ||
                  index
                }
              >
                <span className="severity">
                  {finding.severity || "Issue"}
                </span>

                <h3>
                  {finding.title ||
                    finding.issue ||
                    `Finding ${index + 1}`}
                </h3>

                <p>
                  {finding.description ||
                    finding.cause ||
                    "A visual UI issue was detected."}
                </p>

                {finding.selector && (
                  <code>{finding.selector}</code>
                )}
              </article>
            ))}
          </div>
        </section>
      )}

      {/* No issues */}
      {currentStage === "complete" &&
        findings.length === 0 && (
          <section className="result-card">
            <h2>No UI Issues Detected</h2>

            <p>
              The VLM analysis completed without returning
              any findings.
            </p>
          </section>
        )}
    </div>
  );
}

function PipelineStep({ number, title, active }) {
  return (
    <div
      className={`pipeline-step ${active ? "active" : ""}`}
    >
      <span className="step-number">{number}</span>
      <span>{title}</span>
    </div>
  );
}