"""Patch-proposal endpoints (Week 2, W2.5).

Endpoints
---------
- ``POST /api/v1/findings/{finding_id}/patch-proposals`` — 201, runs the safe
  action engine and returns the proposal record (``proposed`` or ``rejected``).
- ``GET  /api/v1/findings/{finding_id}/patch-proposals`` — every attempt so far.

Rules
-----
- **404** ``FINDING_NOT_FOUND`` when the finding does not exist.
- **409** ``FINDING_NOT_PATCHABLE`` when the finding is not in a patchable
  state (e.g. it already has a ``proposed`` patch awaiting review).
- **201 with ``status: rejected``** is a normal outcome: a generator or
  extractor failure, an unapproved path or an oversized patch is recorded with
  reason codes and returned — never hidden and never reported as success.
- **Nothing is written to disk.** Approving and applying a proposal is a later
  slice; this endpoint only ever produces a reviewable record.
- The response carries only file paths/sizes and provenance; the editable
  payloads stay server-side for the apply step.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.action_engine import ActionEngine, PatchExtractor, StubPatchExtractor
from app.ai.adapters.patch import PatchGenerator, create_patch_generator
from app.config import Settings, get_settings
from app.detection_storage import DetectionStore, get_detection_store
from app.errors import DomainError, ErrorCode
from app.patch_schemas import (
    FindingPatchProposalsResponse,
    PatchProposalResponse,
)
from app.patch_storage import PatchStore, get_patch_store

router = APIRouter(prefix="/api/v1", tags=["patches"])


def get_patch_generator(settings: Settings = Depends(get_settings)) -> PatchGenerator:
    """Build the configured patch generator (fail-fast on an unknown name).

    Built per action-engine instance rather than at startup, matching the
    detection router: the generator must never be shared mutable state across
    test dependency overrides.
    """
    return create_patch_generator(settings)


def get_patch_extractor() -> PatchExtractor:
    """The structured-output extractor.

    Defaults to the explicit W2.6 stub, so today every proposal is rejected with
    ``extractor_not_implemented``. Tests override this dependency with a
    labelled test double; production gets the real extractor in W2.6.
    """
    return StubPatchExtractor()


def get_action_engine(
    settings: Settings = Depends(get_settings),
    detection_store: DetectionStore = Depends(get_detection_store),
    patch_store: PatchStore = Depends(get_patch_store),
    generator: PatchGenerator = Depends(get_patch_generator),
    extractor: PatchExtractor = Depends(get_patch_extractor),
) -> ActionEngine:
    return ActionEngine(
        settings,
        detection_store=detection_store,
        patch_store=patch_store,
        generator=generator,
        extractor=extractor,
    )


@router.post(
    "/findings/{finding_id}/patch-proposals",
    response_model=PatchProposalResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Unknown finding_id"},
        409: {"description": "Finding is not in a patchable state"},
    },
)
def create_patch_proposal(
    finding_id: str,
    engine: ActionEngine = Depends(get_action_engine),
) -> PatchProposalResponse:
    """Generate and safety-check a patch proposal for one finding.

    Returns 201 for both ``proposed`` and ``rejected``: a rejection is a stored,
    reviewable outcome, not a transport error.
    """
    return PatchProposalResponse.from_record(engine.propose(finding_id))


@router.get(
    "/findings/{finding_id}/patch-proposals",
    response_model=FindingPatchProposalsResponse,
    responses={404: {"description": "Unknown finding_id"}},
)
def list_patch_proposals(
    finding_id: str,
    detection_store: DetectionStore = Depends(get_detection_store),
    patch_store: PatchStore = Depends(get_patch_store),
) -> FindingPatchProposalsResponse:
    """Every proposal attempt recorded for a finding, oldest first."""
    record = detection_store.get_finding(finding_id)
    if record is None:
        raise DomainError(
            ErrorCode.FINDING_NOT_FOUND,
            f"finding {finding_id} not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return FindingPatchProposalsResponse(
        finding_id=finding_id,
        finding_status=record.finding.status.value,
        proposals=[
            PatchProposalResponse.from_record(item)
            for item in patch_store.list_for_finding(finding_id)
        ],
    )
