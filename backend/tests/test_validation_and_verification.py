"""W3.3 tests: bounded validation, workspace serving, fresh capture, verification.

What is proven here:

* a passing command passes, a non-zero exit fails, a timeout fails **and kills
  the whole process tree** (a grandchild spawned by the build does not survive);
* the child process cannot see the server's secrets;
* a workspace server becomes ready or fails within a bounded time and is always
  stopped;
* the deterministic checks re-measure the ORIGINAL defect (clipped element,
  overflow, overlap, visibility, contrast) and say ``not_checkable`` instead of
  guessing;
* the policy table: a deterministic failure is never overruled by a model, an
  ``inconclusive`` model never verifies, and a new high-severity issue fails;
* malformed model output becomes ``inconclusive`` with a reason recorded;
* build success + an unfixed defect ends the attempt in ``verification_failed``.

One optional test runs the REAL Playwright collector against a copy of the
ShopLite demo; it is skipped only when no browser is installed on the host.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.ai.adapters.base import VisionAnalyzer
from app.ai.contracts import (
    Finding,
    FindingCategory,
    FindingStatus,
    RawVisionResult,
    Severity,
)
from app.ai.errors import VisionTimeoutError
from app.command_runner import (
    CommandError,
    run_command,
    sanitized_env,
)
from app.config import Settings
from app.detection_schemas import FindingRecord
from app.detection_storage import DetectionStore
from app.evidence_capture import (
    CaptureError,
    FixtureEvidenceCollector,
    ShopLiteCheckoutCollector,
    load_observation,
)
from app.evidence_storage import EvidenceStore
from app.ids import new_id, new_uuid, utcnow
from app.local_server import ServeError, StaticFileServer, served_workspace
from app.orchestrator import AttemptStatus, AttemptStore, Orchestrator
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
)
from app.patch_storage import PatchStore
from app.patch_verifier import SourcePatchVerifier, WorkspaceValidator
from app.schemas import Element, ElementBox, Observation, ScanStatus, Viewport
from app.storage import ScanStore
from app.validation import ValidationRunner, ValidationStatus
from app.verification import (
    AnalyzerVlmVerifier,
    CheckStatus,
    DeterministicVerification,
    MockVlmVerifier,
    VerificationRequest,
    VerificationStatus,
    VlmIssue,
    VlmVerdict,
    VlmVerification,
    contrast_ratio,
    decide,
    deterministic_check,
)
from app.workspace import WorkspaceManager

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_ROOT = _REPO_ROOT / "demo-app"
_MOBILE = Viewport(width=390, height=844)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    values: dict = {"enable_repair_orchestrator": True}
    values.update(overrides)
    return Settings(**values)


def _python(code: str) -> list[str]:
    """An argument-list command that runs inline Python (never a shell string)."""
    return [sys.executable, "-c", code]


def _observation(
    *,
    in_viewport: bool = True,
    width: int = 390,
    box_width: float = 342.0,
    box_x: float = 24.0,
    selector: str = "#place-order",
    route: str = "/checkout",
    style: dict[str, str] | None = None,
    extra: list[Element] | None = None,
) -> Observation:
    elements = [
        Element(
            selector=selector,
            tag="button",
            text="Place demo order · $48.00",
            visible=True,
            in_viewport=in_viewport,
            box=ElementBox(x=box_x, y=420.0, w=box_width, h=48.0),
            computed_style=style,
        ),
        *(extra or []),
    ]
    return Observation(
        url="http://127.0.0.1:4173/checkout",
        route=route,
        state="checkout-before-submit",
        viewport=Viewport(width=width, height=844),
        screenshot="demo-checkout/mobile/checkout.png",
        elements=elements,
        captured_at=utcnow(),
    )


def _capture(*observations: Observation, label: str = "FIXTURE"):
    screens = [(obs, Path("unused.png")) for obs in observations]
    result = FixtureEvidenceCollector(screens).capture(
        scan_id=uuid4(), viewports=[obs.viewport for obs in observations]
    )
    assert result.collector_label == label
    return result


def _finding(
    category: FindingCategory = FindingCategory.clipped_element,
    *,
    selector: str = "#place-order",
) -> Finding:
    return Finding(
        category=category,
        target_candidate=selector,
        observation="MOCK finding used by the verification tests.",
        severity=Severity.high,
        confidence=0.9,
        status=FindingStatus.detected,
    )


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _mini_source(root: Path) -> Path:
    (root / "styles").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<html><body>ok</body></html>\n", encoding="utf-8")
    (root / "styles" / "checkout.css").write_text(":root { color: #111; }\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# command runner: exit codes, timeout, tree kill, environment
# ---------------------------------------------------------------------------


def test_command_success_is_bounded(tmp_path):
    result = run_command(
        _python("print('hello')"),
        cwd=tmp_path,
        timeout_seconds=30,
        max_output_bytes=1024,
    )

    assert result.passed
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert not result.timed_out


def test_non_zero_exit_is_a_failure(tmp_path):
    result = run_command(
        _python("raise SystemExit(3)"),
        cwd=tmp_path,
        timeout_seconds=30,
        max_output_bytes=1024,
    )

    assert not result.passed
    assert result.exit_code == 3
    assert not result.timed_out


def test_output_is_truncated_without_losing_validity(tmp_path):
    result = run_command(
        _python("print('x' * 5000)"),
        cwd=tmp_path,
        timeout_seconds=30,
        max_output_bytes=256,
    )

    assert result.passed
    assert result.truncated
    assert "[output truncated]" in result.stdout
    assert len(result.stdout.encode("utf-8")) < 400


def test_timeout_kills_the_whole_process_tree(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open(r'{pidfile}', 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )

    result = run_command(
        _python(code), cwd=tmp_path, timeout_seconds=1.0, max_output_bytes=1024
    )

    assert result.timed_out
    assert result.exit_code is None
    grandchild = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _pid_alive(grandchild):
        time.sleep(0.1)
    assert not _pid_alive(grandchild), "the grandchild process outlived the timeout"


def test_child_cannot_see_server_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNISIGHT_GITHUB_APP_PRIVATE_KEY", "super-secret-value")
    monkeypatch.setenv("SOME_API_TOKEN", "another-secret")
    code = (
        "import os\n"
        "print('GITHUB=' + str(os.environ.get('OMNISIGHT_GITHUB_APP_PRIVATE_KEY')))\n"
        "print('TOKEN=' + str(os.environ.get('SOME_API_TOKEN')))\n"
        "print('GEMINI=' + str(os.environ.get('GEMINI_API_KEY')))\n"
    )

    result = run_command(
        _python(code), cwd=tmp_path, timeout_seconds=30, max_output_bytes=2048
    )

    assert "super-secret-value" not in result.stdout
    assert "another-secret" not in result.stdout
    assert "GITHUB=None" in result.stdout
    assert "TOKEN=None" in result.stdout


def test_secret_looking_env_extra_is_dropped():
    env = sanitized_env({"MY_API_KEY": "value", "SAFE_FLAG": "1"})

    assert "MY_API_KEY" not in env
    assert env.get("SAFE_FLAG") == "1"


def test_missing_executable_is_a_coded_error(tmp_path):
    with pytest.raises(CommandError) as exc:
        run_command(
            ["definitely-not-a-real-binary-omnisight"],
            cwd=tmp_path,
            timeout_seconds=5,
            max_output_bytes=256,
        )
    assert exc.value.code == "COMMAND_NOT_FOUND"


# ---------------------------------------------------------------------------
# validation runner
# ---------------------------------------------------------------------------


def test_validation_passes_zero_exit(tmp_path):
    runner = ValidationRunner(settings=_settings(validation_commands=[_python("print('ok')")]))

    result = runner.run(tmp_path)

    assert result.status is ValidationStatus.passed
    assert result.passed
    assert result.commands[0].exit_code == 0


def test_validation_fails_non_zero_exit(tmp_path):
    runner = ValidationRunner(
        settings=_settings(validation_commands=[_python("raise SystemExit(2)")])
    )

    result = runner.run(tmp_path)

    assert result.status is ValidationStatus.failed
    assert not result.passed
    assert "exited with code 2" in result.summary()


def test_validation_fails_on_timeout_and_stops(tmp_path):
    runner = ValidationRunner(
        settings=_settings(
            validation_commands=[
                _python("import time; time.sleep(30)"),
                _python("print('never runs')"),
            ],
            command_timeout_seconds=0.5,
        )
    )

    result = runner.run(tmp_path)

    assert result.status is ValidationStatus.failed
    assert result.commands[0].timed_out
    # A later command cannot rescue a timed-out build, so it never runs.
    assert len(result.commands) == 1


def test_validation_without_commands_is_not_configured(tmp_path):
    runner = ValidationRunner(settings=_settings(validation_commands=[]))

    result = runner.run(tmp_path)

    assert result.status is ValidationStatus.not_configured
    assert not result.passed
    assert "not configured" in result.summary()


def test_validation_commands_are_never_a_shell_string():
    """A plain string is parsed as JSON configuration, never split or run."""
    with pytest.raises(Exception):
        Settings(validation_commands="echo hello && rm -rf /")


# ---------------------------------------------------------------------------
# serving the workspace
# ---------------------------------------------------------------------------


def test_static_server_serves_and_stops(tmp_path):
    source = _mini_source(tmp_path / "source")
    settings = _settings(serve_ready_timeout_seconds=10)

    with served_workspace(settings, source) as served:
        from urllib.request import urlopen

        with urlopen(f"{served.base_url}/index.html", timeout=5) as response:
            assert response.status == 200
            assert b"ok" in response.read()
        base_url = served.base_url

    # After the context exits, nothing answers and the port is released.
    import socket

    host, port = base_url.rsplit(":", 1)
    with socket.socket() as probe:
        probe.settimeout(2)
        assert probe.connect_ex((host.rsplit("//", 1)[-1], int(port))) != 0


def test_server_that_never_becomes_ready_fails_in_bounded_time(tmp_path):
    source = _mini_source(tmp_path / "source")
    settings = _settings(
        serve_command=_python("import time; time.sleep(30)"),
        serve_url="http://127.0.0.1:1",
        serve_ready_timeout_seconds=0.4,
    )

    started = time.monotonic()
    with pytest.raises(ServeError) as exc:
        with served_workspace(settings, source):
            pass
    elapsed = time.monotonic() - started

    assert exc.value.code == "SERVE_NOT_READY"
    assert elapsed < 10


def test_serve_url_must_be_loopback(tmp_path):
    source = _mini_source(tmp_path / "source")
    settings = _settings(
        serve_command=_python("print('no')"),
        serve_url="http://example.com:8080",
    )

    with pytest.raises(ServeError) as exc:
        with served_workspace(settings, source):
            pass

    assert exc.value.code == "SERVE_URL_NOT_LOOPBACK"


def test_static_server_rejects_a_missing_root(tmp_path):
    server = StaticFileServer(tmp_path / "does-not-exist")
    with pytest.raises(ServeError) as exc:
        server.start()
    assert exc.value.code == "SERVE_ROOT_MISSING"


# ---------------------------------------------------------------------------
# deterministic checks
# ---------------------------------------------------------------------------


def test_clipped_element_passes_once_inside_the_viewport():
    capture = _capture(_observation(in_viewport=True, box_width=342.0))

    result = deterministic_check(_finding(), capture, viewport=_MOBILE)

    assert result.status is CheckStatus.passed
    assert result.checks[0].name == "target_in_viewport"


def test_clipped_element_still_clipped_is_a_deterministic_failure():
    capture = _capture(
        _observation(in_viewport=False, box_width=520.0)
    )

    result = deterministic_check(_finding(), capture, viewport=_MOBILE)

    assert result.status is CheckStatus.failed
    assert result.checks[0].measured == pytest.approx(154.0)
    assert "clipped" in result.summary()


def test_responsive_layout_failure_measures_overflow():
    capture = _capture(_observation(in_viewport=False, box_width=520.0))

    result = deterministic_check(
        _finding(FindingCategory.responsive_layout_failure), capture, viewport=_MOBILE
    )

    assert result.status is CheckStatus.failed
    assert result.checks[0].name == "horizontal_overflow"
    assert result.checks[0].measured > 0


def test_overlapping_elements_measures_intersection():
    overlapping = Element(
        selector=".summary-card",
        tag="div",
        visible=True,
        in_viewport=True,
        box=ElementBox(x=0.0, y=430.0, w=200.0, h=60.0),
    )
    capture = _capture(_observation(in_viewport=True, extra=[overlapping]))

    result = deterministic_check(
        _finding(FindingCategory.overlapping_elements), capture, viewport=_MOBILE
    )

    assert result.status is CheckStatus.failed
    assert result.checks[0].name == "overlap_area"
    assert result.checks[0].measured > 0


def test_hidden_element_check_uses_visibility():
    observation = _observation()
    observation.elements[0].visible = False  # type: ignore[misc]
    capture = _capture(observation)

    result = deterministic_check(
        _finding(FindingCategory.hidden_or_unreachable_element), capture, viewport=_MOBILE
    )

    assert result.status is CheckStatus.failed


def test_contrast_check_passes_above_the_threshold():
    style = {"color": "#1a1a1a", "background-color": "#ffffff"}
    capture = _capture(_observation(style=style))

    result = deterministic_check(
        _finding(FindingCategory.low_contrast_text), capture, viewport=_MOBILE
    )

    assert result.status is CheckStatus.passed
    assert result.checks[0].measured >= 4.5


def test_contrast_check_fails_below_the_threshold():
    style = {"color": "#bbbbbb", "background-color": "#ffffff"}
    capture = _capture(_observation(style=style))

    result = deterministic_check(
        _finding(FindingCategory.low_contrast_text), capture, viewport=_MOBILE
    )

    assert result.status is CheckStatus.failed


def test_contrast_without_colours_is_not_checkable():
    capture = _capture(_observation(style=None))

    result = deterministic_check(
        _finding(FindingCategory.low_contrast_text), capture, viewport=_MOBILE
    )

    assert result.status is CheckStatus.not_checkable


def test_missing_selector_is_not_checkable_not_a_pass():
    capture = _capture(_observation(selector="#something-else"))

    result = deterministic_check(_finding(), capture, viewport=_MOBILE)

    assert result.status is CheckStatus.not_checkable
    assert "not captured" in result.summary()


def test_contrast_ratio_maths():
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("rgb(255, 255, 255)", "#ffffff") == pytest.approx(1.0)
    assert contrast_ratio("not-a-colour", "#ffffff") is None


# ---------------------------------------------------------------------------
# policy table
# ---------------------------------------------------------------------------


def _deterministic(status: CheckStatus) -> DeterministicVerification:
    return DeterministicVerification(
        status=status,
        category=FindingCategory.clipped_element,
        selector="#place-order",
        checks=[],
    )


def _vlm(verdict: VlmVerdict, *, issues: list[VlmIssue] | None = None, mock=True):
    return VlmVerification(
        verdict=verdict,
        provider_label="MOCK" if mock else "TEST",
        is_mock=mock,
        new_issues=issues or [],
    )


@pytest.mark.parametrize(
    "det,vlm_verdict,expected",
    [
        (CheckStatus.passed, VlmVerdict.resolved, VerificationStatus.verified),
        (CheckStatus.passed, VlmVerdict.not_resolved, VerificationStatus.failed),
        (CheckStatus.passed, VlmVerdict.inconclusive, VerificationStatus.inconclusive),
        (CheckStatus.failed, VlmVerdict.resolved, VerificationStatus.failed),
        (CheckStatus.failed, VlmVerdict.not_resolved, VerificationStatus.failed),
        (CheckStatus.failed, VlmVerdict.inconclusive, VerificationStatus.failed),
        (CheckStatus.not_checkable, VlmVerdict.resolved, VerificationStatus.verified),
        (CheckStatus.not_checkable, VlmVerdict.not_resolved, VerificationStatus.failed),
        (
            CheckStatus.not_checkable,
            VlmVerdict.inconclusive,
            VerificationStatus.inconclusive,
        ),
    ],
)
def test_policy_table(det, vlm_verdict, expected):
    status, reasons = decide(
        deterministic=_deterministic(det), vlm=_vlm(vlm_verdict)
    )

    assert status is expected
    if expected is not VerificationStatus.verified:
        assert reasons, "a non-verified verdict must explain itself"


def test_policy_deterministic_failure_overrides_model_success():
    status, reasons = decide(
        deterministic=_deterministic(CheckStatus.failed),
        vlm=_vlm(VlmVerdict.resolved),
    )

    assert status is VerificationStatus.failed
    assert any("disagrees" in reason for reason in reasons)


def test_policy_new_high_severity_issue_blocks_verification():
    issue = VlmIssue(
        category=FindingCategory.overlapping_elements,
        severity=Severity.high,
        observation="the header now overlaps the summary",
    )

    status, reasons = decide(
        deterministic=_deterministic(CheckStatus.passed),
        vlm=_vlm(VlmVerdict.resolved, issues=[issue]),
    )

    assert status is VerificationStatus.failed
    assert any("new high-severity" in reason for reason in reasons)


def test_policy_ignores_new_low_severity_issues():
    issue = VlmIssue(
        category=FindingCategory.overlapping_elements,
        severity=Severity.low,
        observation="minor spacing difference",
    )

    status, _ = decide(
        deterministic=_deterministic(CheckStatus.passed),
        vlm=_vlm(VlmVerdict.resolved, issues=[issue]),
    )

    assert status is VerificationStatus.verified


def test_policy_missing_vlm_is_inconclusive():
    status, reasons = decide(deterministic=_deterministic(CheckStatus.passed), vlm=None)

    assert status is VerificationStatus.inconclusive
    assert reasons


def test_policy_missing_validation_is_a_failure():
    status, reasons = decide(
        deterministic=_deterministic(CheckStatus.passed),
        vlm=_vlm(VlmVerdict.resolved),
        validation_passed=False,
    )

    assert status is VerificationStatus.failed
    assert reasons == ["validation did not pass"]


# ---------------------------------------------------------------------------
# VLM verification parsing (through the real analyzer boundary)
# ---------------------------------------------------------------------------


class FakeAnalyzer:
    """TEST DOUBLE for a VisionAnalyzer returning scripted verification text."""

    provider_label = "MOCK"
    is_mock = True

    def __init__(self, *, text: str | None = None, error: Exception | None = None):
        self.text = text
        self.error = error
        self.requests = []

    def analyze(self, request, *, repair: bool = False) -> RawVisionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return RawVisionResult(text=self.text or "", provider_label="MOCK")


def _verification_request() -> VerificationRequest:
    return VerificationRequest(
        finding=_finding(),
        route="/checkout",
        viewport=_MOBILE,
        before_artifact_ids=[new_uuid()],
        after_artifact_ids=[new_uuid()],
        deterministic_status=CheckStatus.passed,
    )


def test_analyzer_verifier_parses_a_valid_verdict():
    analyzer = FakeAnalyzer(
        text='{"verdict": "resolved", "confidence": 0.8, "observation": "button fits"}'
    )
    verifier = AnalyzerVlmVerifier(analyzer, settings=_settings())

    result = verifier.verify(_verification_request())

    assert result.verdict is VlmVerdict.resolved
    assert result.is_mock is True
    assert result.provider_label == "MOCK"
    assert analyzer.requests[0].prompt_version == "verify/v1"


def test_analyzer_verifier_rejects_malformed_json():
    analyzer = FakeAnalyzer(text="the button looks fine to me")
    verifier = AnalyzerVlmVerifier(analyzer, settings=_settings())

    result = verifier.verify(_verification_request())

    assert result.verdict is VlmVerdict.inconclusive
    assert "malformed" in result.reason


def test_analyzer_verifier_rejects_a_schema_violation():
    analyzer = FakeAnalyzer(
        text='{"verdict": "definitely_fine", "new_issues": [], "extra": 1}'
    )
    verifier = AnalyzerVlmVerifier(analyzer, settings=_settings())

    result = verifier.verify(_verification_request())

    assert result.verdict is VlmVerdict.inconclusive
    assert "malformed" in result.reason


def test_analyzer_verifier_handles_provider_errors():
    analyzer = FakeAnalyzer(error=VisionTimeoutError("timed out"))
    verifier = AnalyzerVlmVerifier(analyzer, settings=_settings())

    result = verifier.verify(_verification_request())

    assert result.verdict is VlmVerdict.inconclusive
    assert "provider error" in result.reason


def test_wiring_returns_none_without_the_flag(tmp_path):
    from app.repair_wiring import build_repair_components

    components = build_repair_components(
        settings=_settings(enable_repair_orchestrator=False, patch_source_root=str(tmp_path)),
        detection_store=DetectionStore(),
        evidence_store=EvidenceStore(artifacts_root=tmp_path / "artifacts"),
    )

    assert components is None


def test_wiring_returns_none_without_a_source_root(tmp_path):
    from app.repair_wiring import build_repair_components

    components = build_repair_components(
        settings=_settings(enable_repair_orchestrator=True, patch_source_root=""),
        detection_store=DetectionStore(),
        evidence_store=EvidenceStore(artifacts_root=tmp_path / "artifacts"),
    )

    assert components is None


def test_wiring_builds_the_real_components(tmp_path):
    from app.repair_wiring import build_repair_components

    source = _mini_source(tmp_path / "source")
    components = build_repair_components(
        settings=_settings(
            enable_repair_orchestrator=True,
            patch_source_root=str(source),
            approved_patch_paths=["styles/**"],
            workspace_root=tmp_path / "ws",
            vlm_verifier="mock",
        ),
        detection_store=DetectionStore(),
        evidence_store=EvidenceStore(artifacts_root=tmp_path / "artifacts"),
    )

    assert components is not None
    assert type(components.applier).__name__ == "SourcePatchApplier"
    assert type(components.validator).__name__ == "WorkspaceValidator"
    assert type(components.verifier).__name__ == "SourcePatchVerifier"


def test_unknown_mock_scenario_fails_fast():
    with pytest.raises(ValueError):
        MockVlmVerifier("made-up-scenario")


# ---------------------------------------------------------------------------
# SourcePatchVerifier + WorkspaceValidator
# ---------------------------------------------------------------------------


def _verifier(
    tmp_path, *, det_observation: Observation, vlm: MockVlmVerifier, commands=None
):
    source = _mini_source(tmp_path / "source")
    settings = _settings(
        patch_source_root=str(source),
        approved_patch_paths=["styles/**"],
        workspace_root=tmp_path / "ws",
        validation_commands=commands or [],
    )
    workspaces = WorkspaceManager(settings=settings)
    workspace = workspaces.create()

    detection_store = DetectionStore()
    finding = _finding()
    scan_id = new_uuid()
    detection_store.replace_findings(
        scan_id,
        [
            FindingRecord(
                finding=finding,
                scan_id=scan_id,
                adapter_label="MOCK",
                is_mock=True,
                route="/checkout",
                viewport=_MOBILE,
                screenshot_artifact_ids=[new_uuid()],
            )
        ],
    )
    evidence_store = EvidenceStore(artifacts_root=tmp_path / "artifacts")
    verifier = SourcePatchVerifier(
        settings=settings,
        detection_store=detection_store,
        evidence_store=evidence_store,
        workspace_manager=workspaces,
        vlm=vlm,
        collector=FixtureEvidenceCollector([(det_observation, Path("x.png"))]),
    )
    validator = WorkspaceValidator(settings=settings, workspace_manager=workspaces)
    return verifier, validator, workspace, finding.finding_id


def test_verifier_passes_when_both_signals_agree(tmp_path):
    verifier, _validator, workspace, finding_id = _verifier(
        tmp_path,
        det_observation=_observation(in_viewport=True),
        vlm=MockVlmVerifier("resolved"),
    )

    outcome = verifier.verify(finding_id, workspace.workspace_id)

    assert outcome.passed is True
    assert outcome.detail["status"] == "verified"
    assert outcome.detail["is_fixture"] is True
    # A FIXTURE capture is never presented as real deterministic proof.
    assert outcome.deterministic_evidence is None


def test_verifier_fails_when_model_says_still_broken(tmp_path):
    verifier, _validator, workspace, finding_id = _verifier(
        tmp_path,
        det_observation=_observation(in_viewport=True),
        vlm=MockVlmVerifier("not_resolved"),
    )

    outcome = verifier.verify(finding_id, workspace.workspace_id)

    assert outcome.passed is False
    assert outcome.detail["status"] == "failed"


def test_verifier_fails_when_geometry_still_shows_the_defect(tmp_path):
    """Deterministic failure + model success must end failed."""
    verifier, _validator, workspace, finding_id = _verifier(
        tmp_path,
        det_observation=_observation(in_viewport=False, box_width=520.0),
        vlm=MockVlmVerifier("resolved"),
    )

    outcome = verifier.verify(finding_id, workspace.workspace_id)

    assert outcome.passed is False
    assert outcome.detail["deterministic"]["status"] == "fail"
    assert outcome.detail["vlm"]["verdict"] == "resolved"


def test_verifier_is_inconclusive_on_inconclusive_model(tmp_path):
    verifier, _validator, workspace, finding_id = _verifier(
        tmp_path,
        det_observation=_observation(in_viewport=True),
        vlm=MockVlmVerifier("inconclusive"),
    )

    outcome = verifier.verify(finding_id, workspace.workspace_id)

    assert outcome.passed is False
    assert outcome.detail["status"] == "inconclusive"


def test_verifier_reports_a_missing_workspace(tmp_path):
    verifier, _validator, _workspace, finding_id = _verifier(
        tmp_path,
        det_observation=_observation(in_viewport=True),
        vlm=MockVlmVerifier("resolved"),
    )

    outcome = verifier.verify(finding_id, "ws_" + "0" * 32)

    assert outcome.passed is False
    assert "workspace" in outcome.summary


def test_validator_reports_the_configured_result(tmp_path):
    _patch_verifier, validator, workspace, _finding_id = _verifier(
        tmp_path,
        det_observation=_observation(),
        vlm=MockVlmVerifier("resolved"),
        commands=[_python("raise SystemExit(4)")],
    )

    outcome = validator.validate(workspace.workspace_id)

    assert outcome.passed is False
    assert outcome.detail["status"] == "failed"


# ---------------------------------------------------------------------------
# acceptance: build passes, defect remains
# ---------------------------------------------------------------------------


class _Engine:
    """Minimal action engine double for the orchestrator acceptance test."""

    def __init__(self, record: PatchProposalRecord) -> None:
        from app.ai.adapters.patch import MockPatchGenerator

        self.generator = MockPatchGenerator()
        self.record = record

    def propose(self, finding_id: str) -> PatchProposalRecord:
        return self.record


class _Applier:
    """Applies nothing; returns a real workspace id from the workspace manager."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id

    def apply(self, record, proposal, *, attempt_id: str | None = None):
        from app.orchestrator import ApplyResult

        return ApplyResult(
            workspace_id=self.workspace_id, summary="TEST apply", diff="+fixed\n"
        )

    def rollback(self, workspace_id: str):
        """The W3.4 seam: the workspace was never written here, so it is clean."""
        from app.orchestrator import RollbackResult

        return RollbackResult(
            confirmed=True, summary="TEST baseline already matched"
        )


