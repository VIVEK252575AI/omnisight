"""Safe action engine (Week 2, W2.5).

Pipeline for one finding. It never applies edits to a source checkout. Only a
validated proposal is passed to ``PatchStore``, which persists its reviewable
summary and editable patch in the local workflow database:

1. look up the finding; 404 when unknown, 409 when it is not patchable;
2. build a read-only, allow-listed, size-capped source context;
3. ask the replaceable :class:`~app.ai.adapters.patch.PatchGenerator` for RAW
   text, under a wall-clock timeout;
4. hand that text to the :class:`~app.patch_extraction.PatchExtractor`, which
   turns it into a structured :class:`~app.patch_schemas.PatchProposal` (see
   :mod:`app.patch_extraction`, the W2.6 deliverable);
5. safety-validate the proposal (allow-list, extensions, budgets);
6. persist the outcome. Only a fully validated proposal becomes ``proposed``;
   anything else is stored as ``rejected`` with reason codes, and a generator
   failure can never be reported as success.

Generator/extractor failures are **contained**: they produce a clean
``rejected`` record with a reason code and a server-side log line, and they
never change the finding's status, so a corrected attempt can still be made.
Raw text is parsed, never executed, imported, evaluated or shelled out.
"""

from __future__ import annotations

import logging

from fastapi import status

from app.ai.adapters.base import call_with_timeout
from app.ai.adapters.patch import (
    PatchGenerator,
    PatchGeneratorError,
    PatchGeneratorTimeoutError,
)
from app.ai.contracts import Finding, FindingStatus
from app.ai.errors import VisionTimeoutError
from app.config import Settings
from app.detection_schemas import FindingRecord
from app.detection_storage import DetectionStore
from app.errors import DomainError, ErrorCode
from app.ids import new_id, utcnow
from app.patch_extraction import PatchExtractor
from app.patch_paths import normalize_relpath
from app.patch_schemas import (
    PATCH_SCHEMA_VERSION,
    PatchFileSummary,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
    RejectionReason,
)
from app.patch_storage import PatchStore
from app.patch_validation import (
    reasons_as_values,
    summarise_files,
    validate_patch_proposal,
)
from app.source_context import SourceContext, build_source_context

logger = logging.getLogger("omnisight")


