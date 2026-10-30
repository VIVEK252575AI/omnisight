"""Bounded retries, rollback and recovery (Week 3, W3.4).

Every test here drives the REAL :class:`app.orchestrator.Orchestrator` through
its retry ladder. Only the injected components are test doubles, and they are
labelled MOCK in their summaries — the loop, the state machine, the rollback
bookkeeping and the escalation logic are the code under test.

Coverage:
* the ladder itself (fail-fail-succeed, fail-fail-fail, budget ceilings);
* a proposal identical to one that already failed is never re-applied;
* every failed attempt is rolled back and the rollback is verified;
* an unverifiable rollback is a hard error that stops retrying;
* restart recovery settles interrupted attempts and restores their workspaces;
* the attempts endpoint exposes failure codes, rollback status and escalation.

Note (Week 5 review context): these tests are fully unchanged from the Week 3
W3.4 deliverable and still pass against the shipped code. They are read by the
Week 5 code review so the reviewer can (a) audit the W3.4 contract and
(b) check that the Week 5 acceptance criteria stack on top of the wired repair
path rather than a separate test-only fake. These tests are not modified for
Week 5 — the review asserts an unbroken relationship, not a re-verification of
Week 3 itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.ai.adapters.patch import MockPatchGenerator
from app.ai.contracts import FindingStatus
from app.config import Settings
from app.evidence_storage import get_evidence_store
from app.ids import new_id, utcnow
from app.orchestrator import (
    AttemptRecord,
    AttemptStatus,
    AttemptStore,
    Orchestrator,
    StageOutcome,
    _attempt_budget,
    ensure_deliverable,
    get_attempt_store,
    recover_orphaned_scans,
)
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
)
from app.patch_storage import get_patch_store
from app.schemas import ScanStatus
from app.storage import get_store
from app.errors import DomainError, ErrorCode
from app.patch_apply import SourcePatchApplier
from app.workspace import WorkspaceManager, file_sha256
from tests.test_repair_orchestration import (
    _age_scan,
    _seed_finding,
    _seed_scan,
    _settings,
)
from tests.test_workspace_patch_apply import (
    _mini_tree,
    _proposal,
    _record,
    _settings as _applier_settings,
)


#: Scan states that mean the pipeline claims success.
_SUCCESS_ONLY_SCAN_STATES = {ScanStatus.patch_verified, ScanStatus.ready_for_review}


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------


class ScriptedActionEngine:
    """Proposes a DIFFERENT edit per attempt, or fails a chosen stage.

    A different payload each time is what makes the retry ladder observable:
    identical payloads are deliberately caught by the duplicate-patch guard.
    """

    def __init__(self, patch_store, *, stages: list[str]) -> None:
        self.generator = MockPatchGenerator()
        self.patch_store = patch_store
        self.stages = stages
        self.calls: list[str] = []
        self.prior_failures: list[str | None] = []
        self.current_stage = stages[0]

    def propose(
        self, finding_id: str, *, prior_failure: str | None = None
    ) -> PatchProposalRecord:
        index = len(self.calls)
        self.calls.append(finding_id)
        self.prior_failures.append(prior_failure)
        self.current_stage = self.stages[min(index, len(self.stages) - 1)]

        proposal = PatchProposal(
            finding_id=finding_id,
            files=[
                PatchFile(
                    path="styles/checkout.css",
                    edits=[
                        PatchEdit(
                            old_text=f"width: {220 + index}px;",
                            new_text="width: 100%;",
                        )
                    ],
                )
            ],
            rationale=f"MOCK proposal variant {index}",
            generator_label="MOCK",
        )
        record = PatchProposalRecord(
            proposal_id=new_id("prop"),
            finding_id=finding_id,
            status=PatchProposalStatus.proposed,
            generator_label="MOCK",
            rationale=proposal.rationale,
        )
        self.patch_store.add(record, proposal)
        return record


class ConstantActionEngine:
    """Always proposes the byte-identical edit (duplicate-patch detection)."""

    #: Every attempt must fail validation, so the ladder keeps going and the
    #: duplicate-patch guard is what has to stop the re-application.
    current_stage = "validation_failed"

    def __init__(self, patch_store, *, finding_id: str) -> None:
        self.generator = MockPatchGenerator()
        self.patch_store = patch_store
        self.finding_id = finding_id
        self.calls = 0
        self._record: PatchProposalRecord | None = None

    def propose(
        self, finding_id: str, *, prior_failure: str | None = None
    ) -> PatchProposalRecord:
        self.calls += 1
        if self._record is None:
            proposal = PatchProposal(
                finding_id=finding_id,
                files=[
                    PatchFile(
                        path="styles/checkout.css",
                        edits=[
                            PatchEdit(old_text="width: 220px;", new_text="width: 100%;")
                        ],
                    )
                ],
                rationale="MOCK proposal: always the same edit",
                generator_label="MOCK",
            )
            self._record = PatchProposalRecord(
                proposal_id=new_id("prop"),
                finding_id=finding_id,
                status=PatchProposalStatus.proposed,
                generator_label="MOCK",
                rationale=proposal.rationale,
            )
            self.patch_store.add(self._record, proposal)
        return self._record


class CountingApplier:
    """Records apply/rollback calls; optionally fails a rollback."""

    def __init__(
        self,
        *,
        rollback_error: Exception | None = None,
        rollback_confirmed: bool = True,
    ) -> None:
        self.rollback_error = rollback_error
        self.rollback_confirmed = rollback_confirmed
        self.applied: list[str] = []
        self.rollbacks: list[str] = []

    def apply(self, record, proposal, *, attempt_id: str | None = None):
        from app.orchestrator import ApplyResult

        self.applied.append(record.proposal_id)
        return ApplyResult(
            workspace_id=f"ws-fake-{len(self.applied)}",
            summary="MOCK workspace prepared (no real apply)",
        )

    def rollback(self, workspace_id: str):
        from app.orchestrator import RollbackResult

        self.rollbacks.append(workspace_id)
        if self.rollback_error is not None:
            raise self.rollback_error
        return RollbackResult(
            confirmed=self.rollback_confirmed,
            summary=(
                "MOCK baseline restored; every recorded hash matches"
                if self.rollback_confirmed
                else "MOCK baseline could not be verified"
            ),
            detail={"checked_paths": 1},
        )


class StageValidator:
    """Fails validation while the engine's current stage says so."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.calls: list[str] = []

    def validate(self, workspace_id: str) -> StageOutcome:
        self.calls.append(workspace_id)
        if self.engine.current_stage == "validation_failed":
            return StageOutcome(passed=False, summary="MOCK build failed")
        return StageOutcome(passed=True, summary="MOCK build passed")


