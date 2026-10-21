"""Exercise the real dashboard through independent, persisted ShopLite runs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


def _loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise argparse.ArgumentTypeError("dashboard verification only permits an HTTP loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("URL credentials, query, and fragment are not allowed")
    return value.rstrip("/")


def _start_and_verify_run(page, *, start_button: str | re.Pattern[str], timeout_ms: int) -> str:
    page.get_by_role("button", name=start_button).click()
    page.get_by_role("heading", name="ShopLite live demo").wait_for(timeout=timeout_ms)
    run_status = page.locator(".live-run-status .badge").first
    run_status.wait_for(timeout=timeout_ms)
    page.get_by_role("button", name="Approve and validate").wait_for(timeout=timeout_ms)
    if not page.get_by_text("Review exact source diff").is_visible():
        raise RuntimeError("The dashboard did not show the exact patch diff for review")

    run_line = page.locator(".live-run-status .panel__sub").inner_text()
    run_id_match = re.search(r"Run (demo_[0-9a-f]{32})", run_line)
    if run_id_match is None:
        raise RuntimeError("The dashboard did not show a valid persisted run identifier")
    run_id = run_id_match.group(1)

    page.locator("#live-demo-confirm").fill("approve-demo-only")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Approve and validate").click()
    run_status.get_by_text("Verified", exact=True).wait_for(timeout=timeout_ms)
    page.get_by_text("Sandbox verification", exact=True).wait_for(timeout=timeout_ms)
    page.get_by_text(re.compile(r"\d+px\s*→\s*0px")).wait_for(timeout=timeout_ms)
    return run_id


def _wait_for_pending_project_target(page, timeout_ms: int, diagnostics: list[list[str]]) -> None:
    setup_timeout_ms = min(timeout_ms, 15_000)
    try:
        page.wait_for_function(
            """() => document.querySelector('.project-detail') ||
              document.querySelector('.projects-view [role="alert"]')""",
            timeout=setup_timeout_ms,
        )
    except PlaywrightTimeoutError:
        view = page.locator(".projects-view")
        page_text = view.inner_text() if view.count() else page.locator("body").inner_text()
        raise RuntimeError(
            f"Project setup did not update the dashboard: {page_text}; "
            f"browser diagnostics: {' | '.join(item for group in diagnostics for item in group) or 'none'}"
        ) from None
    project_detail = page.locator(".project-detail")
    if project_detail.count() == 0:
        message = page.locator(".projects-view [role='alert']").inner_text()
        raise RuntimeError(f"Project setup failed in the dashboard: {message}")
    detail_text = project_detail.inner_text()
    if "authorization pending" not in detail_text.casefold():
        raise RuntimeError(f"Project target did not remain pending authorization: {detail_text}")


def run(base_url: str, timeout_ms: int) -> dict:
    browser_errors: list[str] = []
    browser_diagnostics: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.on("console", lambda message: browser_diagnostics.append(f"console {message.type}: {message.text}") if message.type == "error" else None)
        page.on("response", lambda response: browser_diagnostics.append(f"HTTP {response.status} {response.request.method} {response.url}") if "/api/v1/projects" in response.url else None)
        page.on("requestfailed", lambda request: browser_diagnostics.append(f"request failed {request.method} {request.url}: {request.failure}") if "/api/v1/projects" in request.url else None)
        response = page.goto(base_url + "/", wait_until="domcontentloaded")
        if response is None or response.status != 200:
            raise RuntimeError("The OmniSight dashboard did not load successfully")

        project_name = "CI Project Setup Verification"
        page.get_by_role("button", name="Projects").click()
        page.get_by_label("Project name").fill(project_name)
        page.get_by_label("Intended test URL").fill("https://client.example.invalid")
        page.get_by_role("button", name="Save project and target").click()
        _wait_for_pending_project_target(page, timeout_ms, [browser_errors, browser_diagnostics])
        saved_project = page.evaluate(
            """async (name) => {
              const response = await fetch('/api/v1/projects');
              const projects = await response.json();
              return projects.find(project => project.name === name) || null;
            }""",
            project_name,
        )
        if not saved_project or not saved_project.get("targets") or saved_project["targets"][0].get("authorization_status") != "pending":
            raise RuntimeError("The created project or its pending target was not returned by the backend")
        page.reload(wait_until="domcontentloaded")
        page.get_by_role("button", name="Projects").click()
        project_row = page.locator(".project-list-row").filter(has_text=project_name)
        project_row.wait_for(timeout=timeout_ms)
        project_row.click()
        _wait_for_pending_project_target(page, timeout_ms, [browser_errors, browser_diagnostics])

        page.get_by_role("button", name="Settings").click()
        page.get_by_text("Project and test flow", exact=True).wait_for(timeout=timeout_ms)
        page.get_by_text("OmniSight ShopLite Demo", exact=True).wait_for(timeout=timeout_ms)
        page.get_by_text("ShopLite checkout visual QA", exact=False).wait_for(timeout=timeout_ms)
        page.get_by_text("Definition SHA-256", exact=False).wait_for(timeout=timeout_ms)
        page.get_by_label("Mobile width").fill("400")
        page.get_by_role("button", name="Publish test-flow version").click()
        workspace_panel = page.locator(".models-view > .panel").first
        workspace_panel.get_by_text(re.compile("ShopLite checkout visual QA.*v2")).wait_for(timeout=timeout_ms)
        workspace_panel.get_by_text("400 × 844", exact=True).wait_for(timeout=timeout_ms)
        page.get_by_role("button", name="Overview").click()

        run_id = _start_and_verify_run(
            page, start_button=re.compile("Start ShopLite scan"), timeout_ms=timeout_ms
        )
        persisted_run = page.evaluate(
            """async (runId) => {
              const response = await fetch(`/api/v1/demo/shoplite/runs/${encodeURIComponent(runId)}`);
              return { status: response.status, body: await response.json() };
            }""",
            run_id,
        )
        mobile_viewport = (
            persisted_run.get("body", {})
            .get("result", {})
            .get("capture", {})
            .get("viewports", {})
            .get("mobile", {})
            .get("viewport")
        )
        if persisted_run.get("status") != 200 or mobile_viewport != {"width": 400, "height": 844}:
            raise RuntimeError(
                "The persisted browser capture did not use the published 400x844 viewport: "
                + json.dumps(
                    {
                        "http_status": persisted_run.get("status"),
                        "mobile_viewport": mobile_viewport,
                        "capture": persisted_run.get("body", {}).get("result", {}).get("capture"),
                    },
                    sort_keys=True,
                )[:2_000]
            )
        evidence_panel = page.locator(".live-evidence-shot")
        evidence_panel_count = evidence_panel.count()
        evidence_panel_text = evidence_panel.first.inner_text() if evidence_panel_count else ""
        if "400 × 844" not in evidence_panel_text:
            capture = persisted_run.get("body", {}).get("result", {}).get("capture", {})
            raise RuntimeError(
                "The dashboard did not render the persisted capture viewport: "
                + json.dumps(
                    {
                        "evidence_panel_count": evidence_panel_count,
                        "evidence_panel_text": evidence_panel_text[:1_000],
                        "screenshot_artifact_id": capture.get("screenshot_artifact_id"),
                        "capture_viewports": capture.get("viewports"),
                    },
                    sort_keys=True,
                )[:2_000]
            )

        page.reload(wait_until="domcontentloaded")
        page.get_by_role("button", name=re.compile("History")).click()
        history_row = page.locator(".hist-row").filter(has_text=run_id)
        history_row.wait_for(timeout=timeout_ms)
        history_row.get_by_text("verified", exact=True).wait_for(timeout=timeout_ms)

        page.get_by_role("button", name="Live ShopLite Demo").click()
        second_run_id = _start_and_verify_run(
            page, start_button="Start real scan", timeout_ms=timeout_ms
        )
        if second_run_id == run_id:
            raise RuntimeError("The rerun reused the previous run identifier")

        page.reload(wait_until="domcontentloaded")
        page.get_by_role("button", name=re.compile("History")).click()
        for completed_run_id in (run_id, second_run_id):
            saved_row = page.locator(".hist-row").filter(has_text=completed_run_id)
            saved_row.wait_for(timeout=timeout_ms)
            saved_row.get_by_text("verified", exact=True).wait_for(timeout=timeout_ms)

        if browser_errors:
            raise RuntimeError("Dashboard JavaScript error: " + browser_errors[0][:300])
        result = {
            "status": "verified",
            "run_ids": [run_id, second_run_id],
            "ui_reviewed_patch_diff": True,
            "ui_approved_sandbox_runs": 2,
            "independent_runs_survived_refresh": 2,
            "persisted_workspace_reviewed": True,
            "persisted_project_created": True,
            "target_remains_pending": True,
            "published_flow_version": 2,
            "flow_viewport_used_by_capture": "400x844",
            "browser_error_count": len(browser_errors),
        }
        browser.close()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=_loopback_url, default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=180_000)
    args = parser.parse_args()
    if args.timeout < 1_000 or args.timeout > 300_000:
        parser.error("--timeout must be between 1000 and 300000 milliseconds")
    try:
        result = run(args.base_url, args.timeout)
    except Exception as exc:
        print(f"Dashboard workflow verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