class ActionEngine:
    """Turns a validated finding into a persisted (but not applied) proposal."""

    def __init__(
        self,
        settings: Settings,
        *,
        detection_store: DetectionStore,
        patch_store: PatchStore,
        generator: PatchGenerator,
        extractor: PatchExtractor,
    ) -> None:
        self.settings = settings
        self.detection_store = detection_store
        self.patch_store = patch_store
        self.generator = generator
        self.extractor = extractor

    # -- public API ---------------------------------------------------------

    def propose(self, finding_id: str) -> PatchProposalRecord:
        """Run the generation/validation pipeline and persist its outcome."""
        record = self.detection_store.get_finding(finding_id)
        if record is None:
            raise DomainError(
                ErrorCode.FINDING_NOT_FOUND,
                f"finding {finding_id} not found",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        finding = record.finding
        if finding.status is not FindingStatus.detected:
            raise DomainError(
                ErrorCode.FINDING_NOT_PATCHABLE,
                (
                    f"finding {finding_id} is '{finding.status.value}' — only a "
                    "'detected' finding may be proposed against"
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        source_context = build_source_context(finding, self.settings)

        try:
            raw_text = self._generate(finding, source_context)
        except PatchGeneratorTimeoutError as exc:
            return self._store_rejected(
                record, [RejectionReason.generator_timeout.value], message=str(exc)
            )
        except PatchGeneratorError as exc:
            return self._store_rejected(
                record, [RejectionReason.generator_error.value], message=str(exc)
            )
        except Exception as exc:  # pragma: no cover - generator contract breach
            logger.exception("patch generator raised an unexpected error")
            return self._store_rejected(
                record,
                [RejectionReason.generator_error.value],
                message=f"generator raised {type(exc).__name__}",
            )

        try:
            extracted = self.extractor.extract(
                raw_text, finding=finding, source_context=source_context
            )
        except Exception as exc:
            logger.exception("patch extractor raised")
            return self._store_rejected(
                record,
                [RejectionReason.schema_invalid.value],
                message=f"extractor raised {type(exc).__name__}",
            )

        if extracted is None or not extracted.ok or extracted.proposal is None:
            reasons = list(
                (extracted.reasons if extracted else [])
                or [RejectionReason.no_patch_found.value]
            )
            return self._store_rejected(
                record,
                reasons,
                message=extracted.message if extracted else "extractor returned no result",
            )

        proposal = extracted.proposal
        reasons = validate_patch_proposal(
            proposal,
            settings=self.settings,
            expected_finding_id=finding.finding_id,
        )
        if reasons:
            return self._store_rejected(
                record,
                reasons_as_values(reasons),
                files=summarise_files(proposal),
                message="proposal failed safety validation",
                extracted=proposal,
            )

        return self._store_proposed(record, proposal)

    # -- internals ----------------------------------------------------------

    def _generate(self, finding: Finding, source_context: SourceContext) -> str:
        """Call the generator under the configured timeout; return raw text."""
        timeout = self.settings.patch_timeout_seconds

        def call() -> object:
            return self.generator.propose(finding, source_context)

        try:
            result = call_with_timeout(call, timeout)
        except VisionTimeoutError:
            raise PatchGeneratorTimeoutError(
                f"patch generator exceeded the {timeout}s timeout"
            ) from None
        if not isinstance(result, str):
            raise PatchGeneratorError("patch generator returned non-text output")
        return result

    def _store_proposed(
        self, record: FindingRecord, proposal: PatchProposal
    ) -> PatchProposalRecord:
        finding = record.finding
        stored_proposal = self._normalised(proposal)

        claimed = self.detection_store.compare_and_set_finding_status(
            finding.finding_id,
            expected=FindingStatus.detected,
            to_status=FindingStatus.patch_proposed,
        )
        if claimed is None:
            raise DomainError(
                ErrorCode.FINDING_NOT_PATCHABLE,
                (
                    f"finding {finding.finding_id} was claimed by another proposal "
                    "while this one was being generated"
                ),
                status_code=status.HTTP_409_CONFLICT,
            )

        proposal_record = PatchProposalRecord(
            proposal_id=new_id("patch"),
            finding_id=finding.finding_id,
            scan_id=record.scan_id,
            status=PatchProposalStatus.proposed,
            rejection_reasons=[],
            files=summarise_files(stored_proposal),
            generator_label=self._generator_label,
            is_mock=self._is_mock,
            schema_version=proposal.schema_version,
            rationale=proposal.rationale,
            attempt=self._next_attempt(finding.finding_id),
            created_at=utcnow(),
            proposal=stored_proposal,
        )
        self.patch_store.add(proposal_record, stored_proposal)
        logger.info(
            "patch proposal stored",
            extra={
                "proposal_id": proposal_record.proposal_id,
                "finding_id": finding.finding_id,
                "files": len(proposal_record.files),
                "generator": proposal_record.generator_label,
            },
        )
        return proposal_record

    def _store_rejected(
        self,
        record: FindingRecord,
        reasons: list[str],
        *,
        files: list[PatchFileSummary] | None = None,
        message: str | None = None,
        extracted: PatchProposal | None = None,
    ) -> PatchProposalRecord:
        """Persist a rejected attempt without touching the finding's status."""
        rejection = PatchProposalRecord(
            proposal_id=new_id("patch"),
            finding_id=record.finding.finding_id,
            scan_id=record.scan_id,
            status=PatchProposalStatus.rejected,
            rejection_reasons=reasons,
            files=files or [],
            generator_label=self._generator_label,
            is_mock=self._is_mock,
            schema_version=PATCH_SCHEMA_VERSION,
            rationale=extracted.rationale if extracted else None,
            attempt=self._next_attempt(record.finding.finding_id),
            created_at=utcnow(),
            proposal=None,
        )
        self.patch_store.add(rejection)
        # The reason code is what callers see; the free-text detail is logged
        # server-side only so no generator/extractor text reaches a response.
        logger.info(
            "patch proposal rejected reasons=%s detail=%s",
            reasons,
            message or "",
            extra={
                "proposal_id": rejection.proposal_id,
                "finding_id": record.finding.finding_id,
                "generator": rejection.generator_label,
            },
        )
        return rejection

    @staticmethod
    def _normalised(proposal: PatchProposal) -> PatchProposal:
        """Store paths in their canonical form so the apply step never re-guesses."""
        files = [
            patch_file.model_copy(
                update={"path": normalize_relpath(patch_file.path) or patch_file.path}
            )
            for patch_file in proposal.files
        ]
        return proposal.model_copy(update={"files": files})

    def _next_attempt(self, finding_id: str) -> int:
        return self.patch_store.count_for_finding(finding_id) + 1

    @property
    def _generator_label(self) -> str:
        """The generator's own label — never a label the model text claims."""
        return self.generator.generator_label

    @property
    def _is_mock(self) -> bool:
        return bool(getattr(self.generator, "is_mock", False))
