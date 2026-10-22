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

import ipaddress
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict

from app.action_engine import ActionEngine, PatchExtractor
from app.ai.adapters.patch import PatchGenerator, create_patch_generator
from app.config import Settings, get_settings
from app.detection_storage import DetectionStore, get_detection_store
from app.errors import DomainError, ErrorCode
from app.patch_extraction import SourcePatchExtractor
from app.patch_schemas import (
    FindingPatchProposalsResponse,
    PatchProposalResponse,
)
from app.patch_storage import PatchStore, get_patch_store
from app.shoplite_sandbox import (
    SandboxFailure,
    ShopLiteSandboxReport,
    run_shoplite_sandbox,
)

router = APIRouter(prefix="/api/v1", tags=["patches"])


class ShopLiteSandboxApproval(BaseModel):
    """An explicit, fixed-scope approval; no target or command is accepted."""

    model_config = ConfigDict(extra="forbid")

    approval: Literal["approve-demo-only"]


def get_patch_generator(settings: Settings = Depends(get_settings)) -> PatchGenerator:
    """Build the configured patch generator (fail-fast on an unknown name).

    Built per action-engine instance rather than at startup, matching the
    detection router: the generator must never be shared mutable state across
    test dependency overrides.
    """
    return create_patch_generator(settings)


def get_patch_extractor(
    settings: Settings = Depends(get_settings),
) -> PatchExtractor:
    """The structured-output extractor (W2.6).

    The production default is the deterministic :class:`SourcePatchExtractor`:
    it isolates the patch payload from prose and rejects anything unsafe. Tests
    may override this dependency with a labelled test double.
    """
    return SourcePatchExtractor(settings)


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


@router.post(
    "/demo/shoplite/patch-proposals/{proposal_id}/validate",
    response_model=ShopLiteSandboxReport,
    responses={
        403: {"description": "Missing or invalid local sandbox approval token"},
        404: {"description": "Unknown proposal"},
        409: {"description": "Proposal is not eligible or sandbox could not run"},
    },
)
def validate_shoplite_patch_in_sandbox(
    proposal_id: str,
    approval: ShopLiteSandboxApproval,
    request: Request,
    x_omnisight_demo_approval: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    patch_store: PatchStore = Depends(get_patch_store),
) -> ShopLiteSandboxReport:
    """Human-approved, loopback-development sandbox for the fixed ShopLite demo.

    The API accepts no filesystem path, command, image, or target URL. Candidate
    files are copied to a temporary directory and mounted read-only into a
    bounded, network-disabled container.
    """
    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "The ShopLite sandbox endpoint only accepts loopback requests",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    configured_token = (
        settings.shoplite_sandbox_token.get_secret_value()
        if settings.shoplite_sandbox_token
        else ""
    )
    if not settings.enable_shoplite_sandbox or not configured_token:
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_UNAVAILABLE,
            "The local ShopLite sandbox is not enabled or configured",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not x_omnisight_demo_approval or not secrets.compare_digest(
        x_omnisight_demo_approval, configured_token
    ):
        raise DomainError(
            ErrorCode.UNAUTHORIZED,
            "A valid local ShopLite sandbox approval token is required",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    record = patch_store.get(proposal_id)
    proposal = patch_store.get_proposal(proposal_id)
    if record is None or proposal is None:
        raise DomainError(
            ErrorCode.PATCH_PROPOSAL_NOT_FOUND,
            "Patch proposal not found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if record.status.value != "proposed":
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_REJECTED,
            "Only a validated patch proposal can be checked in the ShopLite demo",
            status_code=status.HTTP_409_CONFLICT,
        )
    try:
        return run_shoplite_sandbox(
            proposal,
            proposal_id=proposal_id,
            settings=settings,
        )
    except SandboxFailure as exc:
        raise DomainError(
            ErrorCode.SHOPLITE_SANDBOX_REJECTED,
            str(exc),
            status_code=status.HTTP_409_CONFLICT,
            details=[{"field": "sandbox", "code": exc.code}],
        ) from None
