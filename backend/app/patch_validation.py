"""Safety validation for a structured patch proposal (Week 2, W2.5).

This is the single gate that decides whether a proposal may be stored as
``proposed``. It is deliberately pure (no filesystem access, no side effects)
so every rule can be unit-tested directly, and so a future apply step can
re-run it before touching disk.

All rejections are accumulated — a proposal with an unsafe path *and* an
oversized payload reports both codes — and returned in a deterministic order.
"""

from __future__ import annotations

from app.config import Settings
from app.patch_paths import normalize_relpath, path_rejection
from app.patch_schemas import PatchFileSummary, PatchProposal, RejectionReason


def utf8_size(text: str) -> int:
    """UTF-8 byte length of ``text`` (payloads are counted in bytes, not chars)."""
    return len(text.encode("utf-8"))


def summarise_files(proposal: PatchProposal, *, normalise: bool = True) -> list[PatchFileSummary]:
    """Build the API-facing ``files[]`` summary (paths, edit counts, bytes)."""
    summaries: list[PatchFileSummary] = []
    for patch_file in proposal.files:
        rel = normalize_relpath(patch_file.path) if normalise else patch_file.path
        size = sum(utf8_size(e.old_text) + utf8_size(e.new_text) for e in patch_file.edits)
        summaries.append(
            PatchFileSummary(path=rel or patch_file.path, edits=len(patch_file.edits), bytes=size)
        )
    return summaries


def validate_patch_proposal(
    proposal: PatchProposal,
    *,
    settings: Settings,
    expected_finding_id: str | None = None,
) -> list[RejectionReason]:
    """Return the (possibly empty) reasons the proposal must be rejected.

    Checks, in accumulation order:

    - the proposal is for the finding we are proposing against;
    - the file count stays within ``max_patch_files``;
    - every path normalises, is not forbidden, has an allowed extension and is
      on the approved allow-list;
    - no path is repeated;
    - no edit has an empty/whitespace-only ``old_text``;
    - the total edit payload stays within ``max_patch_bytes``.
    """
    reasons: list[RejectionReason] = []

    if expected_finding_id is not None and proposal.finding_id != expected_finding_id:
        reasons.append(RejectionReason.invalid_proposal)

    if len(proposal.files) > settings.max_patch_files:
        reasons.append(RejectionReason.too_many_files)

    seen: set[str] = set()
    total_bytes = 0
    for patch_file in proposal.files:
        rel = normalize_relpath(patch_file.path)
        if rel is None:
            reasons.append(RejectionReason.path_unsafe)
        else:
            if rel in seen:
                reasons.append(RejectionReason.duplicate_path)
            seen.add(rel)
            rejection = path_rejection(rel, settings)
            if rejection is not None:
                reasons.append(rejection)

        for edit in patch_file.edits:
            if not edit.old_text.strip():
                reasons.append(RejectionReason.empty_edit)
            total_bytes += utf8_size(edit.old_text) + utf8_size(edit.new_text)

    if total_bytes > settings.max_patch_bytes:
        reasons.append(RejectionReason.too_large)

    # Deterministic order, duplicates removed.
    return list(dict.fromkeys(reasons))


def reasons_as_values(reasons: list[RejectionReason]) -> list[str]:
    """Render reasons as their stable string values for storage/API responses."""
    return [reason.value for reason in reasons]
