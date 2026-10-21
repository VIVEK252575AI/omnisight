"""Patch generator boundary (Week 2, W2.5).

:class:`PatchGenerator` is the single replaceable interface that turns a
validated finding plus the approved :class:`~app.source_context.SourceContext`
into RAW TEXT. Like the VLM adapter it mirrors:

- the interface knows nothing about a provider, model id, key or endpoint;
- implementations must not parse, execute, import, interpret or persist the
  result — that is the extractor/validator's job downstream;
- the returned text is UNTRUSTED and only ever becomes a stored record after
  :func:`app.patch_validation.validate_patch_proposal` approves it.

The factory selects the implementation from ``OMNISIGHT_PATCH_GENERATOR``;
``mock`` (deterministic, offline, labelled ``MOCK``) is the default, and
``gemini`` is the optional server-side Google Gemini implementation. An
unknown value fails fast rather than silently falling back.

Interface, errors, the mock and the factory share this module because the
boundary is deliberately tiny — unlike the VLM adapter there is no image
resolution or payload budget to separate out.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from app.ai.contracts import Finding
from app.config import Settings
from app.patch_schemas import PATCH_FENCE_TAG, PATCH_SCHEMA_VERSION, RejectionReason
from app.source_context import SourceContext, SourceFile

MOCK_LABEL = "MOCK"
FENCE_TAG = PATCH_FENCE_TAG
MOCK_PATCH_GENERATOR = "mock"
TEAMMATE_PATCH_GENERATOR = "teammate"
GEMINI_PATCH_GENERATOR = "gemini"
KNOWN_PATCH_GENERATORS = (
    MOCK_PATCH_GENERATOR,
    GEMINI_PATCH_GENERATOR,
    TEAMMATE_PATCH_GENERATOR,
)

# Offline, deterministic scenarios for :class:`MockPatchGenerator`. The extras
# exist so the W2.6 extraction rules can be exercised without a provider:
#   prose_with_fence    prose + exactly one `omnisight-patch` block (accepted)
#   two_plain_fences    two untagged code blocks, no payload (no_patch_found)
#   two_tagged_fences   two `omnisight-patch` blocks (ambiguous_multiple_patches)
#   prose_only          no JSON at all (no_patch_found)
#   truncated           cut-off JSON (truncated)
SCENARIOS = (
    "ok",
    "timeout",
    "error",
    "invalid_json",
    "empty",
    "prose_with_fence",
    "two_plain_fences",
    "two_tagged_fences",
    "prose_only",
    "truncated",
)

_CSS_LIKE = {".css", ".scss"}


class PatchGeneratorError(Exception):
    """A normalised patch-generator failure (no provider detail attached)."""

    reason: RejectionReason = RejectionReason.generator_error

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PatchGeneratorTimeoutError(PatchGeneratorError):
    """The generator did not respond within the configured timeout."""

    reason: RejectionReason = RejectionReason.generator_timeout


@runtime_checkable
class PatchGenerator(Protocol):
    """A pluggable source of raw patch-proposal text."""

    generator_label: str
    is_mock: bool

    def propose(self, finding: Finding, source_context: SourceContext) -> str:
        """Return raw, untrusted text describing a proposed edit.

        Transport failures raise :class:`PatchGeneratorError` (or
        :class:`PatchGeneratorTimeoutError`). The returned text is never
        executed, imported or written to disk by the caller before it has been
        parsed and validated.
        """
        ...


def _comment_for(path: str) -> str:
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return "/* OmniSight MOCK patch */" if suffix in _CSS_LIKE else "// OmniSight MOCK patch"


def _pick_source_file(source_context: SourceContext) -> SourceFile | None:
    """Prefer a file the finding's selector matched, else the first file."""
    for hits in source_context.matched_selectors.values():
        for path in hits:
            for source_file in source_context.files:
                if source_file.path == path:
                    return source_file
    return source_context.files[0] if source_context.files else None