class StageVerifier:
    """Fails verification while the engine's current stage says so."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.calls: list[tuple[str, str]] = []

    def verify(self, finding_id: str, workspace_id: str) -> StageOutcome:
        self.calls.append((finding_id, workspace_id))
        if self.engine.current_stage == "verification_failed":
            return StageOutcome(
                passed=False,
                summary="MOCK defect still visible",
                deterministic_evidence="MOCK geometry: still clipped by 18px",
            )
        return StageOutcome(
            passed=True,
            summary="MOCK defect absent",
            deterministic_evidence="MOCK geometry: fully inside 390x844",
        )


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _run(
    scan_store,
    detection_store,
    patch_store,
    *,
    engine,
    applier=None,
    settings=None,
    clock=None,
):
    """Seed one scan + finding, claim it, and run the repair loop."""
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    attempt_store = AttemptStore()
    applier = applier or CountingApplier()
    orchestrator = Orchestrator(
        settings=settings or _settings(),
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=engine,
        applier=applier,
        validator=StageValidator(engine),
        verifier=StageVerifier(engine),
        **({} if clock is None else {"clock": clock}),
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    attempts = orchestrator.run_scan(scan_id)
    return scan_store.get(scan_id), attempts, attempt_store, applier, finding_id


# ---------------------------------------------------------------------------
# the retry ladder
# ---------------------------------------------------------------------------


def test_success_on_the_third_attempt(scan_store, detection_store, patch_store):
    """fail -> fail -> succeed: the third attempt verifies the finding."""
    engine = ScriptedActionEngine(
        patch_store,
        stages=["validation_failed", "validation_failed", "ok"],
    )
    scan, attempts, _, applier, _ = _run(
        scan_store, detection_store, patch_store, engine=engine
    )

    assert [a.n for a in attempts] == [1, 2, 3]
    assert [a.status for a in attempts] == [
        AttemptStatus.rolled_back,
        AttemptStatus.rolled_back,
        AttemptStatus.verified,
    ]
    assert scan.status is ScanStatus.review_preparation
    # Each failed attempt was rolled back; a verified attempt is not.
    assert [a.rollback["confirmed"] for a in attempts[:2]] == [True, True]
    assert attempts[2].rollback is None
    assert applier.rollbacks == ["ws-fake-1", "ws-fake-2"]
    # Only the successful attempt is eligible for delivery.
    ensure_deliverable(scan.status)


def test_prior_failure_context_reaches_the_next_proposal(
    scan_store, detection_store, patch_store
):
    """The retry is informed, not blind: attempt 2 receives attempt 1's failure."""
    engine = ScriptedActionEngine(
        patch_store, stages=["validation_failed", "ok"]
    )
    _run(scan_store, detection_store, patch_store, engine=engine)

    assert engine.prior_failures[0] is None
    second = engine.prior_failures[1]
    assert second is not None
    assert "attempt 1" in second
    assert "validation_failed" in second
    # Bounded text: the cap is a prompt-size guard, not a suggestion.
    assert len(second) <= _settings().repair_prior_failure_max_chars


