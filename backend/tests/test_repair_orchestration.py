"""W3.1 tests: the self-healing orchestration loop (fakes for every component).

What is proven here:

* the attempt transition table is enforced (every allowed move, a sample of
  illegal ones, and that no failure can ever reach ``verified``);
* each injected stage failure ends the attempt in the matching failed status
  with the error recorded, and the scan is NOT reported as verified/review;
* a passing build plus a "verified" claim without deterministic evidence is
  rejected — a build succeeding does not prove the visual defect is gone;
* cancellation between steps is honoured;
* a second repair request cannot start while one is in flight;
* a process restart settles scans left in ``repairing`` (nothing sticks).

Every component below is a clearly labelled TEST DOUBLE: no patch is applied to
a real tree and no model is called. The real PatchApplier / Validator /
Verifier implementations are PENDING (W3.2–W3.4).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.ai.adapters.patch import MockPatchGenerator
from app.ai.contracts import Finding, FindingCategory, FindingStatus, Severity
from app.config import Settings
from app.detection_schemas import FindingRecord
from app.errors import DomainError, ErrorCode
from app.ids import new_id, utcnow
from app import main
from app.orchestrator import (
    ApplyResult,
    AttemptRecord,
    AttemptStatus,
    AttemptStore,
    Orchestrator,
    RollbackResult,
    StageOutcome,
    ensure_deliverable,
    get_attempt_store,
    is_legal_attempt_transition,
    recover_orphaned_scans,
    transition_attempt,
)
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
)
from app.routers import repair
from app.patch_storage import get_patch_store
from app.detection_storage import get_detection_store
from app.evidence_storage import get_evidence_store
from app.event_storage import get_event_store
from app.schemas import ScanCreateRequest, ScanStatus
from app.storage import get_store

_DETECTED_PATH = (
    ScanStatus.evidence_collection,
    ScanStatus.detecting,
    ScanStatus.detected,
)

#: Scan states that must never be produced by a failed attempt.
_SUCCESS_ONLY_SCAN_STATES = {ScanStatus.patch_verified, ScanStatus.ready_for_review}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    values: dict = {
        "enable_playwright_worker": False,
        "enable_vlm_detection": False,
        "enable_patch_engine": False,
        "enable_github_delivery": False,
        "enable_repair_orchestrator": True,
        "max_repair_attempts": 3,
    }
    values.update(overrides)
    return Settings(**values)


def _seed_scan(scan_store, *, status: ScanStatus = ScanStatus.detected) -> UUID:
    """Create a scan and walk it to ``status`` through legal transitions only."""
    scan = scan_store.create(
        ScanCreateRequest(target_url="https://shoplite.test", routes=["/checkout"])
    )
    if status is ScanStatus.detected:
        for to_status in _DETECTED_PATH:
            scan = scan_store.compare_and_transition(
                scan.scan_id,
                expected=frozenset({scan.status}),
                to_status=to_status,
            )
    return scan.scan_id


def _seed_finding(detection_store, scan_id: UUID) -> str:
    finding = Finding(
        category=FindingCategory.clipped_element,
        target_candidate="#checkout-submit",
        observation="MOCK finding used to drive the orchestrator tests.",
        severity=Severity.high,
        confidence=0.9,
        status=FindingStatus.detected,
    )
    detection_store.replace_findings(
        scan_id,
        [
            FindingRecord(
                finding=finding,
                scan_id=scan_id,
                adapter_label="MOCK",
                is_mock=True,
                route="/checkout",
            )
        ],
    )
    return finding.finding_id


def _proposal_pair(finding_id: str) -> tuple[PatchProposalRecord, PatchProposal]:
    """A MOCK proposal record plus the payload the applier would receive."""
    proposal = PatchProposal(
        finding_id=finding_id,
        files=[
            PatchFile(
                path="styles/checkout.css",
                edits=[PatchEdit(old_text="width: 220px;", new_text="width: 100%;")],
            )
        ],
        rationale="MOCK proposal: unclamp the submit button width.",
        generator_label="MOCK",
    )
    record = PatchProposalRecord(
        proposal_id=new_id("prop"),
        finding_id=finding_id,
        status=PatchProposalStatus.proposed,
        generator_label="MOCK",
        rationale=proposal.rationale,
    )
    return record, proposal


class FakeActionEngine:
    """TEST DOUBLE for the W2 ``ActionEngine`` (proposal generation only)."""

    def __init__(self, outcome: PatchProposalRecord | Exception) -> None:
        self.generator = MockPatchGenerator()
        self.outcome = outcome
        self.calls: list[str] = []
        self.prior_failures: list[str | None] = []

    def propose(
        self, finding_id: str, *, prior_failure: str | None = None
    ) -> PatchProposalRecord:
        self.calls.append(finding_id)
        self.prior_failures.append(prior_failure)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeApplier:
    """TEST DOUBLE for the REAL ``PatchApplier`` (apply + rollback)."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        on_apply=None,
        rollback_error: Exception | None = None,
        rollback_confirmed: bool = True,
    ) -> None:
        self.error = error
        self.on_apply = on_apply
        self.rollback_error = rollback_error
        self.rollback_confirmed = rollback_confirmed
        self.calls: list[str] = []
        self.attempt_ids: list[str | None] = []
        self.rollback_calls: list[str] = []

    def apply(self, record, proposal, *, attempt_id: str | None = None) -> ApplyResult:
        self.calls.append(record.proposal_id)
        self.attempt_ids.append(attempt_id)
        if self.on_apply is not None:
            self.on_apply(record, proposal)
        if self.error is not None:
            raise self.error
        return ApplyResult(
            workspace_id="ws-fake-1",
            summary="MOCK workspace prepared (no real apply)",
            diff="--- baseline/x\n+++ workspace/x\n@@ -1 +1 @@\n-MOCK\n+patched\n",
        )

    def rollback(self, workspace_id: str) -> RollbackResult:
        self.rollback_calls.append(workspace_id)
        if self.rollback_error is not None:
            raise self.rollback_error
        if not self.rollback_confirmed:
            return RollbackResult(
                confirmed=False,
                summary="MOCK baseline could not be verified",
            )
        return RollbackResult(
            confirmed=True,
            summary="MOCK baseline restored; every recorded hash matches",
            detail={"checked_paths": 1},
        )