class MockPatchGenerator:
    """Scripted, network-free :class:`PatchGenerator` (labelled ``MOCK``).

    ``scenario`` drives the failure paths deterministically. ``override_payload``
    lets a test inject a structured payload (e.g. too many files) without
    inventing another generator implementation; it is still produced by the
    MOCK generator and is therefore still labelled ``MOCK``.
    """

    generator_label = MOCK_LABEL
    is_mock = True

    def __init__(
        self,
        scenario: str = "ok",
        *,
        override_payload: dict | None = None,
        fallback_path: str = "styles/checkout.css",
        timeout_seconds: float = 30.0,
    ) -> None:
        if scenario not in SCENARIOS:
            raise PatchGeneratorError(
                f"unknown mock scenario '{scenario}'; known: {', '.join(SCENARIOS)}"
            )
        self.scenario = scenario
        self.override_payload = override_payload
        self.fallback_path = fallback_path
        self.timeout_seconds = timeout_seconds
        self.calls: list[dict] = []

    def propose(self, finding: Finding, source_context: SourceContext) -> str:
        self.calls.append(
            {
                "finding_id": finding.finding_id,
                "context_files": len(source_context.files),
            }
        )
        if self.scenario == "timeout":
            raise PatchGeneratorTimeoutError("MOCK: simulated generator timeout")
        if self.scenario == "error":
            raise PatchGeneratorError("MOCK: simulated generator failure")
        if self.scenario == "empty":
            return ""
        if self.scenario == "invalid_json":
            return "I would try widening the button. (not JSON)"
        if self.scenario == "prose_only":
            return (
                "Here is my suggestion.\n\nI recommend widening the submit "
                "button so it is fully visible on a 390px viewport.\n"
            )

        body = json.dumps(self._payload(finding, source_context))
        if self.scenario == "prose_with_fence":
            return (
                "Sure — here is the patch:\n\n"
                f"```{FENCE_TAG}\n{body}\n```\n\n"
                "Let me know if you want more detail.\n"
            )
        if self.scenario == "two_plain_fences":
            return f"First idea:\n\n```\n{body}\n```\n\nSecond idea:\n\n```\n{body}\n```\n"
        if self.scenario == "two_tagged_fences":
            return f"```{FENCE_TAG}\n{body}\n```\n\n```{FENCE_TAG}\n{body}\n```\n"
        if self.scenario == "truncated":
            # Cut the JSON mid-object so it is unbalanced and unparsable.
            return body[: max(1, len(body) - 3)]
        return body  # "ok"

    def _payload(self, finding: Finding, source_context: SourceContext) -> dict:
        base: dict = {
            "finding_id": finding.finding_id,
            "rationale": (
                f"MOCK: minimal single-file edit addressing "
                f"{finding.category.value}."
            ),
            "generator_label": MOCK_LABEL,
            "schema_version": PATCH_SCHEMA_VERSION,
        }
        if self.override_payload is not None:
            base.update(self.override_payload)
            base.setdefault("files", [])
            return base

        source_file = _pick_source_file(source_context)
        if source_file is None or not source_file.text.strip():
            path, old_text = self.fallback_path, "color: red;"
        else:
            # A whole-file, balanced replacement keeps the MOCK edit valid under
            # the W2.6 structural checks (no dangling `{`).
            path, old_text = source_file.path, source_file.text

        new_text = f"{old_text.rstrip()}\n{_comment_for(path)}\n"
        base["files"] = [
            {"path": path, "edits": [{"old_text": old_text, "new_text": new_text}]}
        ]
        return base


def create_patch_generator(settings: Settings) -> PatchGenerator:
    """Build the configured :class:`PatchGenerator`, or fail fast."""
    name = (settings.patch_generator or MOCK_PATCH_GENERATOR).strip().lower()

    if name == MOCK_PATCH_GENERATOR:
        return MockPatchGenerator(
            settings.patch_mock_scenario,
            timeout_seconds=settings.patch_timeout_seconds,
        )

    if name == GEMINI_PATCH_GENERATOR:
        from app.ai.adapters.gemini_patch import GeminiPatchGenerator

        return GeminiPatchGenerator(settings)

    if name == TEAMMATE_PATCH_GENERATOR:
        raise PatchGeneratorError(
            "OMNISIGHT_PATCH_GENERATOR='teammate' requires an injected teammate "
            "callable; the teammate patch generator is not integrated yet"
        )

    raise PatchGeneratorError(
        f"unknown OMNISIGHT_PATCH_GENERATOR '{name}'; known: "
        f"{', '.join(KNOWN_PATCH_GENERATORS)}"
    )
