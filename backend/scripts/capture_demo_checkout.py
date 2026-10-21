"""Run the fixed local checkout flow and write observation/v1 evidence."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from playwright.sync_api import sync_playwright

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
from app.qa_flows import DEFAULT_FLOW_STEPS, validate_flow_steps

DEFAULT_BASE_URL = "http://127.0.0.1:4173"
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}
DEFAULT_FLOW_VIEWPORTS = [VIEWPORTS["mobile"], VIEWPORTS["desktop"]]
ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = ROOT / "backend" / "artifacts"


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise argparse.ArgumentTypeError(
            "this demo runner only permits an HTTP loopback URL"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise argparse.ArgumentTypeError("URL paths, credentials, query, and fragment are not allowed")
    return value.rstrip("/")


def _visible_element(page, selector: str, viewport: dict[str, int]) -> dict | None:
    locator = page.locator(selector).first
    if locator.count() == 0 or not locator.is_visible():
        return None
    box = locator.bounding_box()
    if box is None:
        return None
    return {
        "selector": selector,
        "tag": locator.evaluate("element => element.tagName.toLowerCase()"),
        "text": locator.inner_text(timeout=1000)[:500],
        "visible": True,
        "in_viewport": (
            box["x"] >= 0
            and box["y"] >= 0
            and box["x"] + box["width"] <= viewport["width"]
            and box["y"] + box["height"] <= viewport["height"]
        ),
        "box": {
            "x": box["x"],
            "y": box["y"],
            "w": box["width"],
            "h": box["height"],
        },
    }


def _safe_network_url(value: str) -> str | None:
    """Keep request diagnostics useful without retaining URL credentials or query data."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        netloc = f"{host}:{port}" if port and port not in {80, 443} else host
        return urlunsplit((parsed.scheme, netloc, parsed.path[:1500], "", ""))[:2000]
    except (UnicodeError, ValueError):
        return None


def parse_viewports(raw: str) -> list[dict[str, int]]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("flow viewports must be valid JSON") from None
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, dict) for item in value):
        raise ValueError("flow must define exactly two viewports")
    mobile, desktop = value
    for viewport in value:
        if set(viewport) != {"width", "height"}:
            raise ValueError("viewport definitions contain unsupported fields")
        if any(type(viewport[key]) is not int for key in ("width", "height")):
            raise ValueError("viewport dimensions must be integers")
        if not 320 <= viewport["width"] <= 1920 or not 480 <= viewport["height"] <= 2160:
            raise ValueError("viewport dimensions exceed the supported range")
    if mobile["width"] > 600 or desktop["width"] < 800:
        raise ValueError("flow viewports must be ordered mobile first, then desktop")
    return [mobile, desktop]


def parse_flow_steps(raw: str) -> list[dict]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("flow steps must be valid JSON") from None
    if not isinstance(value, list):
        raise ValueError("flow steps must be a JSON array")
    try:
        return validate_flow_steps(value)
    except Exception:
        raise ValueError("flow steps failed schema validation") from None


def _flow_locator(page, spec: dict):
    strategy = spec["strategy"]
    value = spec.get("value", "")
    exact = spec.get("exact", False)
    if strategy == "role":
        return page.get_by_role(spec["role"], name=value, exact=exact)
    if strategy == "label":
        return page.get_by_label(value, exact=exact)
    if strategy == "placeholder":
        return page.get_by_placeholder(value, exact=exact)
    if strategy == "text":
        return page.get_by_text(value, exact=exact)
    if strategy == "test_id":
        return page.get_by_test_id(value)
    if strategy == "css":
        return page.locator(value)
    raise ValueError("unsupported locator strategy")


