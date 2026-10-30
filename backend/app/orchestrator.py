"""Week 3 self-healing orchestration (W3.1): one bounded attempt per finding.

This module OWNS the repair loop's state machine and nothing else. It
coordinates components that already exist into one enforced sequence:

    finding (status ``detected``)
        -> propose   (existing W2 ``ActionEngine`` — reuse, never re-implemented)
        -> apply     (``PatchApplier``)
        -> validate  (``Validator``)
        -> verify    (``Verifier``)

Rules this module enforces (the reason it exists):

* **Single source of truth for transitions.** ``AttemptStatus`` +
  ``_ATTEMPT_TRANSITIONS`` here; scan-level state stays in
  :mod:`app.schemas` ``LEGAL_TRANSITIONS``. Illegal moves raise a 409
  ``DomainError``.
* **Failure is never success.** A step failure settles the attempt in a
  failed/cancelled status; only ``verification_passed`` resolves to
  ``verified``, and only ``verified`` moves the scan to
  ``review_preparation``. A successful build alone is not acceptance: the
  verifier must return deterministic evidence.
* **Bounded retries with rollback (W3.4).** A finding gets at most
  ``max_repair_attempts`` attempts (hard ceiling
  ``MAX_REPAIR_ATTEMPTS_CEILING``) inside one run's wall-clock budget. Every
  failed attempt is rolled back to its baseline and the rollback is verified by
  hash; the next proposal receives a bounded summary of the failures; a
  byte-identical proposal is not re-applied; and an unverifiable rollback is a
  hard error that stops the ladder and fails the scan.
* **Single flight per scan.** ``POST /repair`` claims the scan with an atomic
  compare-and-transition into ``repairing``; a second concurrent call is a 409.
* **Nothing sticks in a running state.** Cancellation is honoured between
  steps, a cooperative wall-clock deadline escalates a runaway run, and
  :meth:`Orchestrator.recover_orphaned_scans` settles scans left in
  ``repairing`` by a process restart.

Component boundaries are injected, never constructed here. Real
implementations of ``PatchApplier`` / ``Validator`` / ``Verifier`` /
``DeliveryService`` are PENDING (see each Protocol); tests inject fakes and the
API refuses to start a repair when they are not wired, so an unwired deployment
can never report a fake success.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any, Callable, Protocol, runtime_checkable
from uuid import UUID

from fastapi import status
from pydantic import BaseModel, Field

from app.action_engine import ActionEngine
from app.ai.adapters.patch import PatchGenerator
from app.ai.contracts import FindingStatus
from app.config import MAX_REPAIR_ATTEMPTS_CEILING, Settings
from app.detection_schemas import FindingRecord
from app.detection_storage import DetectionStore
from app.errors import DomainError, ErrorCode
from app.ids import new_id, utcnow
from app.patch_schemas import PatchProposal, PatchProposalRecord, PatchProposalStatus
from app.patch_storage import PatchStore
from app.schemas import ScanStatus
from app.storage import ScanStore

logger = logging.getLogger("omnisight")

#: Maximum length of a recorded error string; component messages are untrusted
#: input and must never become an unbounded log/response field.
MAX_ERROR_CHARS = 500

CANCELLED_MESSAGE = "repair attempt cancelled before the next step"


def _safe(message: object) -> str:
    """Bound and flatten a component error message for persistence/logging."""
    text = " ".join(str(message).split()) or "component failed without a message"
    return text[:MAX_ERROR_CHARS]


def _accepts_prior_failure(target: object) -> bool:
    """True when a proposer can take the ``prior_failure`` keyword.

    Retry feedback is optional: a generator/action engine written against the
    original two-argument contract keeps working, it just cannot use the
    previous attempt's failure context.
    """
    try:
        parameters = inspect.signature(target.propose).parameters  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    if "prior_failure" in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _propose(
    action_engine: object, finding_id: str, prior_failure: str | None
) -> PatchProposalRecord:
    """Call ``propose`` with retry context when (and only when) it is accepted."""
    if prior_failure and _accepts_prior_failure(action_engine):
        return action_engine.propose(  # type: ignore[attr-defined]
            finding_id, prior_failure=prior_failure
        )
    return action_engine.propose(finding_id)  # type: ignore[attr-defined]


def _proposal_hash(proposal: PatchProposal) -> str:
    """Stable digest of a proposal's executable content.

    Used to notice a retry that proposes the exact same edit. Only the
    structured payload is hashed (never model prose), so identical edits produce
    identical digests regardless of formatting.
    """
    canonical = proposal.model_dump_json(exclude_none=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Attempt state machine
# ---------------------------------------------------------------------------


class AttemptStatus(str, Enum):
    """Lifecycle of ONE bounded self-healing attempt for one finding.

    Forward path::

        detected -> patch_proposed -> patch_applied ->
        validation_passed -> verified

    Deterministic disagreement ends the attempt at ``validation_failed`` /
    ``verification_failed`` (``rolled_back`` is the W3.4 rollback outcome) —
    those can never be followed by ``verified``. Component/plumbing failures
    have their own statuses (``failed_generate``, ``failed_apply``) so a
    pipeline bug is never reported as a rejected fix.
    """

    detected = "detected"
    patch_proposed = "patch_proposed"
    patch_applied = "patch_applied"
    validation_passed = "validation_passed"
    validation_failed = "validation_failed"
    verified = "verified"
    verification_failed = "verification_failed"
    rolled_back = "rolled_back"
    escalated = "escalated"
    failed_generate = "failed_generate"
    failed_apply = "failed_apply"
    cancelled = "cancelled"
    #: The attempt was left mid-flight by a process restart.
    interrupted = "interrupted"
    #: The workspace could not be restored to its baseline. A hard error: it
    #: stops the retry loop and needs a human, because bytes we cannot account
    #: for are still on disk.
    rollback_failed = "rollback_failed"

    @property
    def is_resumable(self) -> bool:
        """True while the attempt is still expected to advance."""
        return self in _RESUMABLE_ATTEMPT_STATUSES


#: Statuses an attempt may still leave; everything else is an end state.
_RESUMABLE_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.detected,
        AttemptStatus.patch_proposed,
        AttemptStatus.patch_applied,
        AttemptStatus.validation_passed,
    }
)

#: Explicit legal attempt transitions. ``escalated`` is reachable from every
#: resumable state because handing a stuck/ambiguous attempt to a human is
#: always a safe administrative move — and it is still never ``verified``.
_ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.detected: frozenset(
        {
            AttemptStatus.patch_proposed,
            AttemptStatus.failed_generate,
            AttemptStatus.escalated,
            AttemptStatus.cancelled,
            AttemptStatus.interrupted,
        }
    ),
    AttemptStatus.patch_proposed: frozenset(
        {
            AttemptStatus.patch_applied,
            AttemptStatus.failed_apply,
            AttemptStatus.failed_generate,
            AttemptStatus.escalated,
            AttemptStatus.cancelled,
            AttemptStatus.interrupted,
        }
    ),
    AttemptStatus.patch_applied: frozenset(
        {
            AttemptStatus.validation_passed,
            AttemptStatus.validation_failed,
            AttemptStatus.escalated,
            AttemptStatus.cancelled,
            AttemptStatus.interrupted,
        }
    ),
    AttemptStatus.validation_passed: frozenset(
        {
            AttemptStatus.verified,
            AttemptStatus.verification_failed,
            AttemptStatus.escalated,
            AttemptStatus.cancelled,
            AttemptStatus.interrupted,
        }
    ),
    # A failed step is always followed by a rollback (or a rollback failure).
    # ``escalated`` stays legal here so an operator can hand the attempt over
    # without pretending the rollback happened.
    AttemptStatus.validation_failed: frozenset(
        {
            AttemptStatus.rolled_back,
            AttemptStatus.rollback_failed,
            AttemptStatus.escalated,
        }
    ),
    AttemptStatus.verification_failed: frozenset(
        {
            AttemptStatus.rolled_back,
            AttemptStatus.rollback_failed,
            AttemptStatus.escalated,
        }
    ),
    AttemptStatus.failed_apply: frozenset(
        {
            AttemptStatus.rolled_back,
            AttemptStatus.rollback_failed,
            AttemptStatus.escalated,
        }
    ),
    AttemptStatus.verified: frozenset(),
    AttemptStatus.rolled_back: frozenset(),
    AttemptStatus.escalated: frozenset(),
    AttemptStatus.failed_generate: frozenset(),
    AttemptStatus.cancelled: frozenset(),
    AttemptStatus.interrupted: frozenset(),
    AttemptStatus.rollback_failed: frozenset(),
}

#: Statuses the orchestrator treats as the END of an attempt (a rollback
#: follow-up is recorded by W3.4; W3.1 does not execute rollback).
ENDED_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.verified,
        AttemptStatus.validation_failed,
        AttemptStatus.verification_failed,
        AttemptStatus.rolled_back,
        AttemptStatus.escalated,
        AttemptStatus.failed_generate,
        AttemptStatus.failed_apply,
        AttemptStatus.cancelled,
        AttemptStatus.interrupted,
        AttemptStatus.rollback_failed,
    }
)

#: End statuses that must never be retried for the same finding in this run.
#: ``rollback_failed`` is a hard stop: unaccounted-for bytes are on disk, so
#: trying again would compound the problem instead of fixing it.
NO_RETRY_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.verified,
        AttemptStatus.cancelled,
        AttemptStatus.rollback_failed,
        AttemptStatus.interrupted,
    }
)


def is_legal_attempt_transition(
    current: AttemptStatus, to_status: AttemptStatus
) -> bool:
    """True when ``current -> to_status`` is in the attempt transition table."""
    return to_status in _ATTEMPT_TRANSITIONS.get(current, frozenset())


def transition_attempt(
    current: AttemptStatus, to_status: AttemptStatus
) -> AttemptStatus:
    """Return ``to_status`` when legal; raise a 409 domain error otherwise."""
    if not is_legal_attempt_transition(current, to_status):
        raise DomainError(
            ErrorCode.ILLEGAL_TRANSITION,
            (
                f"illegal attempt transition from {current.value} to "
                f"{to_status.value} — allowed next statuses: "
                + (", ".join(sorted(s.value for s in _ATTEMPT_TRANSITIONS[current])) or "none")
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    return to_status


# ---------------------------------------------------------------------------
# Stage results and attempt records
# ---------------------------------------------------------------------------


class StageOutcome(BaseModel):
    """Deterministic result of one build/validate/verify step.

    ``deterministic_evidence`` is required by the verifier: a model (or any
    other source) claiming "fixed" is not acceptance without reproducible
    evidence such as a build/test log line or a fresh geometry measurement.

    ``detail`` is the component's own already-validated, JSON-safe report
    (``ValidationResult`` / verification result). It is recorded on the attempt
    so the API can expose statuses and capped output; it is never raw model
    text or an environment dump.
    """

    passed: bool
    summary: str = Field(min_length=1, max_length=2000)
    deterministic_evidence: str | None = Field(default=None, max_length=2000)
    detail: dict[str, Any] | None = None


class ApplyResult(BaseModel):
    """Where a validated proposal was applied.

    ``diff`` is the unified diff the applier produced against its baseline
    snapshot. It is capped and contains workspace-relative paths only, so it
    can be surfaced on the attempts endpoint without leaking absolute paths.
    """

    workspace_id: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    diff: str | None = Field(default=None, max_length=64_000)


class RollbackResult(BaseModel):
    """Outcome of restoring an attempt's workspace to its recorded baseline.

    ``confirmed`` is not a claim that a restore was attempted — it means the
    bytes were re-hashed against the baseline manifest and matched. An
    implementation that cannot prove the restore reports ``confirmed=False``.
    """

    confirmed: bool
    summary: str = Field(min_length=1, max_length=2000)
    detail: dict[str, Any] | None = None


class AttemptRecord(BaseModel):
    """One bounded attempt of the repair pipeline for a finding.

    Persisted after every step. ``error`` is always a bounded, safe string —
    never a traceback, path or secret.
    """

    model_config = {"from_attributes": True}

    attempt_id: str = Field(min_length=1, max_length=64)
    finding_id: str = Field(min_length=1, max_length=64)
    scan_id: UUID
    n: int = Field(ge=1)
    patch_proposal_id: str | None = Field(default=None, max_length=64)
    workspace_id: str | None = Field(default=None, max_length=200)
    build_result: str | None = Field(default=None, max_length=2000)
    verification_result: str | None = Field(default=None, max_length=2000)
    status: AttemptStatus
    started_at: datetime
    ended_at: datetime | None = None
    error: str | None = Field(default=None, max_length=MAX_ERROR_CHARS)
    #: Unified diff produced when the proposal was applied (workspace-relative
    #: paths only, capped). ``None`` until the apply step succeeds.
    diff: str | None = Field(default=None, max_length=64_000)
    #: Structured build/test report (statuses, exit codes, capped output).
    validation: dict[str, Any] | None = None
    #: Structured verification report (deterministic checks + VLM verdict).
    verification: dict[str, Any] | None = None
    #: Provenance of the proposal used for this attempt, so a mock-generated
    #: proposal is always labelled as such in API output (never implied real).
    generator_label: str | None = Field(default=None, max_length=100)
    #: Machine-readable reason this attempt did not verify (``validation_failed``,
    #: ``verification_failed``, ``duplicate_patch``, ``rollback_incomplete``,
    #: ``cancelled``, ``interrupted``, ...). Stable for the dashboard to key on.
    failure_code: str | None = Field(default=None, max_length=100)
    #: SHA-256 of the proposal payload, so a retry that proposes the exact same
    #: edit is detected instead of being re-applied.
    proposal_hash: str | None = Field(default=None, max_length=64)
    #: Bounded failure context that was given to the generator for this attempt.
    prior_failure: str | None = Field(default=None, max_length=2000)
    #: Rollback outcome for this attempt (``confirmed`` + summary), when a
    #: workspace existed and was restored.
    rollback: dict[str, Any] | None = None
    #: Wall-clock duration of the attempt in milliseconds, set when it settles.
    duration_ms: int | None = Field(default=None, ge=0)


class AttemptListResponse(BaseModel):
    """Ordered attempt history for one scan.

    ``escalation_reason`` mirrors the scan's recorded reason so the dashboard
    can show why a human is needed without a second request, and
    ``can_deliver`` is always false for an escalated scan (delivery is
    human-gated and only a verified, reviewable scan may proceed).
    """

    scan_id: UUID
    attempts: list[AttemptRecord] = Field(default_factory=list)
    scan_status: ScanStatus | None = None
    escalation_reason: str | None = Field(default=None, max_length=2000)
    attempts_remaining: int | None = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# Component Protocols (injected seams; real implementations pending)
# ---------------------------------------------------------------------------


@runtime_checkable
class PatchApplier(Protocol):
    """Applies a VALIDATED proposal inside an isolated workspace.

    Real implementation: :class:`app.patch_apply.SourcePatchApplier` (W3.2). An
    implementation must only ever receive a proposal whose status is
    ``proposed`` and must apply it outside the API process's own source tree.
    """

    def apply(
        self,
        record: PatchProposalRecord,
        proposal: PatchProposal,
        *,
        attempt_id: str | None = None,
    ) -> ApplyResult:
        """Apply the proposal and return the workspace it was applied in."""
        ...

    def rollback(self, workspace_id: str) -> RollbackResult:
        """Restore the workspace to its baseline and prove the bytes match.

        Called after EVERY failed attempt (and by crash recovery). A rollback
        that cannot be verified must report ``confirmed=False`` rather than
        assume success: the orchestrator then stops retrying and marks the scan
        failed with ``rollback_incomplete``.
        """
        ...


@runtime_checkable
class Validator(Protocol):
    """Runs build/tests inside the workspace and reports a machine result."""

    def validate(self, workspace_id: str) -> StageOutcome:
        """Build/test the workspace; ``passed`` means the build/tests passed."""
        ...


@runtime_checkable
class Verifier(Protocol):
    """Re-collects fresh evidence and re-checks the ORIGINAL defect.

    A build passing says nothing about whether the visual defect is gone, so
    an implementation must supply ``deterministic_evidence``; the orchestrator
    refuses to record ``verified`` without it.
    """

    def verify(self, finding_id: str, workspace_id: str) -> StageOutcome:
        """Return the verdict plus the evidence that produced it."""
        ...


@runtime_checkable
class DeliveryService(Protocol):
    """Prepares reviewable delivery (branch/commit/PR) for a verified attempt.

    Real implementation: PENDING — the existing GitHub publish service is
    human-gated and disabled by default. Invoked by W3.4/W4, not by W3.1: this
    slice stops at ``review_preparation`` and never merges or deploys.
    """

    def prepare_review(self, scan_id: UUID, attempts: list[AttemptRecord]) -> StageOutcome:
        """Prepare (never merge, never deploy) the change for human review."""
        ...


@runtime_checkable
class EvidenceCollector(Protocol):
    """Playwright evidence boundary (owned by the automation teammate).

    Real implementation: PENDING — the collection scripts exist as tooling, but
    no in-process collector is wired. Declared here so the verifier's
    re-screenshot step can be injected through this Protocol in W3.2 instead of
    the orchestrator growing its own browser code.
    """

    def collect(self, scan_id: UUID, *, routes: list[str] | None = None) -> list[str]:
        """Collect fresh evidence for a scan and return its artifact ids."""
        ...


# ---------------------------------------------------------------------------
# Attempt store
# ---------------------------------------------------------------------------


class AttemptStore:
    """Thread-safe attempt records behind a small swappable interface.

    Process-local on purpose for W3.1: attempts are created and settled inside
    one background run, and the durable table (plus migration) belongs with the
    W3.4 retry/rollback loop that changes the record shape. The interface is
    the seam that swap replaces — nothing above it knows where records live.
    """

    def __init__(self) -> None:
        self._attempts: dict[str, AttemptRecord] = {}
        self._by_finding: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def add(self, attempt: AttemptRecord) -> AttemptRecord:
        """Insert a new attempt record."""
        with self._lock:
            if attempt.attempt_id in self._attempts:
                raise KeyError(f"attempt {attempt.attempt_id} already exists")
            self._attempts[attempt.attempt_id] = attempt
            self._by_finding.setdefault(attempt.finding_id, []).append(
                attempt.attempt_id
            )
        return attempt

    def replace(self, attempt: AttemptRecord) -> AttemptRecord:
        """Persist a step's updated record."""
        with self._lock:
            if attempt.attempt_id not in self._attempts:
                raise KeyError(f"attempt {attempt.attempt_id} does not exist")
            self._attempts[attempt.attempt_id] = attempt
        return attempt

    def get(self, attempt_id: str) -> AttemptRecord | None:
        """Fetch one attempt by id."""
        with self._lock:
            return self._attempts.get(attempt_id)

    def list_for_finding(self, finding_id: str) -> list[AttemptRecord]:
        """Attempts for one finding, oldest first."""
        with self._lock:
            return [
                self._attempts[item]
                for item in self._by_finding.get(finding_id, [])
            ]

    def list_for_scan(self, scan_id: UUID) -> list[AttemptRecord]:
        """Attempts for one scan, oldest first (the API's ordered history)."""
        with self._lock:
            selected = [a for a in self._attempts.values() if a.scan_id == scan_id]
        return sorted(selected, key=lambda item: (item.started_at, item.n))

    def next_number(self, finding_id: str) -> int:
        """1-based attempt number for the finding's next attempt."""
        return len(self.list_for_finding(finding_id)) + 1

    def has_in_flight(self, scan_id: UUID) -> bool:
        """True when the scan has an attempt that has not reached an end state."""
        return any(
            attempt.status.is_resumable for attempt in self.list_for_scan(scan_id)
        )

    def clear(self) -> None:
        """Drop every record (test/recovery helper)."""
        with self._lock:
            self._attempts.clear()
            self._by_finding.clear()