class FakeValidator:
    """TEST DOUBLE for the PENDING ``Validator``."""

    def __init__(self, outcome: StageOutcome | None = None, error: Exception | None = None) -> None:
        self.outcome = outcome or StageOutcome(
            passed=True, summary="MOCK build_passed (fake validator)"
        )
        self.error = error
        self.calls: list[str] = []

    def validate(self, workspace_id: str) -> StageOutcome:
        self.calls.append(workspace_id)
        if self.error is not None:
            raise self.error
        return self.outcome.model_copy(
            update={"detail": {"status": self.outcome.summary, "commands": []}}
        )


class FakeVerifier:
    """TEST DOUBLE for the PENDING ``Verifier``."""

    def __init__(self, outcome: StageOutcome | None = None, error: Exception | None = None) -> None:
        self.outcome = outcome or StageOutcome(
            passed=True,
            summary="MOCK defect_absent",
            deterministic_evidence="MOCK geometry: submit button fully inside 390x844",
        )
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def verify(self, finding_id: str, workspace_id: str) -> StageOutcome:
        self.calls.append((finding_id, workspace_id))
        if self.error is not None:
            raise self.error
        return self.outcome.model_copy(
            update={
                "detail": {
                    "status": "verified" if self.outcome.passed else "failed",
                    "is_fixture": True,
                }
            }
        )


def _orchestrator(
    *,
    scan_store,
    detection_store,
    patch_store,
    attempt_store,
    settings: Settings | None = None,
    action_engine=None,
    applier=None,
    validator=None,
    verifier=None,
    clock=None,
) -> Orchestrator:
    extra = {} if clock is None else {"clock": clock}
    return Orchestrator(
        settings=settings or _settings(),
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=action_engine or FakeActionEngine(_proposal_pair("unused")[0]),
        applier=applier or FakeApplier(),
        validator=validator or FakeValidator(),
        verifier=verifier or FakeVerifier(),
        **extra,
    )


