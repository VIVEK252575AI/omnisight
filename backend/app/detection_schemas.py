"""API-facing schemas for the VLM detection workflow (Week 2, W2.4).

These live in their own module (rather than ``app/schemas.py``) because they
re-use the canonical W2.3 finding contract in ``app.ai.contracts`` — and that
module already imports ``Viewport`` from ``app/schemas.py``, so importing it
back would create a circular import.

The validated finding itself is embedded (not re-declared) as
:class:`app.ai.contracts.Finding`, so there is exactly one definition of a
finding's fields and the API can never drift from the parser.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.ai.contracts import Finding, VlmOutcome
from app.ids import utcnow
from app.schemas import ScanStatus, Viewport


class FindingRecord(BaseModel):
    """One persisted finding, plus the provenance needed to audit it.

    ``adapter_label`` is always carried so the API can show ``MOCK`` where a
    mock produced the record (mock output must never look like real inference).
    """

    model_config = {"from_attributes": True}

    finding: Finding
    scan_id: UUID
    adapter_label: str = Field(min_length=1, max_length=100)
    is_mock: bool = False
    route: str | None = Field(default=None, max_length=500)
    viewport: Viewport | None = None
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=50)
    screenshot_artifact_ids: list[UUID] = Field(default_factory=list, max_length=50)
    raw_output: str | None = Field(
        default=None,
        description=(
            "Untrusted raw model text for this scan's run; only populated on the "
            "single-finding endpoint when debug exposure is enabled."
        ),
    )
    created_at: datetime = Field(default_factory=utcnow)


class DetectionRun(BaseModel):
    """Record of the most recent detection attempt for a scan.

    Every call records the adapter label, latency, attempts and (on failure) a
    machine-readable reason code, so failures are observable rather than
    silent. ``raw_output`` is only populated when raw output is retained AND
    explicitly requested via the debug flag — it is untrusted model text.
    """

    model_config = {"from_attributes": True}

    scan_id: UUID
    status: ScanStatus
    adapter_label: str = Field(min_length=1, max_length=100)
    is_mock: bool = False
    model_label: str | None = Field(default=None, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)
    schema_version: int = 1
    attempts: int = Field(default=1, ge=1)
    duration_ms: float = Field(default=0.0, ge=0.0)
    outcome: VlmOutcome | None = None
    reason_code: str | None = Field(
        default=None,
        max_length=64,
        description="Machine-readable failure reason (e.g. 'schema_invalid', 'timeout').",
    )
    reason_message: str | None = Field(default=None, max_length=500)
    findings_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    raw_output: str | None = Field(
        default=None,
        description="Untrusted raw model text; only present when debug exposure is on.",
    )


class ScanFindingsResponse(BaseModel):
    """GET /api/v1/scans/{scan_id}/findings response."""

    scan_id: UUID
    status: ScanStatus
    adapter_label: str | None = Field(
        default=None,
        description="Adapter that produced the latest detection ('MOCK' when mocked).",
    )
    detection: DetectionRun | None = None
    findings: list[FindingRecord] = Field(default_factory=list)


class DetectionTriggerResponse(BaseModel):
    """POST /api/v1/scans/{scan_id}/detect response (202 Accepted)."""

    scan_id: UUID
    status: ScanStatus
    accepted: bool = Field(
        description="True when a new detection run was scheduled; false when idempotent.",
    )
    detection: DetectionRun | None = None