#: Process-wide attempt store, mirroring the other module-level stores. Tests
#: inject a fresh instance through the router dependency instead.
_ATTEMPT_STORE = AttemptStore()


def get_attempt_store() -> AttemptStore:
    """Return the process-wide attempt store."""
    return _ATTEMPT_STORE


def settle_attempt(
    store: AttemptStore,
    attempt: AttemptRecord,
    to_status: AttemptStatus,
    error: str | None,
    *,
    failure_code: str | None = None,
    rollback: dict[str, Any] | None = None,
) -> AttemptRecord:
    """Settle an attempt in an end status and persist it.

    Re-reads the stored record first so a status written by another actor (the
    cancel endpoint, the recovery sweep) is honoured instead of overwritten.
    "Ended" means ``ended_at`` is set — an attempt that is sitting in a failed
    step status but has not been settled yet can still record its rollback.
    """
    if to_status not in ENDED_ATTEMPT_STATUSES:
        raise ValueError(f"{to_status.value} is not an end status")
    current = store.get(attempt.attempt_id) or attempt
    if current.ended_at is not None:
        # Already ended: keep the first verdict, never overwrite it.
        return current
    if current.status is not to_status:
        transition_attempt(current.status, to_status)
    ended_at = utcnow()
    started = current.started_at
    finished = current.model_copy(
        update={
            "status": to_status,
            "error": _safe(error) if error else current.error,
            "failure_code": (
                failure_code if failure_code is not None else current.failure_code
            ),
            "rollback": rollback if rollback is not None else current.rollback,
            "ended_at": ended_at,
            "duration_ms": max(0, int((ended_at - started).total_seconds() * 1000)),
        }
    )
    store.replace(finished)
    logger.info(
        "repair attempt ended",
        extra={
            "attempt_id": finished.attempt_id,
            "scan_id": str(finished.scan_id),
            "status": to_status.value,
            "error": finished.error,
        },
    )
    return finished


