"""SIMULATED CI/CD event sender (Week 1 webhook demo).

Posts a synthetic build event to the local OmniSight webhook receiver. This
is a SIMULATION: there is no real GitHub Actions/Jenkins integration. Use it
to demonstrate the receiver end-to-end while the Playwright owner's real
evidence pipeline does not exist yet.

Examples (from ``backend/``)::

    .\\.venv\\Scripts\\python.exe scripts\\send_simulated_event.py
    .\\.venv\\Scripts\\python.exe scripts\\send_simulated_event.py \\
        --event-type build.succeeded --target-url https://example.com/shop
    # When OMNISIGHT_CI_WEBHOOK_SECRET is set on the server:
    .\\.venv\\Scripts\\python.exe scripts\\send_simulated_event.py --secret <value>

PowerShell one-liner equivalent (no secret configured)::

    $body = '{"event_id":"demo-1","event_type":"build.succeeded","source":"simulated",
      "repository":"VIVEK252575AI/omnisight","branch":"lakshy","commit_sha":"0",
      "build_id":"demo","target_url":"https://example.com/shop",
      "timestamp":"2026-01-01T00:00:00Z","metadata":{}}'
    Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/webhooks/ci `
      -ContentType application/json -Body $body
"""

import argparse
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


def build_event(args: argparse.Namespace) -> dict:
    """Construct a synthetic event payload (clearly labelled simulated)."""
    event = {
        "event_id": args.event_id or str(uuid.uuid4()),
        "event_type": args.event_type,
        "source": "simulated",
        "repository": args.repository,
        "branch": args.branch,
        "commit_sha": args.commit_sha,
        "build_id": args.build_id or "sim-" + uuid.uuid4().hex[:8],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {"simulated": True, "sender": "send_simulated_event.py"},
    }
    if args.event_type == "build.succeeded":
        event["target_url"] = args.target_url
    return event


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a SIMULATED CI build event to the OmniSight receiver."
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument(
        "--secret",
        default=None,
        help="Shared secret for HMAC signature (omit unless the server requires it)",
    )
    parser.add_argument("--event-id", default=None, help="Reuse an id to test idempotency")
    parser.add_argument(
        "--event-type",
        default="build.succeeded",
        choices=["build.started", "build.succeeded", "build.failed"],
    )
    parser.add_argument("--target-url", default="https://example.com/shop")
    parser.add_argument("--repository", default="VIVEK252575AI/omnisight")
    parser.add_argument("--branch", default="lakshy")
    parser.add_argument("--commit-sha", default="0" * 40)
    parser.add_argument("--build-id", default=None)
    args = parser.parse_args()

    body = json.dumps(build_event(args)).encode("utf-8")
    headers = {"content-type": "application/json"}
    if args.secret:
        headers["X-OmniSight-Signature"] = hmac.new(
            args.secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()

    endpoint = args.url.rstrip("/") + "/api/v1/webhooks/ci"
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(f"{response.status} {response.read().decode('utf-8')}")
    except urllib.error.HTTPError as exc:
        print(f"{exc.code} {exc.read().decode('utf-8')}")
        return 1
    except urllib.error.URLError as exc:
        print(f"connection failed: {exc.reason}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