def test_exhaustion_escalates_and_blocks_delivery(
    scan_store, detection_store, patch_store
):
    """fail-fail-fail: the budget is spent, so a human is asked and delivery stops."""
    engine = ScriptedActionEngine(patch_store, stages=["validation_failed"])
    scan, attempts, attempt_store, applier, finding_id = _run(
        scan_store, detection_store, patch_store, engine=engine
    )

    assert len(attempts) == 3  # default budget
    assert all(a.status is AttemptStatus.rolled_back for a in attempts)
    assert all(a.error for a in attempts)
    assert scan.status is ScanStatus.escalated
    assert scan.error is not None
    assert "validation_failed" in scan.error
    assert "no repair attempts remain" in scan.error
    assert scan.status not in _SUCCESS_ONLY_SCAN_STATES
    assert attempt_store.has_in_flight(scan.scan_id) is False
    assert len(applier.rollbacks) == 3

    # The finding itself leaves ``detected``, so nothing picks it up again: the
    # loop only ever proposes against detected findings.
    escalated = detection_store.get_finding(finding_id)
    assert escalated.finding.status is FindingStatus.escalated

    with pytest.raises(DomainError) as refused:
        ensure_deliverable(scan.status)
    assert refused.value.code is ErrorCode.REPAIR_NOT_DELIVERABLE


def test_attempt_budget_is_never_exceeded(scan_store, detection_store, patch_store):
    """A five-attempt budget runs five attempts — never six."""
    engine = ScriptedActionEngine(patch_store, stages=["validation_failed"])
    scan, attempts, _, _, _ = _run(
        scan_store,
        detection_store,
        patch_store,
        engine=engine,
        settings=_settings(max_repair_attempts=5),
    )

    assert len(attempts) == 5
    assert engine.calls == [attempts[0].finding_id] * 5
    assert scan.status is ScanStatus.escalated


def test_wall_clock_budget_stops_the_ladder(scan_store, detection_store, patch_store):
    """The attempt count is not the only limit: the run has a time budget too."""
    values = iter([0.0, 0.0, 100.0])

    def clock() -> float:
        return next(values, 100.0)

    engine = ScriptedActionEngine(patch_store, stages=["validation_failed"])
    scan, attempts, _, _, _ = _run(
        scan_store,
        detection_store,
        patch_store,
        engine=engine,
        settings=_settings(repair_run_timeout_seconds=1.0),
        clock=clock,
    )

    # One attempt ran, then the watchdog stopped the ladder well before the
    # three-attempt budget.
    assert len(attempts) == 1
    assert attempts[0].status is AttemptStatus.rolled_back
    assert scan.status is ScanStatus.escalated
    assert "watchdog" in scan.error


