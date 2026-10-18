"""Deterministic code-response extraction and rejection (Week 2, W2.6).

This module isolates the *intended patch content* from a model answer that
mixes prose and code, and rejects anything unsafe or ambiguous. Mid-project
"Extraction Check": the backend must reliably pull executable code out of
natural-language AI output **without ever executing it**.

The entry point is :class:`SourcePatchExtractor`, a :class:`PatchExtractor`
implementation that runs, in order:

1. **extraction** (:func:`extract_payload`) — untrusted text -> a JSON payload;
2. **schema** — payload -> :class:`~app.patch_schemas.PatchProposal`;
3. **paths** — normalise + allow-list + forbidden-file + extension;
4. **edit applicability** — read the target file *read-only* and require
   ``old_text`` to occur exactly once;
5. **payload structure** (:mod:`app.patch_code_checks`) — balanced delimiters
   and forbidden constructs, by extension.

Extraction order (a bare ``omnisight-patch`` and nothing weaker):

- the whole text parses as one JSON object -> use it;
- else exactly one ```` ```omnisight-patch ```` fenced block -> use its body;
- else: several tagged blocks -> ``ambiguous_multiple_patches``; output looks
  cut off -> ``truncated``; otherwise ``no_patch_found``.

Untagged generic code fences are **never** treated as patches, and prose is
discarded and counted (``discarded_prose_chars``).

Safety: nothing here executes, imports, evaluates, shells out, or writes to
disk. Model text is parsed into a validated schema first, and text is size-capped
**before** any parsing so a huge response cannot pin memory or drive a
pathological regex. On any uncertainty the answer is rejected.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from app.ai.contracts import Finding
from app.config import Settings
from app.patch_code_checks import check_payload, is_balanced
from app.patch_paths import normalize_relpath, path_rejection
from app.patch_schemas import PATCH_FENCE_TAG, PatchProposal, RejectionReason
from app.source_context import SourceContext, is_within

logger = logging.getLogger("omnisight")

# The one fence tag the generator prompt asks for. Any other tag (or none) is
# not a patch. Matching is case-insensitive on the tag only.
_TAGGED_FENCE = re.compile(
    r"```[ \t]*" + PATCH_FENCE_TAG + r"[ \t]*\r?\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)

# Curly quotes a chat model sometimes emits instead of straight ones. Applied
# ONLY as a fallback after a strict parse has already failed.
_SMART_QUOTES = str.maketrans(
    {"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2018": "'", "\u2019": "'"}
)


class ExtractionStatus(str, Enum):
    """Outcome of one extraction attempt."""

    accepted = "accepted"
    rejected = "rejected"


@dataclass(frozen=True)
class ExtractionResult:
    """Result of turning raw generator text into a structured proposal.

    ``rejected`` is a normal, expected outcome (the model wrote prose, the
    answer was unusable, a construct was unsafe) and is never raised. ``reasons``
    are :class:`~app.patch_schemas.RejectionReason` values.
    """

    status: ExtractionStatus
    proposal: PatchProposal | None = None
    reasons: list[str] = field(default_factory=list)
    discarded_prose_chars: int = 0
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is ExtractionStatus.accepted


@runtime_checkable
class PatchExtractor(Protocol):
    """Turns untrusted generator text into a structured patch proposal.

    Implementations must never execute, import, evaluate, shell out, or write
    the text they receive.
    """

    label: str

    def extract(
        self,
        raw_text: str,
        *,
        finding: Finding,
        source_context: SourceContext,
    ) -> ExtractionResult:
        ...


@dataclass(frozen=True)
class PayloadExtraction:
    """The JSON payload found in the raw text, or why none could be used."""

    text: str | None = None
    reasons: tuple[str, ...] = ()
    discarded_prose_chars: int = 0
    message: str | None = None


# ---------------------------------------------------------------------------
# pure text -> payload
# ---------------------------------------------------------------------------


def _reject(reason: RejectionReason, discarded: int, message: str) -> PayloadExtraction:
    return PayloadExtraction(
        reasons=(reason.value,), discarded_prose_chars=max(discarded, 0), message=message
    )


def _loads(text: str) -> tuple[object | None, json.JSONDecodeError | None]:
    """Parse JSON, falling back to a curly-quote normalisation once."""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as error:
        first_error = error
    if any(ch in text for ch in "\u201c\u201d\u201e\u2018\u2019"):
        try:
            return json.loads(text.translate(_SMART_QUOTES)), None
        except json.JSONDecodeError:
            pass
    return None, first_error


def _looks_truncated(text: str) -> bool:
    """Heuristic: an odd number of fences, or unbalanced delimiters in JSON-like text."""
    if text.count("```") % 2 == 1:
        return True
    stripped = text.strip()
    looks_structured = stripped[:1] in ("{", "[") or "```" in stripped
    if not looks_structured:
        return False
    return not is_balanced(text)


def extract_payload(raw_text: object, *, max_bytes: int) -> PayloadExtraction:
    """Extract the JSON patch payload from ``raw_text`` (never raises).

    ``max_bytes`` caps the UTF-8 size of the input; an oversized response is
    rejected ``too_large`` before any parsing happens.
    """
    if not isinstance(raw_text, str):
        return PayloadExtraction(
            reasons=(RejectionReason.no_patch_found.value,),
            message="generator returned non-text output",
        )

    text = raw_text.lstrip("\ufeff")
    if not text.strip():
        return PayloadExtraction(
            reasons=(RejectionReason.no_patch_found.value,),
            message="generator text is empty",
        )
    if len(text.encode("utf-8")) > max_bytes:
        return _reject(
            RejectionReason.too_large,
            len(text),
            f"generator text exceeds {max_bytes} bytes",
        )

    stripped = text.strip()
    obj, error = _loads(stripped)
    if error is None:
        if isinstance(obj, dict):
            return PayloadExtraction(
                text=stripped, discarded_prose_chars=len(text) - len(stripped)
            )
        return _reject(
            RejectionReason.schema_invalid,
            len(text),
            "top-level JSON must be an object",
        )

    blocks = _TAGGED_FENCE.findall(text)
    if len(blocks) > 1:
        return _reject(
            RejectionReason.ambiguous_multiple_patches,
            len(text),
            f"more than one `{PATCH_FENCE_TAG}` block was found",
        )
    if len(blocks) == 1:
        block = blocks[0].strip()
        discarded = len(text) - len(block)
        obj, error = _loads(block)
        if error is None:
            if isinstance(obj, dict):
                return PayloadExtraction(text=block, discarded_prose_chars=discarded)
            return _reject(
                RejectionReason.schema_invalid,
                discarded,
                f"`{PATCH_FENCE_TAG}` block must contain a JSON object",
            )
        if _looks_truncated(block):
            return _reject(
                RejectionReason.truncated,
                discarded,
                f"`{PATCH_FENCE_TAG}` block looks truncated",
            )
        return _reject(
            RejectionReason.schema_invalid,
            discarded,
            f"`{PATCH_FENCE_TAG}` block is not valid JSON",
        )

    if _looks_truncated(text):
        return _reject(RejectionReason.truncated, len(text), "output looks truncated")
    return _reject(
        RejectionReason.no_patch_found,
        len(text),
        "no patch object found (prose only, or untagged code blocks)",
    )


# ---------------------------------------------------------------------------
# payload -> proposal
# ---------------------------------------------------------------------------


def _normalise_newlines(obj: dict) -> dict:
    """Normalise CRLF/CR inside edit strings so exact-match is robust."""
    files = obj.get("files")
    if not isinstance(files, list):
        return obj
    for patch_file in files:
        if not isinstance(patch_file, dict):
            continue
        edits = patch_file.get("edits")
        if not isinstance(edits, list):
            continue
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            for key in ("old_text", "new_text"):
                value = edit.get(key)
                if isinstance(value, str) and "\r" in value:
                    edit[key] = value.replace("\r\n", "\n").replace("\r", "\n")
    return obj


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", []))
    message = str(first.get("msg", "invalid"))
    return f"{location}: {message}" if location else message


def _build_proposal(
    payload_text: str, *, finding: Finding
) -> tuple[PatchProposal | None, list[str], str | None]:
    obj, error = _loads(payload_text)
    if error is not None or not isinstance(obj, dict):
        return None, [RejectionReason.schema_invalid.value], "payload is not a JSON object"
    obj = _normalise_newlines(obj)
    try:
        proposal = PatchProposal.model_validate(obj)
    except ValidationError as exc:
        return None, [RejectionReason.schema_invalid.value], _first_error(exc)
    if proposal.finding_id != finding.finding_id:
        return (
            None,
            [RejectionReason.schema_invalid.value],
            "proposal finding_id does not match the finding",
        )
    return proposal, [], None


# ---------------------------------------------------------------------------
# the real extractor
# ---------------------------------------------------------------------------


def _dedupe(reasons: list[RejectionReason]) -> list[str]:
    return list(dict.fromkeys(reason.value for reason in reasons))


class SourcePatchExtractor:
    """Deterministic :class:`PatchExtractor` (the production default).

    It never executes the model text. ``source_context`` is accepted for
    symmetry with the generator contract, but edit applicability is verified
    against the file **on disk** (authoritative and newline-normalised) rather
    than the possibly-truncated context copy.
    """

    label = "deterministic-v1"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def extract(
        self,
        raw_text: str,
        *,
        finding: Finding,
        source_context: SourceContext,
    ) -> ExtractionResult:
        try:
            return self._extract(raw_text, finding=finding)
        except Exception as exc:  # defensive: extraction must never crash the flow
            logger.exception("patch extractor failed")
            return ExtractionResult(
                status=ExtractionStatus.rejected,
                reasons=[RejectionReason.schema_invalid.value],
                message=f"extractor error: {type(exc).__name__}",
            )

    # -- internals ----------------------------------------------------------

    def _extract(self, raw_text: str, *, finding: Finding) -> ExtractionResult:
        payload = extract_payload(
            raw_text, max_bytes=self.settings.patch_max_response_bytes
        )
        if payload.text is None:
            return ExtractionResult(
                status=ExtractionStatus.rejected,
                reasons=list(payload.reasons),
                discarded_prose_chars=payload.discarded_prose_chars,
                message=payload.message,
            )

        proposal, reasons, message = _build_proposal(payload.text, finding=finding)
        if proposal is None:
            return ExtractionResult(
                status=ExtractionStatus.rejected,
                reasons=reasons,
                discarded_prose_chars=payload.discarded_prose_chars,
                message=message,
            )

        path_reasons = self._check_paths(proposal)
        if path_reasons:
            return ExtractionResult(
                status=ExtractionStatus.rejected,
                reasons=_dedupe(path_reasons),
                discarded_prose_chars=payload.discarded_prose_chars,
                message="proposal contains an unsafe or unapproved path",
            )

        applicability = self._check_applicability(proposal)
        if applicability:
            return ExtractionResult(
                status=ExtractionStatus.rejected,
                reasons=_dedupe(applicability),
                discarded_prose_chars=payload.discarded_prose_chars,
                message="a proposed edit does not apply exactly once to its target",
            )

        forbidden = self._check_payloads(proposal)
        if forbidden is not None:
            return ExtractionResult(
                status=ExtractionStatus.rejected,
                reasons=[forbidden.value],
                discarded_prose_chars=payload.discarded_prose_chars,
                message="proposed code contains a forbidden or unbalanced construct",
            )

        return ExtractionResult(
            status=ExtractionStatus.accepted,
            proposal=proposal,
            discarded_prose_chars=payload.discarded_prose_chars,
        )

    def _check_paths(self, proposal: PatchProposal) -> list[RejectionReason]:
        reasons: list[RejectionReason] = []
        for patch_file in proposal.files:
            rel = normalize_relpath(patch_file.path)
            if rel is None:
                reasons.append(RejectionReason.path_unsafe)
                continue
            rejection = path_rejection(rel, self.settings)
            if rejection is not None:
                reasons.append(rejection)
        return reasons

    def _check_applicability(self, proposal: PatchProposal) -> list[RejectionReason]:
        reasons: list[RejectionReason] = []
        root = self._resolved_root()
        if root is None:
            # No source tree to read: we cannot confirm the edit applies, so we
            # reject rather than accept blind.
            return [RejectionReason.edit_not_applicable]

        for patch_file in proposal.files:
            rel = normalize_relpath(patch_file.path)
            if rel is None or path_rejection(rel, self.settings) is not None:
                continue  # already reported by the path layer
            target = root / rel
            try:
                resolved = target.resolve()
            except OSError:
                reasons.append(RejectionReason.edit_not_applicable)
                continue
            if not is_within(resolved, root) or not resolved.is_file():
                # A symlink pointing outside the root, or a missing file.
                reasons.append(RejectionReason.edit_not_applicable)
                continue
            try:
                file_text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                reasons.append(RejectionReason.edit_not_applicable)
                continue
            for edit in patch_file.edits:
                occurrences = file_text.count(edit.old_text)
                if occurrences == 0:
                    reasons.append(RejectionReason.edit_not_applicable)
                elif occurrences > 1:
                    reasons.append(RejectionReason.edit_ambiguous)
        return reasons

    def _check_payloads(self, proposal: PatchProposal) -> RejectionReason | None:
        for patch_file in proposal.files:
            rel = normalize_relpath(patch_file.path)
            if rel is None:
                continue
            for edit in patch_file.edits:
                reason = check_payload(rel, edit.new_text)
                if reason is not None:
                    return reason
        return None

    def _resolved_root(self) -> Path | None:
        configured = (self.settings.patch_source_root or "").strip()
        if not configured:
            return None
        root = Path(configured)
        if not root.is_dir():
            return None
        try:
            return root.resolve()
        except OSError:
            return None