def _attempt_budget(settings: Settings) -> int:
    """Retry budget actually honoured: configured value, hard-capped.

    ``Settings`` already clamps at load time; re-clamping here keeps the ceiling
    a property of the code path itself, so a ``model_construct``-built or future
    settings object cannot quietly exceed it.
    """
    return max(1, min(int(settings.max_repair_attempts), MAX_REPAIR_ATTEMPTS_CEILING))


def ensure_deliverable(scan_status: ScanStatus) -> None:
    """Raise unless this scan may proceed to delivery.

    Delivery (the PyGithub branch/commit/PR step) is human-gated and only
    meaningful for a scan that was prepared for review. An escalated, failed,
    cancelled or still-running scan is refused here — escalation must never
    become a shortcut to a pull request.
    """
    if scan_status is not ScanStatus.review_preparation:
        raise DomainError(
            ErrorCode.REPAIR_NOT_DELIVERABLE,
            (
                f"scan is '{scan_status.value}'; only a scan in "
                "'review_preparation' may proceed to delivery"
            ),
            status_code=status.HTTP_409_CONFLICT,
        )


def _recover_rollback(
    applier: PatchApplier | None, attempt: AttemptRecord
) -> dict[str, Any] | None:
    """Best-effort rollback of an interrupted attempt's workspace.

    Returns the recorded rollback report (or ``None`` when there is nothing to
    restore). Recovery never raises: a scan must always be settled, and an
    unverified restore is reported as ``confirmed=False`` rather than assumed.
    """
    if not attempt.workspace_id:
        return None
    if applier is None:
        # Not the same thing as a failed restore: nothing was attempted, and
        # claiming ``confirmed: False`` here would fail scans that never got
        # the chance to roll back. The workspace is left for the retention
        # sweep, which only ever deletes inside ``WORKSPACE_ROOT``.
        return {
            "confirmed": False,
            "attempted": False,
            "summary": (
                "interrupted workspace was not rolled back: no patch applier "
                "was available during the startup recovery sweep"
            ),
        }
    try:
        result = applier.rollback(attempt.workspace_id)
    except Exception as exc:  # noqa: BLE001 - recovery must settle the scan
        logger.exception("recovery rollback failed for %s", attempt.attempt_id)
        return {
            "confirmed": False,
            "attempted": True,
            "summary": f"rollback failed: {_safe(exc)}",
        }
    detail = {
        "confirmed": bool(result.confirmed),
        "attempted": True,
        "summary": _safe(result.summary),
    }
    if result.detail:
        detail.update(result.detail)
    return detail


