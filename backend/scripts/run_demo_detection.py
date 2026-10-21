"""Run the fixed ShopLite browser capture and submit it to OmniSight's API."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from capture_demo_checkout import (
    ARTIFACT_ROOT,
    DEFAULT_BASE_URL,
    _base_url,
    capture,
)

DEFAULT_API_URL = "http://127.0.0.1:8000"
DETECTION_COMPLETE = {"detected", "detection_failed", "needs_inspection"}


def _loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise argparse.ArgumentTypeError("this demo runner only permits an HTTP loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("URL credentials, query, and fragment are not allowed")
    return value.rstrip("/")


def _api_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"OmniSight API returned invalid JSON (HTTP {response.status_code})") from None
    if not isinstance(body, dict):
        raise RuntimeError("OmniSight API returned an unexpected response")
    if response.is_error:
        detail = body.get("error", {}).get("message", "request failed")
        raise RuntimeError(f"OmniSight API returned HTTP {response.status_code}: {detail[:300]}")
    return body


def run(
    *,
    checkout_url: str,
    api_url: str,
    artifacts: Path,
    expected: str,
    timeout_seconds: float,
) -> dict:
    capture_result = capture(checkout_url, artifacts, expected)
    if not capture_result["matches_expected"]:
        raise RuntimeError("browser capture did not match the expected layout state")

    observation_path = artifacts / "demo-checkout" / "mobile" / "observation.json"
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    scan_request = {
        "target_url": f"{checkout_url}/checkout",
        "viewports": [{"width": 390, "height": 844}],
        "routes": ["/checkout"],
    }

    with httpx.Client(base_url=api_url, timeout=10.0) as client:
        scan = _api_json(client.post("/api/scans", json=scan_request))
        scan_id = scan["scan_id"]
        evidence = _api_json(
            client.post(
                f"/api/v1/scans/{scan_id}/evidence",
                content=json.dumps(observation),
                headers={"content-type": "application/json"},
            )
        )
        capture_result["evidence_id"] = evidence["evidence_id"]
        capture_result["screenshot_artifact_id"] = evidence["screenshot_artifact_id"]
        _api_json(client.post(f"/api/v1/scans/{scan_id}/detect"))

        deadline = time.monotonic() + timeout_seconds
        findings_response = None
        while time.monotonic() < deadline:
            findings_response = _api_json(
                client.get(f"/api/v1/scans/{scan_id}/findings")
            )
            if findings_response.get("status") in DETECTION_COMPLETE:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(f"detection did not finish within {timeout_seconds:g} seconds")

    summary = {
        "flow": "demo-checkout-detection-v1",
        "scan_id": scan_id,
        "status": findings_response["status"],
        "adapter": findings_response.get("adapter_label"),
        "is_mock": (
            findings_response.get("detection", {}).get("is_mock")
            if findings_response.get("detection")
            else None
        ),
        "finding_count": len(findings_response.get("findings", [])),
        "findings": [
            {
                "finding_id": item["finding"]["finding_id"],
                "category": item["finding"]["category"],
                "target_candidate": item["finding"]["target_candidate"],
                "target_unverified": item["finding"]["target_unverified"],
                "confidence": item["finding"]["confidence"],
            }
            for item in findings_response.get("findings", [])
        ],
        "capture": capture_result,
    }
    summary_path = artifacts / "demo-checkout" / "api-run-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["summary_file"] = summary_path.relative_to(artifacts).as_posix()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout-url", type=_base_url, default=DEFAULT_BASE_URL)
    parser.add_argument("--api-url", type=_loopback_url, default=DEFAULT_API_URL)
    parser.add_argument("--artifacts", type=Path, default=ARTIFACT_ROOT)
    parser.add_argument("--expected", choices=("overflow", "no-overflow"), default="overflow")
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    try:
        summary = run(
            checkout_url=args.checkout_url,
            api_url=args.api_url,
            artifacts=args.artifacts.resolve(),
            expected=args.expected,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"Demo detection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] in {"detected", "needs_inspection"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