def test_build_passes_but_defect_remains_ends_verification_failed(tmp_path):
    verifier, validator, workspace, finding_id = _verifier(
        tmp_path,
        det_observation=_observation(in_viewport=False, box_width=520.0),
        vlm=MockVlmVerifier("resolved"),
        commands=[_python("print('build ok')")],
    )

    scan_store = ScanStore()
    scan = scan_store.create(
        type(
            "Req",
            (),
            {
                "target_url": "https://shoplite.test",
                "viewports": [_MOBILE],
                "routes": ["/checkout"],
            },
        )()
    )
    for to_status in (
        ScanStatus.evidence_collection,
        ScanStatus.detecting,
        ScanStatus.detected,
    ):
        scan = scan_store.compare_and_transition(
            scan.scan_id, expected=frozenset({scan.status}), to_status=to_status
        )
    scan_store.compare_and_transition(
        scan.scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )

    detection_store = DetectionStore()
    detection_store.replace_findings(
        scan.scan_id,
        [_finding_record_for(scan.scan_id, finding_id)],
    )
    patch_store = PatchStore()
    proposal = PatchProposal(
        finding_id=finding_id,
        files=[
            PatchFile(
                path="styles/checkout.css",
                edits=[PatchEdit(old_text="color: #111;", new_text="color: #222;")],
            )
        ],
        rationale="TEST proposal",
        generator_label="MOCK",
    )
    record = PatchProposalRecord(
        proposal_id=new_id("prop"),
        finding_id=finding_id,
        scan_id=scan.scan_id,
        status=PatchProposalStatus.proposed,
        generator_label="MOCK",
    )
    patch_store.add(record, proposal)

    orchestrator = Orchestrator(
        settings=_settings(
            patch_source_root=str(tmp_path / "source"),
            validation_commands=[_python("print('build ok')")],
            workspace_root=tmp_path / "ws",
        ),
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=AttemptStore(),
        action_engine=_Engine(record),
        applier=_Applier(workspace.workspace_id),
        validator=validator,
        verifier=verifier,
    )
    attempts = orchestrator.run_scan(scan.scan_id)

    attempt = attempts[0]
    # The build passed …
    assert attempt.validation["status"] == "passed"
    # … but the defect is measurable in the fresh evidence, so it is NOT
    # verified: the attempt is rolled back (W3.4) and the reason is recorded.
    assert attempt.status is AttemptStatus.rolled_back
    assert attempt.failure_code == "verification_failed"
    assert attempt.verification["status"] == "failed"
    assert attempt.rollback["confirmed"] is True
    # The budget was spent on retries, so the scan needs a human.
    assert scan_store.get(scan.scan_id).status is ScanStatus.escalated


