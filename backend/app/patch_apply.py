"""Controlled source patch application inside an isolated workspace (W3.2).

This is the real :class:`app.orchestrator.PatchApplier`. It is the ONLY code
that writes a model-proposed edit to disk, and it writes only inside a
workspace created by :mod:`app.workspace` — never into the tree the API server
runs from, never into ``.git``, never on behalf of text that did not pass the
allow-list.

Order of operations (every check happens before the first byte is written):

1. **Re-validate** the proposal with the same gate that let it become
   ``proposed`` (:func:`app.patch_validation.validate_patch_proposal`). A patch
   can never skip validation by reaching this layer directly.
2. **Create** the workspace (pristine ``baseline/`` plus working ``source/``).
3. **Plan** every file in memory: the path must normalise, be inside the
   working copy, still hash to its baseline value (so it was not changed since
   the snapshot), and each ``old_text`` must occur EXACTLY once.
4. **Diff** the plan against the baseline and refuse an oversized diff (never
   silently truncate).
5. **Write** atomically: a temp file beside the target, then ``os.replace``.
   Line endings are preserved — the ShopLite CSS is CRLF, and a patch that
   silently rewrote the whole file to LF would produce an unreviewable diff.
6. **Verify** the written bytes differ from the baseline; any failure restores
   the baseline so a half-applied workspace is never left behind.

Failure is a :class:`PatchApplyError` with a stable code. The workspace is kept
(marked ``failed``) for the configured retention period so a failed attempt can
be inspected, and is deleted later by
:meth:`app.workspace.WorkspaceManager.cleanup_expired`.
"""

from __future__ import annotations

import logging
import os
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path

from app.config import Settings
from app.orchestrator import ApplyResult, RollbackResult
from app.patch_paths import normalize_relpath
from app.patch_schemas import PatchEdit, PatchProposal, PatchProposalRecord
from app.patch_validation import validate_patch_proposal
from app.workspace import Workspace, WorkspaceError, WorkspaceManager, file_sha256

logger = logging.getLogger("omnisight")

MAX_SUMMARY_EDITS = 20