def test_configured_budget_above_the_ceiling_is_clamped(caplog):
    """The retry budget has a hard ceiling in code, not only in documentation."""
    with caplog.at_level("WARNING", logger="omnisight"):
        settings = Settings(max_repair_attempts=99)

    assert settings.max_repair_attempts == 5
    assert "hard ceiling" in caplog.text
    # And the loop re-clamps, so an unvalidated settings object cannot exceed it.
    unvalidated = Settings.model_construct(max_repair_attempts=99)
    assert _attempt_budget(unvalidated) == 5


def test_zero_attempt_budget_is_still_one_attempt():
    """A nonsense budget can never mean 'skip the checks and call it verified'."""
    unvalidated = Settings.model_construct(max_repair_attempts=0)
    assert _attempt_budget(unvalidated) == 1


# ---------------------------------------------------------------------------
# duplicate proposals
# ---------------------------------------------------------------------------


def test_identical_proposal_is_not_reapplied(scan_store, detection_store, patch_store):
    """A byte-identical retry counts as a failed attempt but applies nothing."""
    finding_scan = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, finding_scan)
    engine = ConstantActionEngine(patch_store, finding_id=finding_id)
    applier = CountingApplier()

    attempt_store = AttemptStore()
    orchestrator = Orchestrator(
        settings=_settings(),
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=engine,
        applier=applier,
        validator=StageValidator(engine),
        verifier=StageVerifier(engine),
    )
    scan_store.compare_and_transition(
        finding_scan,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )

    # The first proposal is identical to none, so it is applied once and fails.
    attempts = orchestrator.run_scan(finding_scan)

    first = attempts[0]
    assert first.status is AttemptStatus.rolled_back
    assert first.failure_code == "validation_failed"
    assert first.proposal_hash is not None
    # Attempts 2 and 3 propose the same digest: recorded, never re-applied.
    for later in attempts[1:]:
        assert later.status is AttemptStatus.failed_generate
        assert later.failure_code == "duplicate_patch"
        assert later.proposal_hash == first.proposal_hash
    assert len(applier.applied) == 1
    assert len(applier.rollbacks) == 1
    assert scan_store.get(finding_scan).status is ScanStatus.escalated


# ---------------------------------------------------------------------------
# rollback failures (hard errors)
# ---------------------------------------------------------------------------


def test_unverified_rollback_stops_retrying_and_fails_the_scan(
    scan_store, detection_store, patch_store
):
    """A rollback that cannot prove the restore is a hard error, not a retry."""
    engine = ScriptedActionEngine(patch_store, stages=["validation_failed"])
    applier = CountingApplier(rollback_confirmed=False)
    scan, attempts, _, applier, _ = _run(
        scan_store, detection_store, patch_store, engine=engine, applier=applier
    )

    assert len(attempts) == 1  # no retry after an unverifiable rollback
    assert attempts[0].status is AttemptStatus.rollback_failed
    assert attempts[0].failure_code == "rollback_incomplete"
    assert attempts[0].rollback["confirmed"] is False
    assert scan.status is ScanStatus.failed
    assert "rollback_incomplete" in scan.error
    assert applier.applied and len(applier.rollbacks) == 1
    with pytest.raises(DomainError):
        ensure_deliverable(scan.status)


def test_rollback_exception_is_a_hard_error(scan_store, detection_store, patch_store):
    """A raising rollback is recorded as unconfirmed, never swallowed."""
    engine = ScriptedActionEngine(patch_store, stages=["validation_failed"])
    applier = CountingApplier(rollback_error=RuntimeError("disk went away"))
    scan, attempts, _, _, _ = _run(
        scan_store, detection_store, patch_store, engine=engine, applier=applier
    )

    assert len(attempts) == 1
    assert attempts[0].status is AttemptStatus.rollback_failed
    assert attempts[0].rollback["confirmed"] is False
    assert "RuntimeError" in attempts[0].rollback["summary"]
    assert scan.status is ScanStatus.failed


