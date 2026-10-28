"""MR.1 Vision Audit harness.

Drives the *real* FastAPI detection path over HTTP (uvicorn on a real port):
create scan -> submit screenshot-only or DOM observation -> trigger detection ->
poll findings -> score against ground truth -> store one record per
(case, mode, repetition).

IMPORTANT: there is no real VLM provider configured in this repository. Without
``--adapter`` pointing at a real implementation this harness runs the MOCK
adapter, which ignores the images. MOCK results verify the HARNESS only and are
never evidence of VLM accuracy. Every record carries ``adapter_label`` and
``model_inference_performed`` so a mock run cannot be mistaken for a real one.

Usage (from ``backend/``)::

    .\\.venv\\Scripts\\python.exe scripts\\vision_audit.py
    .\\.venv\\Scripts\\python.exe scripts\\vision_audit.py --repetitions 3 ^
        --mock-scenario clipped_button --out ..\\docs\\vision_audit_results.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCANS = "/api/scans"
DETECT = "/api/v1/scans/{scan_id}/detect"
FINDINGS = "/api/v1/scans/{scan_id}/findings"

DEFECT_RUN = "defect"
CLEAN = "clean"
AMBIGUOUS = "needs_inspection_or_hedged"


# ---------------------------------------------------------------------------
# Scoring (pure; unit-tested without a server)
# ---------------------------------------------------------------------------

_SELECTOR_TOKEN = re.compile(r"[#.][A-Za-z0-9_-]+")


def _selector_tokens(value: str) -> set[str]:
    return {t.lower() for t in _SELECTOR_TOKEN.findall(value)}


def target_matches(expected: str | None, candidate: str | None) -> bool:
    """True when a finding's target plausibly denotes the ground-truth target."""
    if not expected or not candidate:
        return False
    e, c = expected.strip().lower(), candidate.strip().lower()
    if e == c:
        return True
    # "#checkout" vs "button#checkout" / "checkout-btn" are the same region of
    # interest; compare id/class tokens, never free text.
    return bool(_selector_tokens(e) & _selector_tokens(c))