class PatchApplyError(RuntimeError):
    """A safe, coded patch-application failure.

    ``code`` is stable and machine-readable; ``message`` is a safe summary that
    never contains an absolute path, file contents, a traceback or a secret.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _has_crlf(raw: bytes) -> bool:
    """True when the file uses CRLF line endings (ShopLite's CSS does)."""
    return b"\r\n" in raw


def _lf(text: str) -> str:
    """Normalise a text (file or edit) to LF for matching."""
    return text.replace("\r\n", "\n")


def _apply_edits(text: str, edits: list[PatchEdit], *, relpath: str) -> str:
    """Apply exact-match edits to ``text`` (already LF-normalised)."""
    original = text
    for edit in edits:
        old = _lf(edit.old_text)
        new = _lf(edit.new_text)
        if not old.strip():
            raise PatchApplyError(
                "PATCH_EDIT_EMPTY", f"an edit for {relpath} has empty old_text"
            )
        occurrences = text.count(old)
        if occurrences == 0:
            raise PatchApplyError(
                "PATCH_EDIT_NOT_APPLICABLE",
                f"the edit for {relpath} does not match the current file contents",
            )
        if occurrences > 1:
            # Ambiguity is refused, never resolved by guessing a position.
            raise PatchApplyError(
                "PATCH_EDIT_AMBIGUOUS",
                f"the edit for {relpath} matches {occurrences} places; exactly one is required",
            )
        text = text.replace(old, new, 1)
    if text == original:
        raise PatchApplyError(
            "PATCH_NO_CHANGE", f"the edits for {relpath} would not change the file"
        )
    return text


def _unified_diff(relpath: str, before_lf: str, after_lf: str) -> str:
    """Unified diff with workspace-relative paths only (no absolute paths)."""
    lines = unified_diff(
        before_lf.splitlines(keepends=True),
        after_lf.splitlines(keepends=True),
        fromfile=f"baseline/{relpath}",
        tofile=f"workspace/{relpath}",
        n=3,
        lineterm="\n",
    )
    return "".join(lines)


def _resolve_target(source_root: Path, relpath: str) -> Path:
    """Resolve a proposal path inside the working copy, refusing any escape."""
    candidate = source_root / relpath
    resolved = candidate.resolve()
    try:
        resolved.relative_to(source_root.resolve())
    except ValueError:
        raise PatchApplyError(
            "PATCH_PATH_ESCAPE", f"the path '{relpath}' resolves outside the workspace"
        ) from None
    if candidate.is_symlink() or resolved.is_symlink():
        raise PatchApplyError(
            "PATCH_PATH_ESCAPE", f"the path '{relpath}' is a symlink"
        )
    if not resolved.is_file():
        raise PatchApplyError(
            "PATCH_TARGET_MISSING", f"the path '{relpath}' is not a file in the workspace"
        )
    return resolved


class SourcePatchApplier:
    """Applies a validated proposal to an isolated copy of the source tree.

    Structurally implements :class:`app.orchestrator.PatchApplier`.
    """

    def __init__(self, *, settings: Settings, workspace_manager: WorkspaceManager) -> None:
        self.settings = settings
        self.workspaces = workspace_manager

    # -- public API ---------------------------------------------------------

    def apply(
        self,
        record: PatchProposalRecord,
        proposal: PatchProposal,
        *,
        attempt_id: str | None = None,
    ) -> ApplyResult:
        """Apply the proposal and return the workspace id plus the diff."""
        self._revalidate(record, proposal)
        workspace = self._create_workspace(record, attempt_id)
        try:
            plan = self._plan(workspace, proposal)
            diff = self._build_diff(workspace, plan)
            self._write(workspace, plan)
        except (PatchApplyError, WorkspaceError):
            self._mark_failed(workspace)
            raise
        except OSError as exc:
            self._mark_failed(workspace)
            self._restore(workspace)
            raise PatchApplyError(
                "PATCH_WRITE_FAILED", f"writing the patch failed: {type(exc).__name__}"
            ) from None

        applied = self.workspaces.mark_status(workspace, "applied")
        summary = self._summary(proposal)
        logger.info(
            "patch applied",
            extra={
                "workspace_id": applied.workspace_id,
                "proposal_id": record.proposal_id,
                "files": len(plan),
            },
        )
        return ApplyResult(
            workspace_id=applied.workspace_id, summary=summary, diff=diff
        )

    def rollback(self, workspace_id: str) -> RollbackResult:
        """Restore a workspace to its baseline and prove the bytes match.

        This is the W3.4 undo. It never raises for an ordinary failure: the
        caller records ``confirmed=False`` and stops retrying. A workspace that
        is already gone counts as NOT confirmed — an unverifiable restore is
        never reported as a clean one.
        """
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            return RollbackResult(
                confirmed=False,
                summary=(
                    f"workspace {workspace_id} is no longer present, so its "
                    "baseline could not be verified"
                ),
            )
        try:
            restored = self.workspaces.restore_baseline(workspace)
        except WorkspaceError as exc:
            # ``restore_baseline`` already re-hashes; a raise means it could not
            # prove the restore.
            return RollbackResult(
                confirmed=False,
                summary=f"restore failed ({exc.code}): {exc}",
            )
        except OSError as exc:
            return RollbackResult(
                confirmed=False,
                summary=f"restore failed: {type(exc).__name__}",
            )

        drifted = self.workspaces.verify_against_baseline(restored)
        if drifted:
            return RollbackResult(
                confirmed=False,
                summary=(
                    "the restored workspace still differs from its baseline "
                    f"in {len(drifted)} file(s)"
                ),
                detail={"drifted_paths": drifted[:MAX_SUMMARY_EDITS]},
            )

        self.workspaces.mark_status(restored, "rolled_back")
        logger.info("workspace rolled back", extra={"workspace_id": workspace_id})
        return RollbackResult(
            confirmed=True,
            summary=(
                "workspace restored to its baseline; every recorded file hash "
                "matches"
            ),
            detail={"checked_paths": len(restored.manifest)},
        )

    # -- steps --------------------------------------------------------------

    def _revalidate(self, record: PatchProposalRecord, proposal: PatchProposal) -> None:
        """Re-run the W2 safety gate; a proposal cannot skip it here."""
        reasons = validate_patch_proposal(
            proposal, settings=self.settings, expected_finding_id=record.finding_id
        )
        if reasons:
            codes = ", ".join(r.value for r in reasons)
            raise PatchApplyError(
                "PATCH_REJECTED", f"the proposal failed patch safety validation ({codes})"
            )

    def _create_workspace(
        self, record: PatchProposalRecord, attempt_id: str | None
    ) -> Workspace:
        try:
            return self.workspaces.create(
                scan_id=str(record.scan_id) if record.scan_id else None,
                attempt_id=attempt_id,
            )
        except WorkspaceError as exc:
            raise PatchApplyError(exc.code, str(exc)) from None

    def _plan(
        self, workspace: Workspace, proposal: PatchProposal
    ) -> dict[str, dict]:
        """Compute every file's new content without touching disk.

        Returns ``{relpath: {"before": str, "after": str, "crlf": bool}}``.
        """
        plan: dict[str, dict] = {}
        for patch_file in proposal.files:
            relpath = normalize_relpath(patch_file.path)
            if relpath is None:
                raise PatchApplyError(
                    "PATCH_PATH_UNSAFE", "a proposal path is not a safe relative path"
                )
            recorded = workspace.baseline_hash(relpath)
            if recorded is None:
                raise PatchApplyError(
                    "PATCH_TARGET_MISSING",
                    f"'{relpath}' was not part of the workspace snapshot",
                )

            target = _resolve_target(workspace.source, relpath)
            raw = target.read_bytes()
            if sha256(raw).hexdigest() != recorded:
                # The file changed between snapshot and apply: refuse rather
                # than overwrite somebody else's content.
                raise PatchApplyError(
                    "PATCH_BASELINE_MISMATCH",
                    f"'{relpath}' changed since the workspace baseline was recorded",
                )
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise PatchApplyError(
                    "PATCH_TARGET_NOT_UTF8", f"'{relpath}' is not valid UTF-8"
                ) from None

            before_lf = _lf(decoded)
            after_lf = _apply_edits(before_lf, patch_file.edits, relpath=relpath)
            plan[relpath] = {
                "before": before_lf,
                "after": after_lf,
                "crlf": _has_crlf(raw),
            }
        return plan

    def _build_diff(self, workspace: Workspace, plan: dict[str, dict]) -> str:
        """Unified diff of the plan, refused when it exceeds the configured cap."""
        chunks = [
            _unified_diff(relpath, entry["before"], entry["after"])
            for relpath, entry in sorted(plan.items())
        ]
        diff = "".join(chunks)
        size = len(diff.encode("utf-8"))
        if size > self.settings.max_diff_bytes:
            raise PatchApplyError(
                "PATCH_DIFF_TOO_LARGE",
                (
                    "the patch diff exceeds the reviewable size limit "
                    f"({size} > {self.settings.max_diff_bytes} bytes)"
                ),
            )
        return diff

    def _write(self, workspace: Workspace, plan: dict[str, dict]) -> None:
        """Write each planned file atomically, then verify it really changed."""
        written: list[tuple[str, Path]] = []
        try:
            for relpath, entry in sorted(plan.items()):
                target = _resolve_target(workspace.source, relpath)
                text = entry["after"]
                if entry["crlf"]:
                    # Preserve the file's CRLF endings so the diff stays minimal.
                    text = text.replace("\n", "\r\n")
                tmp = target.with_name(target.name + ".omnisight-tmp")
                with tmp.open("wb") as handle:
                    handle.write(text.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, target)
                written.append((relpath, target))
        except OSError:
            # Roll the whole workspace back: a partial apply is not a result.
            self._restore(workspace)
            raise

        for relpath, target in written:
            baseline = workspace.baseline_hash(relpath)
            if file_sha256(target) == baseline:
                self._restore(workspace)
                raise PatchApplyError(
                    "PATCH_NO_CHANGE", f"'{relpath}' is unchanged after applying the patch"
                )
        drifted = [
            relpath
            for relpath in self.workspaces.verify_against_baseline(workspace)
            if relpath not in plan
        ]
        if drifted:
            # Something other than the proposal changed; never report success.
            self._restore(workspace)
            raise PatchApplyError(
                "PATCH_UNEXPECTED_DRIFT",
                "files outside the proposal changed inside the workspace",
            )

    # -- helpers ------------------------------------------------------------

    def _summary(self, proposal: PatchProposal) -> str:
        parts = [
            f"{rel} ({len(f.edits)} edit{'s' if len(f.edits) != 1 else ''})"
            for rel, f in (
                (normalize_relpath(f.path) or f.path, f) for f in proposal.files
            )
        ]
        shown = parts[:MAX_SUMMARY_EDITS]
        suffix = "" if len(parts) <= MAX_SUMMARY_EDITS else f" and {len(parts) - MAX_SUMMARY_EDITS} more"
        return f"applied to isolated workspace: {', '.join(shown)}{suffix}"

    def _mark_failed(self, workspace: Workspace) -> None:
        try:
            self.workspaces.mark_status(workspace, "failed")
        except (OSError, WorkspaceError):  # pragma: no cover - best effort
            logger.warning(
                "could not mark workspace failed",
                extra={"workspace_id": workspace.workspace_id},
            )

    def _restore(self, workspace: Workspace) -> None:
        try:
            self.workspaces.restore_baseline(workspace)
        except (OSError, WorkspaceError):  # pragma: no cover - best effort
            logger.warning(
                "could not restore workspace baseline",
                extra={"workspace_id": workspace.workspace_id},
            )


def build_patch_applier(settings: Settings) -> SourcePatchApplier:
    """Build the applier from configuration.

    The single construction point the repair wiring
    (:func:`app.routers.repair.register_repair_components`) uses once the real
    validator and verifier exist. Raises
    :class:`~app.workspace.WorkspaceError` (``WORKSPACE_SOURCE_UNSET``) when no
    patch source is configured, so an unconfigured deployment cannot appear to
    have applied anything.
    """
    return SourcePatchApplier(
        settings=settings,
        workspace_manager=WorkspaceManager(settings=settings),
    )