def _execute_flow_steps(
    page, steps: list[dict], viewport_name: str, evidence_dir: Path,
    artifact_root: Path, base_url: str, capture_id: str,
) -> list[dict]:
    """Execute only the schema-validated finite set of browser operations."""
    results: list[dict] = []
    for step in steps:
        started = time.monotonic()
        outcome = "passed"
        evidence = None
        try:
            step_type = step["type"]
            if step_type == "navigate":
                response = page.goto(
                    f"{base_url}{step['url']}", wait_until="domcontentloaded",
                    timeout=step.get("timeout_ms", 15000),
                )
                if response is None or response.status >= 400:
                    outcome = "failed"
            elif step_type == "assert_url":
                actual_path = urlsplit(page.url).path.rstrip("/") or "/"
                if actual_path != step["url"]:
                    outcome = "failed"
            elif step_type == "screenshot":
                screenshot = evidence_dir / f"flow-step-{capture_id}-{step['key']}.jpg"
                screenshot_bytes = page.screenshot(type="jpeg", quality=75, full_page=False)
                if len(screenshot_bytes) > 2_000_000:
                    outcome = "failed"
                else:
                    screenshot.write_bytes(screenshot_bytes)
                    evidence = screenshot.relative_to(artifact_root).as_posix()
            elif step_type == "capture_dom":
                locator = _flow_locator(page, step["locator"]) if step.get("locator") else page.locator("body")
                if locator.count() != 1:
                    outcome = "failed"
                else:
                    snapshot = locator.evaluate(
                        "element => ({tag: element.tagName.toLowerCase(), child_count: element.children.length, child_tags: Array.from(element.children).slice(0, 50).map(child => child.tagName.toLowerCase())})"
                    )
                    dom_path = evidence_dir / f"flow-step-{capture_id}-{step['key']}.json"
                    dom_path.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
                    evidence = dom_path.relative_to(artifact_root).as_posix()
            else:
                locator = _flow_locator(page, step["locator"])
                timeout = step.get("timeout_ms", 5000)
                if step_type == "wait_for":
                    locator.wait_for(state=step.get("state", "visible"), timeout=timeout)
                    count = locator.count()
                    if count > 1 or (step.get("state") not in {"hidden", "detached"} and count != 1):
                        outcome = "failed"
                elif step_type == "assert_visible":
                    locator.wait_for(state="visible", timeout=timeout)
                    if locator.count() != 1 or not locator.is_visible():
                        outcome = "failed"
                else:
                    if locator.count() != 1:
                        outcome = "failed"
                    elif step_type == "click":
                        locator.click(timeout=timeout)
                    elif step_type == "fill":
                        locator.fill(step["value"], timeout=timeout)
                    elif step_type == "select_option":
                        locator.select_option(step["value"], timeout=timeout)
                    elif step_type == "assert_text":
                        observed = locator.inner_text(timeout=timeout).strip()
                        expected = step["text"]
                        matches = observed == expected if step.get("exact") else expected in observed
                        if not matches:
                            outcome = "failed"
                    else:
                        outcome = "failed"
        except Exception as exc:
            outcome = "timeout" if "Timeout" in type(exc).__name__ else "execution_error"
        results.append({
            "step_key": step["key"],
            "label": step["label"],
            "type": step["type"],
            "viewport": viewport_name,
            "status": outcome,
            "duration_ms": round((time.monotonic() - started) * 1000),
            **({"evidence": evidence} if evidence else {}),
        })
    return results