def _region(value: Any) -> dict | None:
    """Normalise a region: findings use width/height, DOM boxes use w/h."""
    if not isinstance(value, dict):
        return None
    try:
        return {
            "x": float(value["x"]),
            "y": float(value["y"]),
            "w": float(value["w"] if "w" in value else value["width"]),
            "h": float(value["h"] if "h" in value else value["height"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def regions_overlap(expected: dict | None, candidate: Any) -> bool:
    """True when the expected and reported boxes share any area."""
    e = _region(expected)
    c = _region(candidate)
    if e is None or c is None:
        return False
    left = max(e["x"], c["x"])
    right = min(e["x"] + e["w"], c["x"] + c["w"])
    top = max(e["y"], c["y"])
    bottom = min(e["y"] + e["h"], c["y"] + c["h"])
    return right > left and bottom > top


def score_run(case: dict, record: dict) -> dict:
    """Score one run against one ground-truth case.

    Returns category/target/region booleans plus a single verdict. The verdict
    is deliberately conservative: a defect is "detected" only when a finding's
    category matches the injected category; target/region plausibility is
    reported separately so a right-category/wrong-target hit is visible.
    """
    gt = case.get("injected_defect")
    expectation = case.get("expectation")
    findings = [r.get("finding", {}) for r in record.get("findings", [])]
    categories = [f.get("category") for f in findings]
    scan_status = record.get("scan_status")
    outcome = record.get("outcome")
    failed = scan_status == "detection_failed"

    if gt is None:
        if failed:
            verdict = "error"
        elif expectation == AMBIGUOUS:
            hedged = outcome == "needs_inspection" or any(
                f.get("confidence") is not None and f["confidence"] < 0.5
                for f in findings
            )
            verdict = "hedged" if hedged else "not_hedged"
        elif findings:
            verdict = "false_positive"
        elif outcome == "no_defects" or scan_status == "detected":
            verdict = "clean"
        else:
            verdict = "error"
        return {
            "verdict": verdict,
            "category_correct": None,
            "target_plausible": None,
            "region_plausible": None,
        }

    expected_category = gt["category"]
    category_correct = expected_category in categories
    matching = [f for f in findings if f.get("category") == expected_category]
    target_plausible = any(
        target_matches(gt.get("target"), f.get("target_candidate")) for f in matching
    )
    region_plausible = any(
        regions_overlap(gt.get("region"), f.get("region")) for f in matching
    )

    if failed:
        verdict = "error"
    elif not findings:
        verdict = "missed"
    elif category_correct:
        verdict = "detected"
    else:
        verdict = "wrong_category"

    result = {
        "verdict": verdict,
        "category_correct": category_correct,
        "target_plausible": bool(target_plausible),
        "region_plausible": bool(region_plausible),
    }
    if case.get("case_id") == "hidden_submit":
        result["plain_result"] = "detected" if category_correct else "missed"
    return result


# ---------------------------------------------------------------------------
# HTTP / server plumbing
# ---------------------------------------------------------------------------

def request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int, artifacts_dir: Path, mock_scenario: str, adapter: str):
    os.environ["OMNISIGHT_ARTIFACTS_DIR"] = str(artifacts_dir)
    os.environ["OMNISIGHT_VLM_ADAPTER"] = adapter
    os.environ["OMNISIGHT_VLM_MOCK_SCENARIO"] = mock_scenario
    os.environ["OMNISIGHT_DETECTION_EXPOSE_RAW_OUTPUT"] = "true"
    os.environ["OMNISIGHT_AI_ALLOW_RAW_OUTPUT_STORAGE"] = "false"
    os.environ["OMNISIGHT_LOG_LEVEL"] = "WARNING"

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
            return server
        time.sleep(0.1)
    raise RuntimeError("uvicorn did not start within 20s")


def build_observation(case: dict, mode: str) -> dict:
    elements = list(case["dom_elements"]) if mode == "with_dom" else []
    return {
        "schema_version": "observation/v1",
        "url": f"https://shoplite.local{case['route']}",
        "route": case["route"],
        "state": f"{case['case_id']}-{mode}",
        "viewport": case["viewport"],
        "screenshot": case["image"],
        "elements": elements,
        "console_errors": [],
        "network_errors": [],
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def wait_settled(base: str, scan_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        code, payload = request("GET", f"{base}{FINDINGS.format(scan_id=scan_id)}?include_raw=true")
        if code != 200:
            raise RuntimeError(f"findings request failed ({code}): {payload}")
        last = payload
        status = payload.get("status")
        if status not in {"created", "evidence_collection", "detecting"}:
            return payload
        time.sleep(0.1)
    raise RuntimeError(f"detection did not settle within {timeout}s (last status={last.get('status')})")


def run_one(base: str, case: dict, mode: str, repetition: int, mock_scenario: str, timeout: float) -> dict:
    started = time.perf_counter()
    code, scan = request(
        "POST",
        f"{base}{SCANS}",
        {
            "target_url": f"https://shoplite.local{case['route']}",
            "viewports": [case["viewport"]],
            "routes": [case["route"]],
        },
    )
    if code != 201:
        raise RuntimeError(f"scan create failed ({code}): {scan}")
    scan_id = scan["scan_id"]

    code, evidence = request(
        "POST",
        f"{base}/api/v1/scans/{scan_id}/evidence",
        build_observation(case, mode),
    )
    if code != 201:
        raise RuntimeError(f"evidence rejected ({code}): {evidence}")

    code, trigger = request("POST", f"{base}{DETECT.format(scan_id=scan_id)}")
    if code != 202:
        raise RuntimeError(f"detection trigger failed ({code}): {trigger}")

    payload = wait_settled(base, scan_id, timeout)
    wall_ms = round((time.perf_counter() - started) * 1000.0, 3)
    detection = payload.get("detection") or {}
    findings = payload.get("findings") or []

    record = {
        "case_id": case["case_id"],
        "mode": mode,
        "repetition": repetition,
        "mock_scenario": mock_scenario,
        "scan_id": scan_id,
        "evidence_id": evidence.get("evidence_id"),
        "scan_status": payload.get("status"),
        "adapter_label": payload.get("adapter_label") or detection.get("adapter_label"),
        "is_mock": detection.get("is_mock"),
        "model_inference_performed": detection.get("is_mock") is not True,
        "detection_status": detection.get("status"),
        "outcome": detection.get("outcome"),
        "reason_code": detection.get("reason_code"),
        "reason_message": detection.get("reason_message"),
        "attempts": detection.get("attempts"),
        "duration_ms": detection.get("duration_ms"),
        "wall_ms": wall_ms,
        "raw_output": detection.get("raw_output"),
        "raw_output_present": detection.get("raw_output") is not None,
        "findings": findings,
        "triggers": {"accepted": trigger.get("accepted")},
        "parse_status": (
            "ok"
            if detection.get("outcome") is not None
            else ("rejected" if detection.get("reason_code") else "unknown")
        ),
    }
    record["score"] = score_run(case, record)
    return record


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summarize(records: list[dict], cases: list[dict]) -> dict:
    by_case: dict[str, dict] = {}
    for case in cases:
        cid = case["case_id"]
        by_case[cid] = {
            "case_id": cid,
            "expectation": case["expectation"],
            "injected_category": (case.get("injected_defect") or {}).get("category"),
            "modes": {},
        }

    for record in records:
        entry = by_case[record["case_id"]]["modes"].setdefault(
            record["mode"],
            {
                "runs": 0,
                "verdicts": {},
                "category_correct": 0,
                "target_plausible": 0,
                "region_plausible": 0,
                "wall_ms": [],
            },
        )
        entry["runs"] += 1
        verdict = record["score"]["verdict"]
        entry["verdicts"][verdict] = entry["verdicts"].get(verdict, 0) + 1
        if record["score"].get("category_correct"):
            entry["category_correct"] += 1
        if record["score"].get("target_plausible"):
            entry["target_plausible"] += 1
        if record["score"].get("region_plausible"):
            entry["region_plausible"] += 1
        entry["wall_ms"].append(record["wall_ms"])

    for entry in by_case.values():
        for mode_entry in entry["modes"].values():
            mode_entry["stable"] = len(mode_entry["verdicts"]) == 1
            mode_entry["median_wall_ms"] = round(
                statistics.median(mode_entry["wall_ms"]), 3
            )
            del mode_entry["wall_ms"]

    defects = [r for r in records if r["score"].get("category_correct") is not None]
    hidden = [
        {
            "mode": r["mode"],
            "repetition": r["repetition"],
            "verdict": r["score"]["verdict"],
            "plain_result": r["score"].get("plain_result"),
        }
        for r in records
        if r["case_id"] == "hidden_submit"
    ]
    return {
        "total_records": len(records),
        "modes": sorted({r["mode"] for r in records}),
        "repetitions": sorted({r["repetition"] for r in records}),
        "adapter_labels": sorted({r["adapter_label"] for r in records}),
        "is_mock": sorted({r["is_mock"] for r in records}),
        "model_inference_performed": any(r["model_inference_performed"] for r in records),
        "defect_runs": len(defects),
        "category_correct_runs": sum(1 for r in defects if r["score"]["category_correct"]),
        "target_plausible_runs": sum(1 for r in defects if r["score"]["target_plausible"]),
        "region_plausible_runs": sum(1 for r in defects if r["score"]["region_plausible"]),
        "healthy_runs": sum(1 for r in records if r["case_id"] == "healthy_control"),
        "false_positive_runs": sum(
            1 for r in records if r["score"]["verdict"] == "false_positive"
        ),
        "ambiguous_runs": sum(1 for r in records if r["case_id"] == "ambiguous_case"),
        "hedged_runs": sum(1 for r in records if r["score"]["verdict"] == "hedged"),
        "detection_failures": sum(1 for r in records if r["scan_status"] == "detection_failed"),
        "hidden_submit": hidden,
        "cases": list(by_case.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MR.1 vision audit harness (real API path)")
    here = Path(__file__).resolve().parent
    repo = here.parent.parent
    parser.add_argument(
        "--manifest",
        type=Path,
        default=here.parent / "tests" / "fixtures" / "vision_audit" / "ground_truth.json",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--modes", default="screenshot_only,with_dom")
    parser.add_argument("--adapter", default="mock")
    parser.add_argument("--mock-scenario", default="clipped_button")
    parser.add_argument("--out", type=Path, default=repo / "docs" / "vision_audit_results.json")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    artifacts_dir = args.manifest.resolve().parent
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    port = args.port or free_port()
    base = f"http://127.0.0.1:{port}"
    server = start_server(port, artifacts_dir, args.mock_scenario, args.adapter)
    records: list[dict] = []
    failures: list[str] = []
    try:
        for case in cases:
            for mode in modes:
                for repetition in range(1, args.repetitions + 1):
                    try:
                        record = run_one(base, case, mode, repetition, args.mock_scenario, args.timeout)
                    except Exception as exc:  # noqa: BLE001 - audit must record, not crash
                        failures.append(
                            f"{case['case_id']}/{mode}/rep{repetition}: {type(exc).__name__}: {exc}"
                        )
                        print(
                            json.dumps(
                                {
                                    "case_id": case["case_id"],
                                    "mode": mode,
                                    "repetition": repetition,
                                    "result": "HARNESS_ERROR",
                                    "detail": str(exc)[:300],
                                }
                            ),
                            flush=True,
                        )
                        continue
                    records.append(record)
                    print(
                        json.dumps(
                            {
                                "case_id": record["case_id"],
                                "mode": record["mode"],
                                "repetition": repetition,
                                "adapter": record["adapter_label"],
                                "status": record["scan_status"],
                                "verdict": record["score"]["verdict"],
                            }
                        ),
                        flush=True,
                    )
    finally:
        server.should_exit = True

    summary = summarize(records, cases)
    result = {
        "task": "MR.1 Vision Audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus_id": manifest.get("corpus_id"),
        "corpus_provenance": manifest.get("provenance"),
        "adapter_note": manifest.get("adapter_note"),
        "real_model_audit_performed": bool(summary["model_inference_performed"]),
        "adapter_requested": args.adapter,
        "mock_scenario": args.mock_scenario,
        "manifest": str(args.manifest),
        "artifacts_dir": str(artifacts_dir),
        "harness_failures": failures,
        "summary": summary,
        "records": records,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "result": "OK" if not failures else "INCOMPLETE",
                "records": len(records),
                "expected": len(cases) * len(modes) * args.repetitions,
                "failures": len(failures),
                "real_model_audit_performed": result["real_model_audit_performed"],
                "out": str(args.out),
            }
        ),
        flush=True,
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