# ---------------------------------------------------------------------------
# transition table (table-driven)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current,allowed",
    [
        (AttemptStatus.detected, AttemptStatus.patch_proposed),
        (AttemptStatus.detected, AttemptStatus.failed_generate),
        (AttemptStatus.detected, AttemptStatus.cancelled),
        (AttemptStatus.patch_proposed, AttemptStatus.patch_applied),
        (AttemptStatus.patch_proposed, AttemptStatus.failed_apply),
        (AttemptStatus.patch_applied, AttemptStatus.validation_passed),
        (AttemptStatus.patch_applied, AttemptStatus.validation_failed),
        (AttemptStatus.validation_passed, AttemptStatus.verified),
        (AttemptStatus.validation_passed, AttemptStatus.verification_failed),
        (AttemptStatus.validation_failed, AttemptStatus.rolled_back),
        (AttemptStatus.verification_failed, AttemptStatus.rolled_back),
        (AttemptStatus.detected, AttemptStatus.escalated),
        (AttemptStatus.patch_applied, AttemptStatus.escalated),
    ],
)
def test_legal_attempt_transitions(current, allowed):
    assert is_legal_attempt_transition(current, allowed)
    assert transition_attempt(current, allowed) is allowed


@pytest.mark.parametrize(
    "current,target",
    [
        # Fast-forwarding the pipeline is illegal.
        (AttemptStatus.detected, AttemptStatus.verified),
        (AttemptStatus.detected, AttemptStatus.patch_applied),
        (AttemptStatus.patch_proposed, AttemptStatus.validation_passed),
        # No failure can ever resolve into success.
        (AttemptStatus.failed_generate, AttemptStatus.verified),
        (AttemptStatus.failed_apply, AttemptStatus.verified),
        (AttemptStatus.validation_failed, AttemptStatus.verified),
        (AttemptStatus.verification_failed, AttemptStatus.verified),
        (AttemptStatus.cancelled, AttemptStatus.verified),
        (AttemptStatus.escalated, AttemptStatus.verified),
        # End states are end states.
        (AttemptStatus.verified, AttemptStatus.patch_applied),
        (AttemptStatus.rolled_back, AttemptStatus.validation_passed),
    ],
)
def test_illegal_attempt_transitions_raise_409(current, target):
    assert not is_legal_attempt_transition(current, target)
    with pytest.raises(DomainError) as exc:
        transition_attempt(current, target)
    assert exc.value.code is ErrorCode.ILLEGAL_TRANSITION
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------------


def test_successful_run_verifies_and_moves_scan_to_review(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    record, proposal = _proposal_pair(finding_id)
    patch_store.add(record, proposal)
    attempt_store = AttemptStore()

    applier = FakeApplier()
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=FakeActionEngine(record),
        applier=applier,
    )
    # Claim the scan exactly as the endpoint does.
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    attempts = orchestrator.run_scan(scan_id)

    assert len(attempts) == 1
    attempt = attempts[0]
    # The applier is told which attempt it is staging a workspace for.
    assert applier.attempt_ids == [attempt.attempt_id]
    assert attempt.status is AttemptStatus.verified
    assert attempt.n == 1
    assert attempt.patch_proposal_id == record.proposal_id
    assert attempt.workspace_id == "ws-fake-1"
    # The workspace is created per attempt, and its diff is recorded.
    assert attempt.attempt_id.startswith("attempt_")
    assert attempt.diff is not None and "+++ workspace/x" in attempt.diff
    assert attempt.build_result == "MOCK build_passed (fake validator)"
    assert attempt.verification_result == "MOCK defect_absent"
    assert attempt.error is None
    assert attempt.ended_at is not None
    # MOCK provenance is carried, never implied real inference.
    assert attempt.generator_label == "MOCK"

    scan = scan_store.get(scan_id)
    assert scan.status is ScanStatus.review_preparation
    assert scan.status not in _SUCCESS_ONLY_SCAN_STATES


def test_run_finding_is_a_single_bounded_attempt(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    record, proposal = _proposal_pair(finding_id)
    patch_store.add(record, proposal)
    attempt_store = AttemptStore()
    engine = FakeActionEngine(record)

    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=engine,
    )
    first = orchestrator.run_finding(finding_id)
    second = orchestrator.run_finding(finding_id)

    assert first.status is AttemptStatus.verified
    assert first.n == 1
    # A second explicit call is a NEW numbered attempt, not a silent retry of
    # the first: the bounded retry loop itself is W3.4.
    assert second.n == 2
    assert len(attempt_store.list_for_finding(finding_id)) == 2
    assert engine.calls == [finding_id, finding_id]