def recover_orphaned_scans(
    *,
    scan_store: ScanStore,
    attempt_store: AttemptStore,
    settings: Settings,
    applier: PatchApplier | None = None,
) -> list[UUID]:
    """Settle scans left in ``repairing`` with no live attempt.

    Runs at process start. A repair run lives in a background task of ONE API
    process, so after a restart nothing can still be working on a scan left in
    ``repairing``. Each such scan is returned to ``detected`` (retryable) or
    escalated once its attempts are exhausted — never left running, and never
    reported as verified. Only scans older than ``repair_stuck_after_seconds``
    are touched so a genuinely in-flight run in another worker is not stolen.

    Needs only the stores, so recovery still works while the component
    implementations are pending.
    """
    recovered: list[UUID] = []
    for scan in scan_store.list():
        if scan.status is not ScanStatus.repairing:
            continue
        stuck_for = (utcnow() - scan.updated_at).total_seconds()
        if stuck_for < settings.repair_stuck_after_seconds:
            continue
        attempts = attempt_store.list_for_scan(scan.scan_id)
        budget = _attempt_budget(settings)
        exhausted = bool(attempts) and all(
            attempt_store.next_number(a.finding_id) - 1 >= budget for a in attempts
        )
        rollback_incomplete = False
        for attempt in attempts:
            if not attempt.status.is_resumable:
                continue
            rollback = _recover_rollback(applier, attempt)
            if (
                rollback is not None
                and rollback.get("attempted", True)
                and not rollback.get("confirmed")
            ):
                rollback_incomplete = True
            settle_attempt(
                attempt_store,
                attempt,
                AttemptStatus.interrupted,
                "interrupted by a process restart while the attempt was in flight",
                failure_code="interrupted",
                rollback=rollback,
            )

        if rollback_incomplete:
            # A workspace we could not restore outlives the restart, so this is
            # a hard error: no retry, no delivery, a human has to look.
            target = ScanStatus.failed
            reason = (
                "rollback_incomplete: an interrupted attempt's workspace could "
                "not be restored to its baseline"
            )
        elif exhausted:
            target = ScanStatus.escalated
            reason = (
                "repair attempts were exhausted before the run was interrupted "
                f"by a process restart ({stuck_for:.0f}s in 'repairing')"
            )
        else:
            target = ScanStatus.detected
            reason = (
                "repair run was orphaned by a process restart "
                f"({stuck_for:.0f}s in 'repairing'); retry with POST /repair"
            )
        updated = scan_store.compare_and_transition(
            scan.scan_id,
            expected=frozenset({ScanStatus.repairing}),
            to_status=target,
            error=reason,
        )
        if updated is not None:
            recovered.append(scan.scan_id)
    if recovered:
        logger.warning("recovered %d orphaned repair scan(s)", len(recovered))
    return recovered


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Drives ONE bounded attempt per finding through the repair pipeline.

    Every step persists its record before the next one starts, so a reader can
    always see how far an attempt got. A failure settles the attempt and the
    scan; it never advances either one towards a verified outcome.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        scan_store: ScanStore,
        detection_store: DetectionStore,
        patch_store: PatchStore,
        attempt_store: AttemptStore,
        action_engine: ActionEngine,
        applier: PatchApplier,
        validator: Validator,
        verifier: Verifier,
        delivery: DeliveryService | None = None,
        evidence_collector: EvidenceCollector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        generator = getattr(action_engine, "generator", None)
        if not isinstance(generator, PatchGenerator):
            # A repair run without a proposal generator could only ever fake
            # progress; refuse to construct instead.
            raise TypeError(
                "action_engine must expose a PatchGenerator as .generator"
            )
        self.settings = settings
        self.scan_store = scan_store
        self.detection_store = detection_store
        self.patch_store = patch_store
        self.attempt_store = attempt_store
        self.action_engine = action_engine
        self.applier = applier
        self.validator = validator
        self.verifier = verifier
        self.delivery = delivery
        self.evidence_collector = evidence_collector
        self._clock = clock

    # -- scan level ---------------------------------------------------------

    def run_scan(self, scan_id: UUID) -> list[AttemptRecord]:
        """Run the repair pipeline for every patchable finding in a scan.

        Called from the background task started by ``POST /repair``. The scan
        must already be in ``repairing`` (claimed atomically by the endpoint);
        anything else is ignored rather than re-opened.
        """
        scan = self.scan_store.get(scan_id)
        if scan is None:
            logger.warning("repair run skipped: scan %s does not exist", scan_id)
            return []
        if scan.status is not ScanStatus.repairing:
            logger.warning(
                "repair run skipped: scan %s is '%s', not 'repairing'",
                scan_id,
                scan.status.value,
            )
            return []

        findings = self._patchable_findings(scan_id)
        if not findings:
            # The endpoint verified patchable findings existed; if they are all
            # gone the situation needs a human, not a silent no-op success.
            self._settle(
                scan_id,
                ScanStatus.escalated,
                "no findings remained in 'detected' status when the repair run started",
            )
            return []

        deadline = self._clock() + self.settings.repair_run_timeout_seconds
        #: Final attempt per finding, for the scan-level decision.
        finals: list[AttemptRecord] = []
        for record in findings:
            if self._cancel_requested(scan_id):
                break
            if self._clock() > deadline:
                self._settle(
                    scan_id,
                    ScanStatus.escalated,
                    (
                        "repair run exceeded its "
                        f"{self.settings.repair_run_timeout_seconds:g}s watchdog "
                        f"deadline after {len(finals)} finding(s)"
                    ),
                )
                return self.attempt_store.list_for_scan(scan_id)
            finals.append(
                self.run_finding(record.finding.finding_id, deadline=deadline)
            )
            if not self._still_repairing(scan_id):
                # A hard error (rollback_incomplete, cancel) already settled the
                # scan; do not keep driving findings or re-open it.
                return self.attempt_store.list_for_scan(scan_id)

        if self._cancel_requested(scan_id):
            # The cancel endpoint already settled the scan; do not reopen it.
            return self.attempt_store.list_for_scan(scan_id)

        self._settle_after_attempts(scan_id, finals)
        # Every attempt of the run, oldest first: the retry ladder is the
        # deliverable, so a caller must be able to see all of it.
        return self.attempt_store.list_for_scan(scan_id)

    def _patchable_findings(self, scan_id: UUID) -> list[FindingRecord]:
        return [
            record
            for record in self.detection_store.list_for_scan(scan_id)
            if record.finding.status is FindingStatus.detected
        ]

    def _settle_after_attempts(
        self, scan_id: UUID, attempts: list[AttemptRecord]
    ) -> None:
        """Move the scan on: review when verified, else retry or escalate."""
        if any(a.status is AttemptStatus.verified for a in attempts):
            self._settle(scan_id, ScanStatus.review_preparation, None)
            return
        budget = _attempt_budget(self.settings)
        failed = [a for a in attempts if a.status is not AttemptStatus.verified]
        if not failed:
            self._settle(
                scan_id, ScanStatus.detected, "repair run produced no attempt"
            )
            return
        exhausted = all(
            self.attempt_store.next_number(a.finding_id) - 1 >= budget
            for a in failed
        )
        summary = self._escalation_reason(failed)
        if exhausted:
            self._escalate_findings(failed)
            self._settle(
                scan_id,
                ScanStatus.escalated,
                f"{summary} — no repair attempts remain ({budget} per finding)",
            )
            return
        self._settle(scan_id, ScanStatus.detected, summary)

    def _escalate_findings(self, attempts: list[AttemptRecord]) -> None:
        """Hand exhausted findings to a human so nothing retries them silently.

        The finding leaves ``detected``, which is what makes a later automatic
        pass skip it: the loop only ever proposes against ``detected`` findings.
        """
        for attempt in attempts:
            updated = self.detection_store.compare_and_set_finding_status(
                attempt.finding_id,
                expected=frozenset({FindingStatus.detected}),
                to_status=FindingStatus.escalated,
            )
            if updated is None:
                logger.warning(
                    "finding %s could not be escalated: its status changed first",
                    attempt.finding_id,
                )
            else:
                logger.info(
                    "finding escalated after exhausting its repair budget",
                    extra={"finding_id": attempt.finding_id},
                )

    def _escalation_reason(self, attempts: list[AttemptRecord]) -> str:
        """Human-readable summary of why a repair run did not verify."""
        parts = [
            (
                f"finding {a.finding_id} attempt {a.n} ended {a.status.value}"
                + (f" [{a.failure_code}]" if a.failure_code else "")
                + (f": {a.error}" if a.error else "")
            )
            for a in attempts
        ]
        return _safe("; ".join(parts))

    def _still_repairing(self, scan_id: UUID) -> bool:
        """True while the scan is still owned by this repair run."""
        scan = self.scan_store.get(scan_id)
        return scan is not None and scan.status is ScanStatus.repairing

    def _settle(
        self, scan_id: UUID, to_status: ScanStatus, error: str | None
    ) -> None:
        """Atomically move a ``repairing`` scan to its settled status."""
        updated = self.scan_store.compare_and_transition(
            scan_id,
            expected=frozenset({ScanStatus.repairing}),
            to_status=to_status,
            error=error,
        )
        if updated is None:
            logger.warning(
                "scan %s was not settled to '%s': another actor changed it first",
                scan_id,
                to_status.value,
            )
            return
        logger.info(
            "repair run settled",
            extra={"scan_id": str(scan_id), "status": to_status.value},
        )

    def _cancel_requested(self, scan_id: UUID) -> bool:
        """Cancellation is signalled by the scan itself reaching ``cancelled``."""
        scan = self.scan_store.get(scan_id)
        return scan is not None and scan.status is ScanStatus.cancelled

    # -- finding level ------------------------------------------------------

    def run_finding(
        self, finding_id: str, *, deadline: float | None = None
    ) -> AttemptRecord:
        """Run up to ``max_repair_attempts`` bounded attempts for one finding.

        The ladder is: propose -> apply -> validate -> verify. After a failed
        attempt the workspace is rolled back to its baseline before the next one
        starts; the next proposal receives a bounded summary of the previous
        failures;        a proposal byte-identical to one that already failed is not
        re-applied; and an attempt that cannot be rolled back stops the ladder
        immediately. ``deadline`` is the run's monotonic wall-clock budget: it is
        checked between attempts, in addition to the attempt count.
        """
        record = self.detection_store.get_finding(finding_id)
        if record is None:
            raise DomainError(
                ErrorCode.FINDING_NOT_FOUND,
                f"finding {finding_id} not found",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        budget = _attempt_budget(self.settings)
        last: AttemptRecord | None = None
        for _ in range(budget):
            if last is not None and last.status in NO_RETRY_ATTEMPT_STATUSES:
                break
            if self._cancel_requested(record.scan_id):
                break
            attempt = self.attempt_store.add(
                AttemptRecord(
                    attempt_id=new_id("attempt"),
                    finding_id=finding_id,
                    scan_id=record.scan_id,
                    n=self.attempt_store.next_number(finding_id),
                    status=AttemptStatus.detected,
                    started_at=utcnow(),
                    prior_failure=self._prior_failure(finding_id),
                )
            )
            try:
                settled = self._run_attempt(record, attempt)
            except DomainError:
                # Domain errors are the API's; the attempt must still be settled
                # so it cannot look in-flight forever.
                settled = self._finish(
                    attempt,
                    AttemptStatus.escalated,
                    "repair aborted by a domain error",
                    failure_code="aborted",
                )
            except Exception as exc:  # noqa: BLE001 - boundary guard, logged below
                logger.exception(
                    "unexpected orchestrator failure for finding %s", finding_id
                )
                settled = self._finish(
                    attempt,
                    AttemptStatus.escalated,
                    f"unexpected error: {_safe(exc)}",
                    failure_code="unexpected_error",
                )
            # The store is the source of truth: a step may have been settled by
            # another actor (cancel/recovery), and the local copy would be stale.
            last = self.attempt_store.get(settled.attempt_id) or settled
            if last.status is AttemptStatus.verified:
                return last
            if last.status is AttemptStatus.rollback_failed:
                # Hard error: the baseline is not provably restored, so retrying
                # would stack another edit on top of unaccounted-for bytes.
                self._settle(
                    record.scan_id,
                    ScanStatus.failed,
                    (
                        "rollback_incomplete: attempt "
                        f"{last.n} for finding {finding_id} could not be restored "
                        "to its baseline; manual attention required"
                    ),
                )
                return last
            if deadline is not None and self._clock() > deadline:
                self._settle(
                    record.scan_id,
                    ScanStatus.escalated,
                    (
                        "repair run exceeded its "
                        f"{self.settings.repair_run_timeout_seconds:g}s watchdog "
                        f"deadline on attempt {last.n} of finding {finding_id}"
                    ),
                )
                return last
            logger.info(
                "repair attempt failed, retrying if the budget allows",
                extra={
                    "finding_id": finding_id,
                    "attempt": last.n,
                    "status": last.status.value,
                    "budget": budget,
                },
            )
        if last is None:
            raise DomainError(
                ErrorCode.REPAIR_STATE_CONFLICT,
                f"no repair attempt could be started for finding {finding_id}",
                status_code=status.HTTP_409_CONFLICT,
            )
        return last

    def _run_attempt(
        self, record: FindingRecord, attempt: AttemptRecord
    ) -> AttemptRecord:
        finding_id = attempt.finding_id

        # -- 1. propose (existing W2 action engine; never re-implemented) ----
        if (stopped := self._stopped(attempt)) is not None:
            return stopped
        try:
            proposal_record = _propose(
                self.action_engine, finding_id, attempt.prior_failure
            )
        except DomainError as exc:
            return self._fail(
                attempt,
                AttemptStatus.failed_generate,
                "proposal_rejected",
                exc.message,
            )
        except Exception as exc:  # noqa: BLE001 - generator boundary
            logger.exception("proposal generation failed for %s", finding_id)
            return self._fail(
                attempt, AttemptStatus.failed_generate, "generator_error", _safe(exc)
            )

        attempt = attempt.model_copy(
            update={
                "patch_proposal_id": proposal_record.proposal_id,
                "generator_label": proposal_record.generator_label,
            }
        )
        self.attempt_store.replace(attempt)
        if proposal_record.status is not PatchProposalStatus.proposed:
            reasons = ", ".join(proposal_record.rejection_reasons) or "unspecified"
            return self._fail(
                attempt,
                AttemptStatus.failed_generate,
                "proposal_rejected",
                f"proposal {proposal_record.proposal_id} was not applied: rejected ({reasons})",
            )
        attempt = self._advance(attempt, AttemptStatus.patch_proposed)

        # -- 2. apply ------------------------------------------------------
        if (stopped := self._stopped(attempt)) is not None:
            return stopped
        payload = self.patch_store.get_proposal(proposal_record.proposal_id)
        if payload is None:
            return self._fail(
                attempt,
                AttemptStatus.failed_apply,
                "apply_failed",
                f"proposal {proposal_record.proposal_id} has no stored patch payload",
            )

        # A retry that proposes the very same edit would fail the very same way.
        # It still counts as a failed attempt, but nothing is re-applied.
        proposal_hash = _proposal_hash(payload)
        attempt = attempt.model_copy(update={"proposal_hash": proposal_hash})
        self.attempt_store.replace(attempt)
        if proposal_hash in self._already_failed_hashes(
            finding_id, attempt.attempt_id
        ):
            return self._fail(
                attempt,
                AttemptStatus.failed_generate,
                "duplicate_patch",
                (
                    "the generator proposed an edit identical to one that already "
                    "failed for this finding; it was not re-applied"
                ),
            )
        try:
            applied = self.applier.apply(
                proposal_record, payload, attempt_id=attempt.attempt_id
            )
        except Exception as exc:  # noqa: BLE001 - applier boundary
            logger.exception("patch application failed for %s", finding_id)
            return self._fail(
                attempt, AttemptStatus.failed_apply, "apply_failed", _safe(exc)
            )
        attempt = attempt.model_copy(
            update={"workspace_id": applied.workspace_id, "diff": applied.diff}
        )
        self.attempt_store.replace(attempt)
        attempt = self._advance(attempt, AttemptStatus.patch_applied)

        # -- 3. validate ---------------------------------------------------
        if (stopped := self._stopped(attempt)) is not None:
            return stopped
        try:
            validation = self.validator.validate(applied.workspace_id)
        except Exception as exc:  # noqa: BLE001 - validator boundary
            logger.exception("workspace validation failed for %s", finding_id)
            return self._fail(
                attempt,
                AttemptStatus.validation_failed,
                "validator_error",
                f"validator error: {_safe(exc)}",
            )
        attempt = attempt.model_copy(
            update={
                "build_result": validation.summary,
                "validation": validation.detail,
            }
        )
        # Persist before settling: settle_attempt re-reads the stored record so
        # another actor's status wins, which would otherwise drop this result.
        self.attempt_store.replace(attempt)
        if not validation.passed:
            return self._fail(
                attempt,
                AttemptStatus.validation_failed,
                "validation_failed",
                validation.summary,
            )
        attempt = self._advance(attempt, AttemptStatus.validation_passed)

        # -- 4. verify -----------------------------------------------------
        if (stopped := self._stopped(attempt)) is not None:
            return stopped
        try:
            verification = self.verifier.verify(finding_id, applied.workspace_id)
        except Exception as exc:  # noqa: BLE001 - verifier boundary
            logger.exception("visual verification failed for %s", finding_id)
            return self._fail(
                attempt,
                AttemptStatus.verification_failed,
                "verifier_error",
                f"verifier error: {_safe(exc)}",
            )
        attempt = attempt.model_copy(
            update={
                "verification_result": verification.summary,
                "verification": verification.detail,
            }
        )
        self.attempt_store.replace(attempt)
        if not verification.passed:
            return self._fail(
                attempt,
                AttemptStatus.verification_failed,
                "verification_failed",
                verification.summary,
            )
        if not (verification.deterministic_evidence or "").strip():
            # "Fixed" without reproducible evidence is not a fix. A passing
            # build got us here; it still does not prove the defect is gone.
            return self._fail(
                attempt,
                AttemptStatus.verification_failed,
                "no_deterministic_evidence",
                "verification claimed success without deterministic evidence",
            )
        return self._finish(attempt, AttemptStatus.verified, None)

    # -- helpers -----------------------------------------------------------

    def _stopped(self, attempt: AttemptRecord) -> AttemptRecord | None:
        """Return the settled record when the run was cancelled, else ``None``."""
        if not self._cancel_requested(attempt.scan_id):
            return None
        return self._finish(
            attempt, AttemptStatus.cancelled, CANCELLED_MESSAGE, failure_code="cancelled"
        )

    def _advance(
        self, attempt: AttemptRecord, to_status: AttemptStatus
    ) -> AttemptRecord:
        """Validate the transition, persist it, and return the updated record."""
        transition_attempt(attempt.status, to_status)
        updated = attempt.model_copy(update={"status": to_status})
        self.attempt_store.replace(updated)
        logger.info(
            "attempt step",
            extra={
                "attempt_id": updated.attempt_id,
                "scan_id": str(updated.scan_id),
                "status": to_status.value,
            },
        )
        return updated

    def _finish(
        self,
        attempt: AttemptRecord,
        to_status: AttemptStatus,
        error: str | None,
        *,
        failure_code: str | None = None,
        rollback: dict[str, Any] | None = None,
    ) -> AttemptRecord:
        """Settle an attempt in an end status and persist it."""
        return settle_attempt(
            self.attempt_store,
            attempt,
            to_status,
            error,
            failure_code=failure_code,
            rollback=rollback,
        )

    # -- retry / rollback --------------------------------------------------

    def _fail(
        self,
        attempt: AttemptRecord,
        to_status: AttemptStatus,
        failure_code: str,
        error: str,
    ) -> AttemptRecord:
        """Record a failed step, roll the workspace back, then settle.

        The failure status is written first (an audit trail that says what
        actually went wrong), then the workspace is restored and the attempt is
        settled as ``rolled_back``/``rollback_failed`` and given ``ended_at``.
        A failure can never settle as ``verified``.
        """
        failed = self._advance(attempt, to_status).model_copy(
            update={"error": _safe(error), "failure_code": failure_code}
        )
        self.attempt_store.replace(failed)
        rolled = self._roll_back(failed)
        return self._finish(
            rolled,
            rolled.status,
            rolled.error,
            failure_code=rolled.failure_code,
            rollback=rolled.rollback,
        )

    def _roll_back(self, attempt: AttemptRecord) -> AttemptRecord:
        """Restore the attempt's workspace to its baseline and prove it.

        An attempt that never got a workspace has nothing to restore. A rollback
        that raises, or that cannot prove the restore, is a hard failure — never
        silently treated as a clean undo.
        """
        if not attempt.workspace_id:
            return attempt
        try:
            result = self.applier.rollback(attempt.workspace_id)
        except Exception as exc:  # noqa: BLE001 - rollback boundary
            logger.exception("workspace rollback failed for %s", attempt.attempt_id)
            failed = self._advance(
                attempt.model_copy(
                    update={
                        "rollback": {
                            "confirmed": False,
                            "attempted": True,
                            "summary": f"rollback raised {type(exc).__name__}",
                        }
                    }
                ),
                AttemptStatus.rollback_failed,
            ).model_copy(
                update={
                    "error": _safe(f"workspace rollback failed: {exc}"),
                    "failure_code": "rollback_incomplete",
                }
            )
            self.attempt_store.replace(failed)
            return failed

        report: dict[str, Any] = {
            "confirmed": bool(result.confirmed),
            "summary": _safe(result.summary),
        }
        if result.detail:
            report.update(result.detail)
        updated = attempt.model_copy(update={"rollback": report})
        self.attempt_store.replace(updated)
        if not report["confirmed"]:
            failed = self._advance(updated, AttemptStatus.rollback_failed)
            failed = failed.model_copy(
                update={
                    "error": _safe(
                        "the workspace could not be restored to its baseline: "
                        + result.summary
                    ),
                    "failure_code": "rollback_incomplete",
                }
            )
            self.attempt_store.replace(failed)
            return failed
        return self._advance(updated, AttemptStatus.rolled_back)

    def _already_failed_hashes(
        self, finding_id: str, attempt_id: str
    ) -> frozenset[str]:
        """Proposal digests from earlier, non-verified attempts of this finding."""
        return frozenset(
            prior.proposal_hash
            for prior in self.attempt_store.list_for_finding(finding_id)
            if prior.attempt_id != attempt_id
            and prior.proposal_hash
            and prior.status is not AttemptStatus.verified
        )

    def _prior_failure(self, finding_id: str) -> str | None:
        """Bounded summary of this finding's earlier failures, for the generator.

        Built only from records the loop itself wrote (status + code + message),
        never from raw model text, and truncated to the configured cap so the
        retry prompt cannot grow without limit.
        """
        limit = int(self.settings.repair_prior_failure_max_chars)
        if limit <= 0:
            return None
        failures = [
            (
                f"attempt {prior.n} ended {prior.status.value}"
                + (f" ({prior.failure_code})" if prior.failure_code else "")
                + (f": {prior.error}" if prior.error else "")
            )
            for prior in self.attempt_store.list_for_finding(finding_id)
            if prior.status is not AttemptStatus.verified
        ]
        if not failures:
            return None
        text = " | ".join(failures)
        return text[-limit:]

    # -- crash recovery ----------------------------------------------------

    def recover_orphaned_scans(self) -> list[UUID]:
        """Settle scans left in ``repairing`` by a previous process."""
        return recover_orphaned_scans(
            scan_store=self.scan_store,
            attempt_store=self.attempt_store,
            settings=self.settings,
            applier=self.applier,
        )