class FixedEngine:
    """Proposes one already-stored proposal (real applier, fake generator)."""

    def __init__(self, record: PatchProposalRecord) -> None:
        self.generator = MockPatchGenerator()
        self.record = record

    def propose(
        self, finding_id: str, *, prior_failure: str | None = None
    ) -> PatchProposalRecord:
        return self.record


def test_real_applier_rollback_restores_every_baseline_hash(
    tmp_path, scan_store, detection_store, patch_store
):
    """The REAL applier, driven by the orchestrator, restores the baseline.

    Only the generator/validator/verifier are doubles; the workspace copy, the
    edit, the rollback and the hash check are the shipped code.
    """
    source = _mini_tree(tmp_path / "source")
    applier_settings = _applier_settings(source, tmp_path / "ws")
    applier = SourcePatchApplier(
        settings=applier_settings,
        workspace_manager=WorkspaceManager(
            settings=applier_settings,
            source_root=source,
            workspace_root=tmp_path / "ws",
        ),
    )
    baseline = {
        path.relative_to(source).as_posix(): file_sha256(path)
        for path in sorted(source.rglob("*"))
        if path.is_file()
    }

    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    proposal = _proposal(finding_id=finding_id)
    record = _record(proposal, finding_id=finding_id)
    patch_store.add(record, proposal)

    class FailingValidator:
        def validate(self, workspace_id: str) -> StageOutcome:
            return StageOutcome(passed=False, summary="MOCK build failed")

    attempt_store = AttemptStore()
    orchestrator = Orchestrator(
        settings=_settings(max_repair_attempts=1),
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=FixedEngine(record),
        applier=applier,
        validator=FailingValidator(),
        verifier=StageVerifier(ScriptedActionEngine(patch_store, stages=["ok"])),
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )

    attempts = orchestrator.run_scan(scan_id)

    assert attempts[0].status is AttemptStatus.rolled_back
    assert attempts[0].rollback["confirmed"] is True
    assert attempts[0].rollback["checked_paths"] == len(baseline)
    workspace = applier.workspaces.get(attempts[0].workspace_id)
    assert workspace.status == "rolled_back"
    assert {
        path.relative_to(workspace.source).as_posix(): file_sha256(path)
        for path in sorted(workspace.source.rglob("*"))
        if path.is_file()
    } == baseline
    # The original source tree was never touched by any of this.
    assert {
        path.relative_to(source).as_posix(): file_sha256(path)
        for path in sorted(source.rglob("*"))
        if path.is_file()
    } == baseline


# ---------------------------------------------------------------------------
# crash recovery
# ---------------------------------------------------------------------------


def _orphan(attempt_store, scan_id, finding_id, *, status, workspace_id):
    return attempt_store.add(
        AttemptRecord(
            attempt_id=new_id("attempt"),
            finding_id=finding_id,
            scan_id=scan_id,
            n=1,
            status=status,
            started_at=utcnow(),
            workspace_id=workspace_id,
        )
    )