# ---------------------------------------------------------------------------
# failure at each stage
# ---------------------------------------------------------------------------


def _run_failing(scan_store, detection_store, patch_store, **kwargs):
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    record, proposal = _proposal_pair(finding_id)
    patch_store.add(record, proposal)
    attempt_store = AttemptStore()
    action_engine = kwargs.pop("action_engine", None) or FakeActionEngine(record)
    # One attempt per test: the per-stage assertions are about a single failure,
    # and the retry ladder has its own tests below.
    kwargs.setdefault("settings", _settings(max_repair_attempts=1))
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=action_engine,
        **kwargs,
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    attempts = orchestrator.run_scan(scan_id)
    return scan_store.get(scan_id), attempts, attempt_store


@pytest.mark.parametrize(
    "kwargs,expected_status,fragment",
    [
        (
            {"applier": FakeApplier(error=RuntimeError("workspace unavailable"))},
            AttemptStatus.failed_apply,
            "workspace unavailable",
        ),
        (
            {
                "validator": FakeValidator(
                    StageOutcome(passed=False, summary="build failed: 2 errors")
                )
            },
            # A failed step is rolled back, so the END status is ``rolled_back``.
            AttemptStatus.rolled_back,
            "build failed",
        ),
        (
            {
                "verifier": FakeVerifier(
                    StageOutcome(
                        passed=False,
                        summary="defect still visible at 390x844",
                        deterministic_evidence="MOCK geometry: still clipped by 18px",
                    )
                )
            },
            AttemptStatus.rolled_back,
            "still visible",
        ),
        (
            {"verifier": FakeVerifier(error=RuntimeError("verifier crashed"))},
            AttemptStatus.rolled_back,
            "verifier crashed",
        ),
        (
            {"validator": FakeValidator(error=RuntimeError("container died"))},
            AttemptStatus.rolled_back,
            "container died",
        ),
    ],
)
def test_stage_failure_settles_attempt_and_never_verifies(
    scan_store, detection_store, patch_store, kwargs, expected_status, fragment
):
    scan, attempts, attempt_store = _run_failing(
        scan_store, detection_store, patch_store, **kwargs
    )

    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.status is expected_status
    assert attempt.status is not AttemptStatus.verified
    assert attempt.error is not None
    assert fragment in attempt.error
    assert attempt.ended_at is not None
    # The one-attempt budget is now spent, so the scan goes to a human: never
    # verified, never awaiting review, and never deliverable.
    assert scan.status is ScanStatus.escalated
    assert scan.status not in _SUCCESS_ONLY_SCAN_STATES
    assert scan.status is not ScanStatus.review_preparation
    assert attempt_store.has_in_flight(scan.scan_id) is False
    with pytest.raises(DomainError) as refused:
        ensure_deliverable(scan.status)
    assert refused.value.code is ErrorCode.REPAIR_NOT_DELIVERABLE


def test_verification_without_deterministic_evidence_is_not_a_fix(
    scan_store, detection_store, patch_store
):
    """A passing build + a bare "fixed" claim must not become ``verified``."""
    scan, attempts, _ = _run_failing(
        scan_store,
        detection_store,
        patch_store,
        verifier=FakeVerifier(
            StageOutcome(passed=True, summary="model says the defect is gone")
        ),
    )

    attempt = attempts[0]
    assert attempt.status is AttemptStatus.rolled_back
    assert "without deterministic evidence" in attempt.error
    assert attempt.failure_code == "no_deterministic_evidence"
    assert scan.status not in _SUCCESS_ONLY_SCAN_STATES