def capture(
    base_url: str, artifact_root: Path, expected: str,
    viewports: list[dict[str, int]] | None = None,
    steps: list[dict] | None = None,
) -> dict:
    base_url = _base_url(base_url)
    configured = parse_viewports(json.dumps(DEFAULT_FLOW_VIEWPORTS if viewports is None else viewports))
    configured_steps = validate_flow_steps(DEFAULT_FLOW_STEPS if steps is None else steps)
    allowed_target = urlsplit(base_url)
    capture_id = uuid4().hex
    active_viewports = {"desktop": configured[1], "mobile": configured[0]}
    run_dir = artifact_root / "demo-checkout"
    run_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    captured_at = datetime.now(timezone.utc).isoformat()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for viewport_name, viewport in active_viewports.items():
                context = browser.new_context(viewport=viewport, device_scale_factor=1)
                def enforce_target_policy(route):
                    requested = urlsplit(route.request.url)
                    if (
                        requested.scheme == "http"
                        and requested.hostname == allowed_target.hostname
                        and requested.port == allowed_target.port
                        and requested.username is None
                        and requested.password is None
                    ):
                        route.continue_()
                    else:
                        route.abort("blockedbyclient")

                context.route("**/*", enforce_target_policy)
                page = context.new_page()
                console_errors: list[dict] = []
                page_errors: list[dict] = []
                network_errors: list[dict] = []
                page.on(
                    "console",
                    lambda message: console_errors.append(
                        {"message": message.text[:1000], "type": message.type}
                    )
                    if message.type == "error" and len(console_errors) < 20
                    else None,
                )
                page.on(
                    "requestfailed",
                    lambda request: network_errors.append(
                        {
                            "url": safe_url,
                            "method": request.method[:20],
                            "error": "request_failed",
                            "resource_type": request.resource_type[:100],
                        }
                    )
                    if len(network_errors) < 20
                    and (safe_url := _safe_network_url(request.url)) is not None
                    else None,
                )
                page.on(
                    "response",
                    lambda response: network_errors.append(
                        {
                            "url": safe_url,
                            "method": response.request.method[:20],
                            "status": response.status,
                            "resource_type": response.request.resource_type[:100],
                        }
                    )
                    if response.status >= 400
                    and len(network_errors) < 20
                    and (safe_url := _safe_network_url(response.url)) is not None
                    else None,
                )
                page.on(
                    "pageerror",
                    lambda error: page_errors.append(
                        {"message": str(error)[:1000], "type": "pageerror"}
                    )
                    if len(page_errors) < 20
                    else None,
                )

                response = page.goto(
                    f"{base_url}/checkout",
                    wait_until="networkidle",
                    timeout=10_000,
                )
                if response is None or response.status >= 400:
                    raise RuntimeError("demo checkout did not return a successful response")
                page.get_by_role("heading", name="Checkout", exact=True).wait_for(
                    state="visible", timeout=5_000
                )

                button = page.locator("#place-order")
                box = button.bounding_box()
                if box is None:
                    raise RuntimeError("place-order control has no visible bounding box")
                overflow_px = max(
                    0.0, box["x"] + box["width"] - viewport["width"]
                )

                evidence_dir = run_dir / viewport_name
                evidence_dir.mkdir(parents=True, exist_ok=True)
                screenshot = evidence_dir / "checkout.png"
                page.screenshot(path=str(screenshot), full_page=False)

                elements = [
                    item
                    for selector in (
                        "h1",
                        "#place-order",
                        "[data-testid='checkout-summary']",
                    )
                    if (item := _visible_element(page, selector, viewport)) is not None
                ]
                observation = {
                    "schema_version": "observation/v1",
                    "url": page.url,
                    "route": "/checkout",
                    "state": "checkout-before-submit",
                    "viewport": viewport,
                    "screenshot": screenshot.relative_to(artifact_root).as_posix(),
                    "elements": elements,
                    "console_errors": console_errors,
                    "page_errors": page_errors,
                    "network_errors": network_errors,
                    "captured_at": captured_at,
                }
                (evidence_dir / "observation.json").write_text(
                    json.dumps(observation, indent=2), encoding="utf-8"
                )

                flow_steps = _execute_flow_steps(
                    page, configured_steps, viewport_name, evidence_dir,
                    artifact_root, base_url, capture_id,
                )
                status = page.locator("#order-status")
                confirmation = status.inner_text(timeout=1000)[:200] if status.count() == 1 else ""

                results[viewport_name] = {
                    "viewport": viewport,
                    "button_overflow_px": round(overflow_px, 2),
                    "order_confirmation": confirmation,
                    "screenshot": observation["screenshot"],
                    "observation": (
                        evidence_dir / "observation.json"
                    ).relative_to(artifact_root).as_posix(),
                    "console_error_count": len(console_errors),
                    "page_error_count": len(page_errors),
                    "network_error_count": len(network_errors),
                    "flow_steps": flow_steps,
                }
                context.close()
        finally:
            browser.close()

    mobile_overflow = results["mobile"]["button_overflow_px"] > 0
    results_summary = {
        "flow": "demo-checkout-v1",
        "target": f"{base_url}/checkout",
        "expected": expected,
        "matches_expected": mobile_overflow == (expected == "overflow"),
        "flow_status": (
            "passed"
            if all(step["status"] == "passed" for viewport in results.values() for step in viewport["flow_steps"])
            else "failed"
        ),
        "flow_steps": [step for viewport in results.values() for step in viewport["flow_steps"]],
        "viewports": results,
        "artifact_root": str(artifact_root),
    }
    (run_dir / "run-summary.json").write_text(
        json.dumps(results_summary, indent=2), encoding="utf-8"
    )
    return results_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=_base_url, default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=ARTIFACT_ROOT,
        help="artifact root for screenshots and observation/v1 JSON",
    )
    parser.add_argument(
        "--expected",
        choices=("overflow", "no-overflow"),
        default="overflow",
        help="expected mobile button state; use no-overflow after the fix",
    )
    parser.add_argument("--viewports-json", default=json.dumps(DEFAULT_FLOW_VIEWPORTS))
    parser.add_argument("--steps-json", default=json.dumps(DEFAULT_FLOW_STEPS))
    args = parser.parse_args()
    try:
        result = capture(
            args.base_url, args.artifacts.resolve(), args.expected,
            parse_viewports(args.viewports_json), parse_flow_steps(args.steps_json),
        )
    except Exception as exc:
        print(f"Demo flow failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0 if result["matches_expected"] and result["flow_status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