def test_restart_rolls_back_an_interrupted_attempt(
    scan_store, detection_store, patch_store
):
    """A restart leaves nothing running and restores the orphaned workspace."""
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    attempt_store = AttemptStore()
    orphan = _orphan(
        attempt_store,
        scan_id,
        finding_id,
        status=AttemptStatus.patch_applied,
        workspace_id="ws-orphan",
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    _age_scan(scan_store, scan_id)
    applier = CountingApplier()

    recovered = recover_orphaned_scans(
        scan_store=scan_store,
        attempt_store=attempt_store,
        settings=_settings(repair_stuck_after_seconds=60),
        applier=applier,
    )

    assert recovered == [scan_id]
    settled = attempt_store.get(orphan.attempt_id)
    assert settled.status is AttemptStatus.interrupted
    assert settled.failure_code == "interrupted"
    assert settled.rollback == {
        "confirmed": True,
        "attempted": True,
        "summary": "MOCK baseline restored; every recorded hash matches",
        "checked_paths": 1,
    }
    assert applier.rollbacks == ["ws-orphan"]
    # Budget remains, so the scan is retryable — never verified.
    assert scan_store.get(scan_id).status is ScanStatus.detected
    assert attempt_store.has_in_flight(scan_id) is False


def test_restart_fails_the_scan_when_rollback_cannot_be_verified(
    scan_store, detection_store, patch_store
):
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    attempt_store = AttemptStore()
    _orphan(
        attempt_store,
        scan_id,
        finding_id,
        status=AttemptStatus.validation_passed,
        workspace_id="ws-orphan",
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
        settings=_settings(repair_stuck_after_seconds=60),
        applier=CountingApplier(rollback_confirmed=False),
    )

    scan = scan_store.get(scan_id)
    assert scan.status is ScanStatus.failed
    assert "rollback_incomplete" in scan.error


def test_restart_without_an_applier_records_that_it_could_not_roll_back(
    scan_store, detection_store, patch_store
):
    """No applier is 'not attempted', which must not be mistaken for a failure."""
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    attempt_store = AttemptStore()
    orphan = _orphan(
        attempt_store,
        scan_id,
        finding_id,
        status=AttemptStatus.patch_applied,
        workspace_id="ws-orphan",
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
        settings=_settings(repair_stuck_after_seconds=60),
        applier=None,
    )

    settled = attempt_store.get(orphan.attempt_id)
    assert settled.status is AttemptStatus.interrupted
    assert settled.rollback["attempted"] is False
    assert settled.rollback["confirmed"] is False
    assert scan_store.get(scan_id).status is ScanStatus.detected


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def retry_client(scan_store, event_store, evidence_store, detection_store, patch_store):
    """TestClient with isolated stores and the attempt store shared by both sides."""
    attempt_store = AttemptStore()
    main.app.dependency_overrides[get_store] = lambda: scan_store
    main.app.dependency_overrides[get_evidence_store] = lambda: evidence_store
    main.app.dependency_overrides[get_patch_store] = lambda: patch_store
    main.app.dependency_overrides[get_attempt_store] = lambda: attempt_store
    # Same budget the run used, so ``attempts_remaining`` is comparable.
    main.app.dependency_overrides[main.get_settings] = lambda: _settings(
        max_repair_attempts=2
    )
    yield attempt_store
    main.app.dependency_overrides.clear()


def test_attempts_endpoint_exposes_failures_rollbacks_and_escalation(
    retry_client, scan_store, detection_store, patch_store
):
    attempt_store = retry_client
    scan_id = _seed_scan(scan_store)
    finding_id = _seed_finding(detection_store, scan_id)
    engine = ScriptedActionEngine(patch_store, stages=["validation_failed"])
    applier = CountingApplier()
    orchestrator = Orchestrator(
        settings=_settings(max_repair_attempts=2),
        scan_store=scan_store,
        detection_store=detection_store,
        patch_store=patch_store,
        attempt_store=attempt_store,
        action_engine=engine,
        applier=applier,
        validator=StageValidator(engine),
        verifier=StageVerifier(engine),
    )
    scan_store.compare_and_transition(
        scan_id,
        expected=frozenset({ScanStatus.detected}),
        to_status=ScanStatus.repairing,
    )
    orchestrator.run_scan(scan_id)

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get(f"/api/v1/scans/{scan_id}/attempts")

    assert response.status_code == 200
    body = response.json()
    assert body["scan_id"] == str(scan_id)
    assert body["scan_status"] == "escalated"
    assert body["escalation_reason"]
    assert "no repair attempts remain" in body["escalation_reason"]
    assert body["attempts_remaining"] == 0
    assert [a["n"] for a in body["attempts"]] == [1, 2]
    for attempt in body["attempts"]:
        assert attempt["status"] == "rolled_back"
        assert attempt["failure_code"] == "validation_failed"
        assert attempt["rollback"]["confirmed"] is True
        assert attempt["duration_ms"] is not None
        assert attempt["proposal_hash"]
        assert attempt["build_result"] == "MOCK build failed"