def test_rejected_proposal_is_a_generate_failure(scan_store, detection_store, patch_store):
    finding_id = "placeholder"
    rejected = PatchProposalRecord(
        proposal_id=new_id("prop"),
        finding_id=finding_id,
        status=PatchProposalStatus.rejected,
        generator_label="MOCK",
        rejection_reasons=["path_not_allowed"],
    )
    scan, attempts, _ = _run_failing(
        scan_store,
        detection_store,
        patch_store,
        action_engine=FakeActionEngine(rejected),
    )

    attempt = attempts[0]
    assert attempt.status is AttemptStatus.failed_generate
    assert "path_not_allowed" in attempt.error
    # The helper runs with a one-attempt budget, so the scan escalates for a
    # human rather than retrying the same rejected proposal.
    assert scan.status is ScanStatus.escalated


def test_proposal_domain_error_is_a_generate_failure(
    scan_store, detection_store, patch_store
):
    error = DomainError(
        ErrorCode.FINDING_NOT_PATCHABLE,
        "finding is already 'patch_proposed'",
        status_code=409,
    )
    scan, attempts, _ = _run_failing(
        scan_store,
        detection_store,
        patch_store,
        action_engine=FakeActionEngine(error),
    )

    assert attempts[0].status is AttemptStatus.failed_generate
    assert "already 'patch_proposed'" in attempts[0].error
    assert scan.status is ScanStatus.escalated


def test_missing_proposal_payload_fails_apply(scan_store, detection_store, patch_store):
    """A record without its payload must not be applied or reported as verified."""
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    record, _proposal = _proposal_pair(finding_id)
    # Deliberately do NOT store the payload.
    attempt_store = AttemptStore()
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=FakeActionEngine(record),
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    attempts = orchestrator.run_scan(scan_id)

    assert attempts[0].status is AttemptStatus.failed_apply
    assert "no stored patch payload" in attempts[0].error
    # Retries cannot help a missing payload, so the budget drains and the scan
    # is escalated instead of being left retryable forever.
    assert scan_store.get(scan_id).status is ScanStatus.escalated


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------


