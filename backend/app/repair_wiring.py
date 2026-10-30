"""Assemble the real repair components from configuration (Week 3).

One place decides whether a deployment can run the self-healing loop, and with
which implementations:

===============  ==========================================================
applier          :class:`app.patch_apply.SourcePatchApplier` (isolated copy)
validator        :class:`app.patch_verifier.WorkspaceValidator` (configured
                 build/test commands)
verifier         :class:`app.patch_verifier.SourcePatchVerifier` (fresh
                 capture + deterministic check + VLM re-check + policy)
VLM verifier     ``OMNISIGHT_VLM_VERIFIER`` (``mock`` by default, labelled
                 ``MOCK``)
===============  ==========================================================

Everything is built lazily, per request, from the stores the API already
injected, so the verifier sees the same detection/evidence records the rest of
the request does.

Returning ``None`` is a first-class outcome: without the feature flag, a patch
source root, or a resolvable VLM verifier, the loop cannot run honestly and
``POST /repair`` keeps answering ``409 REPAIR_UNAVAILABLE``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.ai.adapters.base import VisionAnalyzer
from app.ai.adapters.factory import create_vision_analyzer
from app.ai.errors import VisionConfigurationError
from app.config import Settings
from app.detection_storage import DetectionStore
from app.evidence_capture import ShopLiteCheckoutCollector
from app.evidence_storage import EvidenceStore
from app.orchestrator import DeliveryService, EvidenceCollector
from app.patch_apply import SourcePatchApplier
from app.patch_verifier import SourcePatchVerifier, WorkspaceValidator
from app.verification import build_vlm_verifier
from app.workspace import WorkspaceManager

logger = logging.getLogger("omnisight")


@dataclass(frozen=True)
class RepairComponents:
    """The component implementations the repair loop drives.

    ``delivery`` and ``evidence_collector`` stay optional: PyGithub delivery is
    human-gated, and the Playwright collector is used by the verifier directly
    (the orchestrator's own collector slot is for a future generic re-capture).
    """

    applier: object
    validator: object
    verifier: object
    delivery: DeliveryService | None = None
    evidence_collector: EvidenceCollector | None = None


def _configured(settings: Settings) -> bool:
    """True when the deployment is allowed and able to run the loop."""
    if not settings.enable_repair_orchestrator:
        return False
    if not str(settings.patch_source_root or "").strip():
        logger.info(
            "repair loop unavailable: OMNISIGHT_PATCH_SOURCE_ROOT is not configured"
        )
        return False
    return True


def build_repair_components(
    *,
    settings: Settings,
    detection_store: DetectionStore,
    evidence_store: EvidenceStore,
    analyzer: VisionAnalyzer | None = None,
) -> RepairComponents | None:
    """Build the real components, or ``None`` when the loop cannot run."""
    if not _configured(settings):
        return None

    workspaces = WorkspaceManager(settings=settings)
    try:
        resolved_analyzer = analyzer or create_vision_analyzer(
            settings, resolver=evidence_store.get_artifact
        )
        vlm = build_vlm_verifier(settings, resolved_analyzer)
    except (VisionConfigurationError, ValueError) as exc:
        logger.warning("repair loop unavailable: %s", exc)
        return None

    collector = ShopLiteCheckoutCollector(
        settings=settings, evidence_store=evidence_store
    )
    return RepairComponents(
        applier=SourcePatchApplier(settings=settings, workspace_manager=workspaces),
        validator=WorkspaceValidator(settings=settings, workspace_manager=workspaces),
        verifier=SourcePatchVerifier(
            settings=settings,
            detection_store=detection_store,
            evidence_store=evidence_store,
            workspace_manager=workspaces,
            vlm=vlm,
            collector=collector,
        ),
    )