def _finding_record_for(scan_id, finding_id: str) -> FindingRecord:
    return FindingRecord(
        finding=Finding(
            finding_id=finding_id,
            category=FindingCategory.clipped_element,
            target_candidate="#place-order",
            observation="MOCK finding used by the acceptance test.",
            severity=Severity.high,
            confidence=0.9,
            status=FindingStatus.detected,
        ),
        scan_id=scan_id,
        adapter_label="MOCK",
        is_mock=True,
        route="/checkout",
        viewport=_MOBILE,
        screenshot_artifact_ids=[new_uuid()],
    )


# ---------------------------------------------------------------------------
# optional: the real Playwright collector against a copy of ShopLite
# ---------------------------------------------------------------------------


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001 - environment probe only
        return False


@pytest.mark.skipif(
    not _DEMO_ROOT.is_dir(), reason="the ShopLite demo tree is not present"
)
@pytest.mark.skipif(
    not _chromium_available(), reason="no Playwright Chromium on this host"
)
def test_real_capture_finds_the_shoplite_defect(tmp_path):
    """REAL browser run: serve a copy of ShopLite and measure the clipped button."""
    import shutil

    source = tmp_path / "source"
    shutil.copytree(_DEMO_ROOT, source)
    settings = _settings(
        patch_source_root=str(source),
        approved_patch_paths=["styles/checkout.css"],
        workspace_root=tmp_path / "ws",
        artifacts_dir=tmp_path / "artifacts",
        serve_ready_timeout_seconds=15,
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    collector = ShopLiteCheckoutCollector(
        settings=settings, evidence_store=EvidenceStore(artifacts_root=artifacts)
    )

    result = collector.capture(
        scan_id=new_uuid(), source_dir=source, viewports=[_MOBILE], attempt_id="attempt_test"
    )

    assert len(result.screens) == 2
    assert result.is_fixture is False
    mobile = result.screen_for(_MOBILE)
    assert mobile is not None
    element = next(e for e in mobile.observation.elements if e.selector == "#place-order")
    # The deliberate defect: the 520px button overflows a 390px viewport.
    assert element.in_viewport is False
    assert element.box.x + element.box.w > mobile.viewport.width

    # The stored observation round-trips through the project schema.
    observation_file = (
        artifacts / "verification" / "attempt_test" / "demo-checkout" / "mobile" / "observation.json"
    )
    assert load_observation(observation_file).route == "/checkout"


@pytest.mark.skipif(
    not _DEMO_ROOT.is_dir(), reason="the ShopLite demo tree is not present"
)
@pytest.mark.skipif(
    not _chromium_available(), reason="no Playwright Chromium on this host"
)
def test_real_patch_flips_the_deterministic_check(tmp_path):
    """REAL loop: patch the copied demo, re-capture, and re-measure the same defect."""
    import shutil

    from app.patch_apply import SourcePatchApplier

    source = tmp_path / "source"
    shutil.copytree(_DEMO_ROOT, source)
    settings = _settings(
        patch_source_root=str(source),
        approved_patch_paths=["styles/checkout.css"],
        workspace_root=tmp_path / "ws",
        artifacts_dir=tmp_path / "artifacts",
        serve_ready_timeout_seconds=15,
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    evidence_store = EvidenceStore(artifacts_root=artifacts)
    workspaces = WorkspaceManager(settings=settings)
    workspace = workspaces.create()
    collector = ShopLiteCheckoutCollector(
        settings=settings, evidence_store=evidence_store
    )

    # The defect is measurable BEFORE the patch.
    before = collector.capture(
        scan_id=new_uuid(),
        source_dir=workspace.source,
        viewports=[_MOBILE],
        attempt_id="attempt_before",
    )
    before_check = deterministic_check(_finding(), before, viewport=_MOBILE)
    assert before_check.status is CheckStatus.failed

    # Apply the real fix through the real applier.
    proposal = PatchProposal(
        finding_id="finding-real",
        files=[
            PatchFile(
                path="styles/checkout.css",
                edits=[
                    PatchEdit(
                        old_text="#place-order { width: 520px;",
                        new_text="#place-order { width: min(100%, 520px);",
                    )
                ],
            )
        ],
        rationale="TEST proposal: unclamp the checkout button width.",
        generator_label="TEST",
    )
    record = PatchProposalRecord(
        proposal_id=new_id("prop"),
        finding_id="finding-real",
        status=PatchProposalStatus.proposed,
        generator_label="TEST",
    )
    applier = SourcePatchApplier(settings=settings, workspace_manager=workspaces)
    applied = applier.apply(record, proposal, attempt_id=workspace.attempt_id)
    patched = workspaces.get(applied.workspace_id)

    # … and it is gone AFTER the patch, measured from the patched copy.
    after = collector.capture(
        scan_id=new_uuid(),
        source_dir=patched.source,
        viewports=[_MOBILE],
        attempt_id="attempt_after",
    )
    after_check = deterministic_check(_finding(), after, viewport=_MOBILE)
    assert after_check.status is CheckStatus.passed
    assert before_check.status is not after_check.status