def test_cancellation_between_steps_ends_attempt_cancelled(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    record, proposal = _proposal_pair(finding_id)
    patch_store.add(record, proposal)
    attempt_store = AttemptStore()

    def cancel_scan(_record, _proposal):
        scan_store.compare_and_transition(
            scan_id,
            expected=frozenset({ScanStatus.repairing}),
            to_status=ScanStatus.cancelled,
        )

    validator = FakeValidator()
    verifier = FakeVerifier()
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=FakeActionEngine(record),
        applier=FakeApplier(on_apply=cancel_scan),
        validator=validator,
        verifier=verifier,
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    attempts = orchestrator.run_scan(scan_id)

    attempt = attempts[0]
    assert attempt.status is AttemptStatus.cancelled
    assert "cancelled" in attempt.error
    # Steps after the cancel point never ran.
    assert validator.calls == []
    assert verifier.calls == []
    # The scan stays cancelled: the orchestrator never reopens it.
    assert scan_store.get(scan_id).status is ScanStatus.cancelled


# ---------------------------------------------------------------------------
# watchdog
# ---------------------------------------------------------------------------


def test_watchdog_escalates_a_run_that_overruns_its_deadline(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    _seed_finding(detection_store, scan_id)
    attempt_store = AttemptStore()
    # Deadline is fixed on the first read; the loop's read then shows it blown.
    ticks = iter([0.0, 10.0, 10.0, 10.0])
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        settings=_settings(repair_run_timeout_seconds=1.0),
        clock=lambda: next(ticks),
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    attempts = orchestrator.run_scan(scan_id)

    assert attempts == []
    scan = scan_store.get(scan_id)
    assert scan.status is ScanStatus.escalated
    assert "watchdog" in scan.error


def test_no_patchable_findings_escalates_instead_of_claiming_success(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    attempt_store = AttemptStore()
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )

    assert orchestrator.run_scan(scan_id) == []
    scan = scan_store.get(scan_id)
    assert scan.status is ScanStatus.escalated
    assert "no findings remained" in scan.error


# ---------------------------------------------------------------------------
# crash recovery
# ---------------------------------------------------------------------------


def _age_scan(scan_store, scan_id: UUID, *, hours: int = 2) -> None:
    current = scan_store.get(scan_id)
    scan_store.replace(
        current.model_copy(update={"updated_at": utcnow() - timedelta(hours=hours)})
    )


def test_restart_settles_a_scan_left_in_repairing(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    attempt_store = AttemptStore()
    # An attempt that was mid-flight when the process died.
    orphan = attempt_store.add(
        AttemptRecord(
            attempt_id=new_id("attempt"),
            finding_id=finding_id,
            scan_id=scan_id,
            n=1,
            status=AttemptStatus.validation_passed,
            started_at=utcnow(),
            workspace_id="ws-orphan",
        )
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    _age_scan(scan_store, scan_id)

    recovered = recover_orphaned_scans(
        scan_store=scan_store,
        attempt_store=attempt_store,
        settings=_settings(repair_stuck_after_seconds=60),
    )

    assert recovered == [scan_id]
    assert scan_store.get(scan_id).status is ScanStatus.detected
    assert attempt_store.get(orphan.attempt_id).status is AttemptStatus.interrupted
    assert attempt_store.get(orphan.attempt_id).failure_code == "interrupted"
    # Nothing is left running anywhere.
    assert all(scan.status is not ScanStatus.repairing for scan in scan_store.list())


def test_recovery_leaves_a_fresh_repairing_scan_alone(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )

    recovered = recover_orphaned_scans(
        scan_store=scan_store,
        attempt_store=AttemptStore(),
        settings=_settings(repair_stuck_after_seconds=900),
    )

    assert recovered == []
    assert scan_store.get(scan_id).status is ScanStatus.repairing


def test_recovery_escalates_when_attempts_are_exhausted(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    attempt_store = AttemptStore()
    record, _proposal = _proposal_pair(finding_id)
    # max_repair_attempts=3, so three recorded attempts exhaust the budget.
    for index in range(3):
        attempt_store.add(
            AttemptRecord(
                attempt_id=new_id("attempt"),
                finding_id=finding_id,
                scan_id=scan_id,
                n=index + 1,
                status=AttemptStatus.failed_apply,
                started_at=utcnow(),
                ended_at=utcnow(),
                patch_proposal_id=record.proposal_id,
            )
        )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    _age_scan(scan_store, scan_id)

    recover_orphaned_scans(
        scan_store=scan_store,
        attempt_store=attempt_store,
        settings=_settings(repair_stuck_after_seconds=60, max_repair_attempts=3),
    )

    scan = scan_store.get(scan_id)
    assert scan.status is ScanStatus.escalated
    assert "exhausted" in scan.error


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def repair_attempt_store() -> AttemptStore:
    """The attempt store the endpoint and the orchestrator must share."""
    return AttemptStore()


@pytest.fixture()
def repair_client(
    scan_store,
    event_store,
    evidence_store,
    detection_store,
    patch_store,
    repair_attempt_store,
):
    """TestClient with the repair router wired to isolated stores."""

    def _build(orchestrator=None) -> TestClient:
        main.app.dependency_overrides[get_store] = lambda: scan_store
        main.app.dependency_overrides[get_event_store] = lambda: event_store
        main.app.dependency_overrides[get_evidence_store] = lambda: evidence_store
        main.app.dependency_overrides[get_detection_store] = lambda: detection_store
        main.app.dependency_overrides[get_patch_store] = lambda: patch_store
        main.app.dependency_overrides[get_attempt_store] = (
            lambda: repair_attempt_store
        )
        main.app.dependency_overrides[main.get_settings] = lambda: _settings()
        main.app.dependency_overrides[repair.get_orchestrator] = lambda: orchestrator
        return TestClient(main.app, raise_server_exceptions=False)

    yield _build
    main.app.dependency_overrides.clear()


def test_repair_is_refused_when_components_are_not_wired(repair_client, scan_store):
    scan_id = _seed_scan(scan_store)
    response = repair_client(None).post(f"/api/v1/scans/{scan_id}/repair")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPAIR_UNAVAILABLE"
    # The scan was not claimed.
    assert scan_store.get(scan_id).status is ScanStatus.detected


def test_repair_rejects_unknown_scan(repair_client):
    response = repair_client(None).post(
        "/api/v1/scans/00000000-0000-0000-0000-000000000000/repair"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCAN_NOT_FOUND"


def test_repair_requires_a_detected_scan(
    repair_client, scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store, status=ScanStatus.created)
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=AttemptStore(),
    )
    response = repair_client(orchestrator).post(f"/api/v1/scans/{scan_id}/repair")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPAIR_STATE_CONFLICT"
    assert "created" in response.json()["error"]["message"]


def test_repair_requires_patchable_findings(
    repair_client, scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=AttemptStore(),
    )
    response = repair_client(orchestrator).post(f"/api/v1/scans/{scan_id}/repair")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPAIR_NO_PATCHABLE_FINDINGS"


def test_repair_run_end_to_end_through_the_api(
    repair_client, repair_attempt_store, scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    record, proposal = _proposal_pair(finding_id)
    patch_store.add(record, proposal)
    attempt_store = repair_attempt_store
    orchestrator = _orchestrator(
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=FakeActionEngine(record),
    )
    client = repair_client(orchestrator)

    response = client.post(f"/api/v1/scans/{scan_id}/repair")
    assert response.status_code == 202
    body = response.json()
    assert body["scan_status"] == "repairing"
    assert body["finding_ids"] == [finding_id]

    # TestClient runs the background task before responding to us, so the run
    # has already settled and the attempts are readable.
    assert scan_store.get(scan_id).status is ScanStatus.review_preparation

    history = client.get(f"/api/v1/scans/{scan_id}/attempts")
    assert history.status_code == 200
    payload = history.json()
    assert payload["scan_id"] == str(scan_id)
    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["status"] == "verified"
    assert payload["attempts"][0]["generator_label"] == "MOCK"
    # The structured validation/verification reports are exposed on the API.
    assert payload["attempts"][0]["validation"] is not None
    assert payload["attempts"][0]["verification"]["status"] == "verified"


def test_attempts_for_unknown_scan_is_404(repair_client):
    response = repair_client(None).get(
        "/api/v1/scans/00000000-0000-0000-0000-000000000000/attempts"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCAN_NOT_FOUND"


def test_duplicate_repair_trigger_runs_only_once(
    repair_client, scan_store, detection_store, patch_store
):
    """A second request while the first is still in flight is a 409."""

    class StuckOrchestrator:
        """TEST DOUBLE: leaves the scan in ``repairing`` (a slow live run)."""

        def __init__(self) -> None:
            self.calls: list[UUID] = []

        def run_scan(self, scan_id: UUID):  # pragma: no cover - never executes
            self.calls.append(scan_id)
            return []

    scan_id = _seed_scan(scan_store)
    _seed_finding(detection_store, scan_id)
    client = repair_client(StuckOrchestrator())

    first = client.post(f"/api/v1/scans/{scan_id}/repair")
    second = client.post(f"/api/v1/scans/{scan_id}/repair")

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "REPAIR_STATE_CONFLICT"


def test_cancel_is_idempotent(repair_client, scan_store):
    scan_id = _seed_scan(scan_store)
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    client = repair_client(None)

    first = client.post(f"/api/v1/scans/{scan_id}/cancel")
    second = client.post(f"/api/v1/scans/{scan_id}/cancel")

    assert first.status_code == 200
    assert first.json() == {
        "scan_id": str(scan_id),
        "scan_status": "cancelled",
        "cancelled": True,
    }
    assert second.status_code == 200
    assert second.json() == first.json()
    assert scan_store.get(scan_id).status is ScanStatus.cancelled


def test_cancel_rejects_a_finished_scan(repair_client, scan_store):
    scan_id = _seed_scan(scan_store)
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.failed,
        error="earlier failure",
    )
    response = repair_client(None).post(f"/api/v1/scans/{scan_id}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ILLEGAL_TRANSITION"


def test_orchestrator_rejects_an_engine_without_a_generator(
    scan_store, detection_store, patch_store
):
    class NoGenerator:
        def propose(self, finding_id: str):  # pragma: no cover - never called
            raise AssertionError("must not be called")

    with pytest.raises(TypeError):
        Orchestrator(
            settings=_settings(),
            scan_store=scan_store,
            detection_store=detection_store,
            patch_store=patch_store,
            attempt_store=AttemptStore(),
            action_engine=NoGenerator(),
            applier=FakeApplier(),
            validator=FakeValidator(),
            verifier=FakeVerifier(),
        )
