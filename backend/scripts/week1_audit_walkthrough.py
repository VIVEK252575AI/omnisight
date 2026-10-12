"""Week 1 integration walk-through (W1.5 audit).

Boots the *real* FastAPI application with uvicorn on a real TCP port, drives it
over HTTP, checks the Week 1 boundary (simulated CI event -> scan created ->
evidence registered) plus the negative cases, then shuts the server down.
Prints one JSON result line per step and exits non-zero on any mismatch.

SIMULATION notice: the CI events sent here are synthetic. There is no real
GitHub Actions/Jenkins integration.

Usage (from ``backend/``)::

    .\\.venv\\Scripts\\python.exe scripts\\week1_audit_walkthrough.py --port 8011
    .\\.venv\\Scripts\\python.exe scripts\\week1_audit_walkthrough.py --port 8012 ^
        --artifacts-dir tests\\fixtures --server-secret audit-secret
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CI = "/api/v1/webhooks/ci"
EVENTS = "/api/v1/webhooks/events"
SCANS = "/api/scans"


class Checker:
    def __init__(self) -> None:
        self.failures = 0

    def record(self, name: str, expected, observed, detail: str = "") -> None:
        ok = expected == observed
        if not ok:
            self.failures += 1
        line = {"step": name, "expected": expected, "observed": observed,
                "result": "PASS" if ok else "FAIL"}
        if detail:
            line["detail"] = detail[:200]
        print(json.dumps(line), flush=True)


def request(method: str, url: str, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def sign(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def start_server(port: int, artifacts_dir: str, server_secret: str | None):
    os.environ["OMNISIGHT_ARTIFACTS_DIR"] = artifacts_dir
    if server_secret:
        os.environ["OMNISIGHT_CI_WEBHOOK_SECRET"] = server_secret
    else:
        os.environ.pop("OMNISIGHT_CI_WEBHOOK_SECRET", None)

    import importlib

    import uvicorn

    main = importlib.import_module("app.main")
    config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    return server


def run_checks(base: str, evidence_path: str, secret: str | None) -> int:
    c = Checker()

    st, _ = request("GET", f"{base}/health")
    c.record("health", 200, st)

    event_id = f"audit-{uuid.uuid4()}"
    event = {
        "event_id": event_id, "event_type": "build.succeeded", "source": "simulated",
        "repository": "VIVEK252575AI/omnisight", "branch": "lakshy",
        "commit_sha": "0" * 40, "build_id": "audit-build",
        "target_url": "https://shoplite.local/checkout",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {"audit": True},
    }
    raw = json.dumps(event).encode()
    headers = {"content-type": "application/json"}
    if secret:
        headers["X-OmniSight-Signature"] = sign(secret, raw)
    st, text = request("POST", f"{base}{CI}", raw, headers)
    c.record("ci.build.succeeded", 202, st)
    scan_id = json.loads(text).get("scan_id") if st in (200, 202) else None

    st, text = request("POST", f"{base}{CI}", raw, headers)
    dup = json.loads(text).get("status") if st == 200 else None
    c.record("ci.duplicate", 200, st, detail=f"body.status={dup}")

    st, _ = request("GET", f"{base}{EVENTS}")
    c.record("events.list", 200, st)

    st, text = request("GET", f"{base}{SCANS}/{scan_id}")
    c.record("scan.fetch_created", 200, st, detail=f"status={json.loads(text).get('status') if st == 200 else '?'}")

    evidence = Path(evidence_path).read_bytes()
    st, text = request("POST", f"{base}/api/v1/scans/{scan_id}/evidence", evidence,
                       {"content-type": "application/json"})
    c.record("evidence.submit", 201, st)
    artifact_id = json.loads(text).get("screenshot_artifact_id") if st == 201 else None

    st, _ = request("GET", f"{base}/api/v1/scans/{scan_id}/evidence")
    c.record("evidence.list", 200, st)

    st, text = request("GET", f"{base}{SCANS}/{scan_id}")
    c.record("scan.fetch_collected", "evidence_collection",
             json.loads(text).get("status") if st == 200 else None)

    st, _ = request("GET", f"{base}/api/v1/artifacts/{artifact_id}")
    c.record("artifact.fetch", 200, st)

    st, text = request("POST", f"{base}/api/v1/scans/{scan_id}/evidence", evidence,
                       {"content-type": "application/json"})
    code = json.loads(text)["error"]["code"] if st in (400, 409, 422) else None
    c.record("evidence.wrong_state", 409, st, detail=f"code={code}")

    st, text = request("POST", f"{base}{SCANS}",
                       json.dumps({"target_url": "https://shoplite.local/checkout"}).encode(),
                       {"content-type": "application/json"})
    scan2 = json.loads(text).get("scan_id") if st == 201 else None
    bad = json.loads(evidence.decode())
    bad["screenshot"] = "screenshots/../../escape.png"
    st, text = request("POST", f"{base}/api/v1/scans/{scan2}/evidence",
                       json.dumps(bad).encode(), {"content-type": "application/json"})
    code = json.loads(text)["error"]["code"] if st == 400 else None
    c.record("evidence.traversal", 400, st, detail=f"code={code}")

    if secret:
        st, _ = request("POST", f"{base}{CI}", raw, {"content-type": "application/json",
                        "X-OmniSight-Signature": "deadbeef"})
        c.record("ci.bad_signature", 401, st)
        st, _ = request("POST", f"{base}{CI}", raw, {"content-type": "application/json"})
        c.record("ci.missing_signature", 401, st)
    else:
        print(json.dumps({"step": "ci.signature", "result": "SKIP",
                          "detail": "server has no secret; signature not enforced"}), flush=True)

    print(json.dumps({"summary": "FAIL" if c.failures else "PASS", "failures": c.failures}), flush=True)
    return 1 if c.failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Week 1 backend walk-through (SIMULATED).")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--artifacts-dir", default="tests/fixtures")
    ap.add_argument("--evidence-file", default="tests/fixtures/observation_fixture.json")
    ap.add_argument("--server-secret", default=None,
                    help="If set, the booted server requires this HMAC secret")
    args = ap.parse_args()

    server = start_server(args.port, args.artifacts_dir, args.server_secret)
    base = f"http://127.0.0.1:{args.port}"
    try:
        return run_checks(base, args.evidence_file, args.server_secret)
    finally:
        server.should_exit = True
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
