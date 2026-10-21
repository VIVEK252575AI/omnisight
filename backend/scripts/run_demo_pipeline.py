"""Run ShopLite browser capture -> Gemini finding -> safe patch -> sandbox.

The target URL and API URL must be loopback-only. Candidate code is verified in
the bounded Docker sandbox and is never written into the checkout. Draft PR
publishing is a separate, explicitly confirmed API action.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from run_demo_detection import (
    DEFAULT_API_URL,
    _loopback_url,
    run as run_detection,
)
from capture_demo_checkout import DEFAULT_FLOW_VIEWPORTS, parse_viewports

DEFAULT_TIMEOUT = 45.0

def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"OmniSight returned invalid JSON (HTTP {response.status_code})") from None
    if not isinstance(body, dict):
        raise RuntimeError("OmniSight returned an unexpected response")
    if response.is_error:
        error = body.get("error")
        message = error.get("message", "request failed") if isinstance(error, dict) else "request failed"
        raise RuntimeError(f"OmniSight returned HTTP {response.status_code}: {str(message)[:300]}")
    return body


def run_pipeline(
    *,
    checkout_url: str,
    api_url: str,
    artifacts: Path,
    expected: str,
    timeout_seconds: float,
    approve_sandbox: bool,
    viewports: list[dict[str, int]] | None = None,
) -> dict:
    configured_viewports = parse_viewports(
        json.dumps(DEFAULT_FLOW_VIEWPORTS if viewports is None else viewports)
    )
    detection = run_detection(
        checkout_url=checkout_url,
        api_url=api_url,
        artifacts=artifacts,
        expected=expected,
        timeout_seconds=timeout_seconds,
        viewports=configured_viewports,
    )
    result: dict = {
        "flow": "shoplite-gemini-safe-patch-sandbox-v1",
        "scan_id": detection["scan_id"],
        "detection_status": detection["status"],
        "detection_adapter": detection.get("adapter"),
        "detection_is_mock": detection.get("is_mock"),
        "detection_reason_code": detection.get("reason_code"),
        "detection_reason_message": detection.get("reason_message"),
        "findings": detection.get("findings", []),
        "capture": detection.get("capture"),
    }
    if detection["status"] != "detected" or not detection.get("findings"):
        result["status"] = "needs_inspection"
        return result

    proposal = None
    attempts: list[dict] = []
    with httpx.Client(base_url=api_url, timeout=timeout_seconds) as client:
        for finding in detection["findings"]:
            attempt = _json(client.post(
                f"/api/v1/findings/{quote(finding['finding_id'], safe='')}/patch-proposals"
            ))
            attempts.append({
                "proposal_id": attempt["proposal_id"],
                "finding_id": attempt["finding_id"],
                "status": attempt["status"],
                "rejection_reasons": attempt.get("rejection_reasons", []),
                "files": attempt.get("files", []),
                "generator_label": attempt.get("generator_label"),
                "is_mock": attempt.get("is_mock"),
            })
            if attempt["status"] == "proposed":
                proposal = attempt
                break
        result["proposal_attempts"] = attempts
        if proposal is None:
            result["status"] = "patch_rejected"
            return result
        result["proposal"] = attempts[-1]
        if not approve_sandbox:
            result["status"] = "awaiting_sandbox_approval"
            return result

        approval_token = os.environ.get("OMNISIGHT_SHOPLITE_SANDBOX_TOKEN", "").strip()
        if not approval_token:
            raise RuntimeError("Set OMNISIGHT_SHOPLITE_SANDBOX_TOKEN in this terminal before approving the sandbox run")
        sandbox = _json(client.post(
            f"/api/v1/demo/shoplite/patch-proposals/{quote(proposal['proposal_id'], safe='')}/validate",
            json={"approval": "approve-demo-only"},
            headers={"X-OmniSight-Demo-Approval": approval_token},
        ))
        result["sandbox"] = sandbox
        result["status"] = "verified" if sandbox.get("status") == "verified" else "validation_failed"

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout-url", default="http://127.0.0.1:4173")
    parser.add_argument("--api-url", type=_loopback_url, default=DEFAULT_API_URL)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--expected", choices=("overflow", "no-overflow"), default="overflow")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--viewports-json", default=json.dumps(DEFAULT_FLOW_VIEWPORTS))
    parser.add_argument(
        "--approve-sandbox",
        action="store_true",
        help="explicitly approve running this safe proposal in the fixed ShopLite Docker sandbox",
    )
    args = parser.parse_args()
    try:
        parsed = urlsplit(args.checkout_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("the ShopLite checkout URL must be plain HTTP on loopback")
        if args.timeout <= 0 or args.timeout > 180:
            raise RuntimeError("--timeout must be greater than 0 and no more than 180 seconds")
        artifacts = args.artifacts.resolve()
        summary = run_pipeline(
            checkout_url=args.checkout_url.rstrip("/"), api_url=args.api_url,
            artifacts=artifacts, expected=args.expected, timeout_seconds=args.timeout,
            approve_sandbox=args.approve_sandbox,
            viewports=parse_viewports(args.viewports_json),
        )
    except Exception as exc:
        print(f"ShopLite pipeline failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    output = artifacts / "demo-checkout" / "omnisight-e2e-summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["summary_file"] = str(output)
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("status") == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
