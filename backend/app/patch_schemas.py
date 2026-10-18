"""Patch-proposal schemas (Week 2, W2.5).

A patch proposal is the *structured, validated* replacement for free-form model
output. Raw generator text never reaches storage or the API: it is parsed by a
:class:`~app.action_engine.PatchExtractor` into :class:`PatchProposal`, then
safety-checked by :func:`app.patch_validation.validate_patch_proposal`.

This module lives apart from ``app/schemas.py`` because it embeds the canonical
W2.3 :class:`~app.ai.contracts.Finding`, whose module already imports
``Viewport`` from ``app/schemas.py`` (importing it back would be circular).

Nothing here is executed, imported, evaluated or written to disk. The full
validated edits are kept in memory (field ``proposal`` on the record) for the
later apply step; the API returns only a summary of paths and sizes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field

from app.ids import utcnow

PATCH_SCHEMA_VERSION = 1


class RejectionReason(str, Enum):
    """Closed set of machine-readable reasons a proposal is rejected."""

    # Path safety / allow-list.
    path_unsafe = "path_unsafe"
    forbidden_file = "forbidden_file"
    extension_not_allowed = "extension_not_allowed"
    path_not_allowed = "path_not_allowed"
    # Size / structure budgets.
    too_many_files = "too_many_files"
    too_large = "too_large"
    duplicate_path = "duplicate_path"
    empty_edit = "empty_edit"
    invalid_proposal = "invalid_proposal"
    # Generator / extractor lifecycle.
    generator_timeout = "generator_timeout"
    generator_error = "generator_error"
    extractor_not_implemented = "extractor_not_implemented"


class PatchProposalStatus(str, Enum):
    """Outcome of one proposal attempt (never implies the patch was applied)."""

    proposed = "proposed"
    rejected = "rejected"


class PatchEdit(BaseModel):
    """One exact-match replacement: ``old_text`` must appear verbatim in the file.

    An empty ``new_text`` is allowed (it deletes ``old_text``); an empty or
    whitespace-only ``old_text`` is not, because it would match everywhere.
    """

    model_config = {"extra": "forbid"}

    old_text: str = Field(min_length=1, max_length=8000)
    new_text: str = Field(default="", max_length=8000)


class PatchFile(BaseModel):
    """Edits for a single file, addressed by a normalised repo-relative path."""

    model_config = {"extra": "forbid"}

    path: str = Field(min_length=1, max_length=500)
    edits: list[PatchEdit] = Field(min_length=1, max_length=50)


class PatchProposal(BaseModel):
    """Structured patch proposal produced by the extractor (validated form)."""

    model_config = {"extra": "forbid"}

    finding_id: str = Field(min_length=1, max_length=64)
    files: list[PatchFile] = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=4000)
    generator_label: str = Field(min_length=1, max_length=100)
    schema_version: int = PATCH_SCHEMA_VERSION


class PatchFileSummary(BaseModel):
    """Summary of one proposed file: its path, edit count and payload size.

    ``bytes`` is the UTF-8 size of the proposed edit payloads (``old_text`` +
    ``new_text``), i.e. how much text the change would move.
    """

    model_config = {"extra": "forbid"}

    path: str = Field(min_length=1, max_length=500)
    edits: int = Field(ge=0)
    bytes: int = Field(ge=0)


class PatchProposalRecord(BaseModel):
    """One persisted proposal attempt, plus its provenance and outcome.

    ``generator_label`` is always carried so ``MOCK`` output can never be
    mistaken for real model output. ``proposal`` holds the full validated edits
    for the later apply step and is deliberately NOT exposed by the API.
    """

    model_config = {"from_attributes": True}

    proposal_id: str = Field(min_length=1, max_length=64)
    finding_id: str = Field(min_length=1, max_length=64)
    scan_id: UUID | None = None
    status: PatchProposalStatus
    rejection_reasons: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Closed-set reason codes (RejectionReason); empty when proposed.",
    )
    files: list[PatchFileSummary] = Field(default_factory=list, max_length=200)
    generator_label: str = Field(min_length=1, max_length=100)
    is_mock: bool = False
    schema_version: int = PATCH_SCHEMA_VERSION
    rationale: str | None = Field(default=None, max_length=4000)
    attempt: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utcnow)
    proposal: PatchProposal | None = Field(
        default=None,
        description="Full validated edits for the apply step; never returned by the API.",
    )


class PatchProposalResponse(BaseModel):
    """API view of a proposal record: summary only, no editable payloads."""

    proposal_id: str
    finding_id: str
    scan_id: UUID | None = None
    status: PatchProposalStatus
    rejection_reasons: list[str] = []
    files: list[PatchFileSummary] = []
    generator_label: str
    is_mock: bool = False
    schema_version: int = PATCH_SCHEMA_VERSION
    rationale: str | None = None
    attempt: int = 1
    created_at: datetime

    @classmethod
    def from_record(cls, record: PatchProposalRecord) -> "PatchProposalResponse":
        return cls(**record.model_dump(exclude={"proposal"}))


class FindingPatchProposalsResponse(BaseModel):
    """GET /api/v1/findings/{finding_id}/patch-proposals response."""

    finding_id: str
    finding_status: str = Field(
        min_length=1,
        description="Current finding status (patch_proposed blocks further attempts).",
    )
    proposals: list[PatchProposalResponse] = Field(default_factory=list)
