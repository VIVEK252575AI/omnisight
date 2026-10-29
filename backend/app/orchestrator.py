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
* **One bounded attempt (W3.1).** The retry loop and rollback execution are
  W3.4; the structure below is the seam they extend (``max_repair_attempts``
  already decides whether a failed scan is retryable or escalated).
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

import logging
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Callable, Protocol, runtime_checkable
from uuid import UUID

from fastapi import status
from pydantic import BaseModel, Field

from app.action_engine import ActionEngine
from app.ai.adapters.patch import PatchGenerator
from app.ai.contracts import FindingStatus
from app.config import Settings
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
        }
    ),
    AttemptStatus.patch_proposed: frozenset(
        {
            AttemptStatus.patch_applied,
            AttemptStatus.failed_apply,
            AttemptStatus.escalated,
            AttemptStatus.cancelled,
        }
    ),
    AttemptStatus.patch_applied: frozenset(
        {
            AttemptStatus.validation_passed,
            AttemptStatus.validation_failed,
            AttemptStatus.escalated,
            AttemptStatus.cancelled,
        }
    ),
    AttemptStatus.validation_passed: frozenset(
        {
            AttemptStatus.verified,
            AttemptStatus.verification_failed,
            AttemptStatus.escalated,
            AttemptStatus.cancelled,
        }
    ),
    AttemptStatus.validation_failed: frozenset(
        {AttemptStatus.rolled_back, AttemptStatus.escalated}
    ),
    AttemptStatus.verification_failed: frozenset(
        {AttemptStatus.rolled_back, AttemptStatus.escalated}
    ),
    AttemptStatus.verified: frozenset(),
    AttemptStatus.rolled_back: frozenset(),
    AttemptStatus.escalated: frozenset(),
    AttemptStatus.failed_generate: frozenset(),
    AttemptStatus.failed_apply: frozenset(),
    AttemptStatus.cancelled: frozenset(),
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
    """

    passed: bool
    summary: str = Field(min_length=1, max_length=2000)
    deterministic_evidence: str | None = Field(default=None, max_length=2000)


class ApplyResult(BaseModel):
    """Where a validated proposal was applied."""

    workspace_id: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)


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
    #: Provenance of the proposal used for this attempt, so a mock-generated
    #: proposal is always labelled as such in API output (never implied real).
    generator_label: str | None = Field(default=None, max_length=100)


class AttemptListResponse(BaseModel):
    """Ordered attempt history for one scan."""

    scan_id: UUID
    attempts: list[AttemptRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Component Protocols (injected seams; real implementations pending)
# ---------------------------------------------------------------------------


@runtime_checkable
class PatchApplier(Protocol):
    """Applies a VALIDATED proposal inside an isolated workspace.

    Real implementation: PENDING (W3.4 links the ShopLite sandbox staging
    chain). An implementation must only ever receive a proposal whose status is
    ``proposed`` and must apply it outside the API process's own source tree.
    """

    def apply(
        self, record: PatchProposalRecord, proposal: PatchProposal
    ) -> ApplyResult:
        """Apply the proposal and return the workspace it was applied in."""
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
) -> AttemptRecord:
    """Settle an attempt in an end status and persist it.

    Re-reads the stored record first so a status written by another actor (the
    cancel endpoint, the recovery sweep) is honoured instead of overwritten.
    """
    current = store.get(attempt.attempt_id) or attempt
    if not current.status.is_resumable:
        # Already ended: keep the first verdict, never overwrite it.
        return current
    transition_attempt(current.status, to_status)
    finished = current.model_copy(
        update={
            "status": to_status,
            "error": _safe(error) if error else current.error,
            "ended_at": utcnow() if to_status in ENDED_ATTEMPT_STATUSES else None,
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


def recover_orphaned_scans(
    *, scan_store: ScanStore, attempt_store: AttemptStore, settings: Settings
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
        exhausted = bool(attempts) and all(
            attempt_store.next_number(a.finding_id) - 1 >= settings.max_repair_attempts
            for a in attempts
        )
        for attempt in attempts:
            if attempt.status.is_resumable:
                settle_attempt(
                    attempt_store,
                    attempt,
                    AttemptStatus.escalated,
                    "orphaned by a process restart while the attempt was in flight",
                )
        updated = scan_store.compare_and_transition(
            scan.scan_id,
            expected=frozenset({ScanStatus.repairing}),
            to_status=(
                ScanStatus.escalated if exhausted else ScanStatus.detected
            ),
            error=(
                "repair run was orphaned by a process restart "
                f"({stuck_for:.0f}s in 'repairing')"
            ),
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
        attempts: list[AttemptRecord] = []
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
                        f"deadline after {len(attempts)} attempt(s)"
                    ),
                )
                return attempts
            attempts.append(self.run_finding(record.finding.finding_id))

        if self._cancel_requested(scan_id):
            # The cancel endpoint already settled the scan; do not reopen it.
            return attempts

        self._settle_after_attempts(scan_id, attempts)
        return attempts

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
        failed = [a for a in attempts if a.status is not AttemptStatus.verified]
        reason = failed[0].error if failed else "repair run produced no attempt"
        exhausted = all(
            self.attempt_store.next_number(a.finding_id) - 1
            >= self.settings.max_repair_attempts
            for a in failed
        ) if failed else True
        target = ScanStatus.escalated if exhausted else ScanStatus.detected
        if exhausted:
            reason = (
                f"{reason} (no repair attempts remain: "
                f"{self.settings.max_repair_attempts} per finding)"
            )
        self._settle(scan_id, target, reason)

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

    def run_finding(self, finding_id: str) -> AttemptRecord:
        """Run one bounded attempt: propose -> apply -> validate -> verify."""
        record = self.detection_store.get_finding(finding_id)
        if record is None:
            raise DomainError(
                ErrorCode.FINDING_NOT_FOUND,
                f"finding {finding_id} not found",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        attempt = self.attempt_store.add(
            AttemptRecord(
                attempt_id=new_id("attempt"),
                finding_id=finding_id,
                scan_id=record.scan_id,
                n=self.attempt_store.next_number(finding_id),
                status=AttemptStatus.detected,
                started_at=utcnow(),
            )
        )
        try:
            settled = self._run_attempt(record, attempt)
            # The store is the source of truth: a step may have been settled by
            # another actor (cancel/recovery), and the local copy would be stale.
            return self.attempt_store.get(settled.attempt_id) or settled
        except DomainError:
            # Domain errors are the API's; the attempt must still be settled so
            # it cannot look in-flight forever.
            return self._finish(
                attempt, AttemptStatus.escalated, "repair aborted by a domain error"
            )
        except Exception as exc:  # noqa: BLE001 - boundary guard, logged below
            logger.exception(
                "unexpected orchestrator failure for finding %s", finding_id
            )
            return self._finish(
                attempt, AttemptStatus.escalated, f"unexpected error: {_safe(exc)}"
            )

    def _run_attempt(
        self, record: FindingRecord, attempt: AttemptRecord
    ) -> AttemptRecord:
        finding_id = attempt.finding_id

        # -- 1. propose (existing W2 action engine; never re-implemented) ----
        if (stopped := self._stopped(attempt)) is not None:
            return stopped
        try:
            proposal_record = self.action_engine.propose(finding_id)
        except DomainError as exc:
            return self._finish(attempt, AttemptStatus.failed_generate, exc.message)
        except Exception as exc:  # noqa: BLE001 - generator boundary
            logger.exception("proposal generation failed for %s", finding_id)
            return self._finish(
                attempt, AttemptStatus.failed_generate, _safe(exc)
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
            return self._finish(
                attempt,
                AttemptStatus.failed_generate,
                f"proposal {proposal_record.proposal_id} was not applied: rejected ({reasons})",
            )
        attempt = self._advance(attempt, AttemptStatus.patch_proposed)

        # -- 2. apply ------------------------------------------------------
        if (stopped := self._stopped(attempt)) is not None:
            return stopped
        payload = self.patch_store.get_proposal(proposal_record.proposal_id)
        if payload is None:
            return self._finish(
                attempt,
                AttemptStatus.failed_apply,
                f"proposal {proposal_record.proposal_id} has no stored patch payload",
            )
        try:
            applied = self.applier.apply(proposal_record, payload)
        except Exception as exc:  # noqa: BLE001 - applier boundary
            logger.exception("patch application failed for %s", finding_id)
            return self._finish(attempt, AttemptStatus.failed_apply, _safe(exc))
        attempt = attempt.model_copy(update={"workspace_id": applied.workspace_id})
        self.attempt_store.replace(attempt)
        attempt = self._advance(attempt, AttemptStatus.patch_applied)

        # -- 3. validate ---------------------------------------------------
        if (stopped := self._stopped(attempt)) is not None:
            return stopped
        try:
            validation = self.validator.validate(applied.workspace_id)
        except Exception as exc:  # noqa: BLE001 - validator boundary
            logger.exception("workspace validation failed for %s", finding_id)
            return self._finish(
                attempt,
                AttemptStatus.validation_failed,
                f"validator error: {_safe(exc)}",
            )
        attempt = attempt.model_copy(update={"build_result": validation.summary})
        # Persist before settling: settle_attempt re-reads the stored record so
        # another actor's status wins, which would otherwise drop this result.
        self.attempt_store.replace(attempt)
        if not validation.passed:
            return self._finish(
                attempt, AttemptStatus.validation_failed, validation.summary
            )
        attempt = self._advance(attempt, AttemptStatus.validation_passed)

        # -- 4. verify -----------------------------------------------------
        if (stopped := self._stopped(attempt)) is not None:
            return stopped
        try:
            verification = self.verifier.verify(finding_id, applied.workspace_id)
        except Exception as exc:  # noqa: BLE001 - verifier boundary
            logger.exception("visual verification failed for %s", finding_id)
            return self._finish(
                attempt,
                AttemptStatus.verification_failed,
                f"verifier error: {_safe(exc)}",
            )
        attempt = attempt.model_copy(
            update={"verification_result": verification.summary}
        )
        self.attempt_store.replace(attempt)
        if not verification.passed:
            return self._finish(
                attempt, AttemptStatus.verification_failed, verification.summary
            )
        if not (verification.deterministic_evidence or "").strip():
            # "Fixed" without reproducible evidence is not a fix. A passing
            # build got us here; it still does not prove the defect is gone.
            return self._finish(
                attempt,
                AttemptStatus.verification_failed,
                "verification claimed success without deterministic evidence",
            )
        return self._finish(attempt, AttemptStatus.verified, None)

    # -- helpers -----------------------------------------------------------

    def _stopped(self, attempt: AttemptRecord) -> AttemptRecord | None:
        """Return the settled record when the run was cancelled, else ``None``."""
        if not self._cancel_requested(attempt.scan_id):
            return None
        return self._finish(attempt, AttemptStatus.cancelled, CANCELLED_MESSAGE)

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
    ) -> AttemptRecord:
        """Settle an attempt in an end status and persist it."""
        return settle_attempt(self.attempt_store, attempt, to_status, error)

    # -- crash recovery ----------------------------------------------------

    def recover_orphaned_scans(self) -> list[UUID]:
        """Settle scans left in ``repairing`` by a previous process."""
        return recover_orphaned_scans(
            scan_store=self.scan_store,
            attempt_store=self.attempt_store,
            settings=self.settings,
        )
