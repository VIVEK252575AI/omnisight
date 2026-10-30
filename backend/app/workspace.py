"""Isolated per-attempt workspaces for applying validated patches (Week 3, W3.2).

A repair attempt may never write into the tree the API server runs from. This
module owns the only place a patch is allowed to land:

    <workspace_root>/<workspace_id>/
        baseline/        pristine copy of the approved source tree
        source/          working copy the applier edits
        workspace.json   metadata + SHA-256 manifest of every copied file

Design rules enforced here:

* **Nothing escapes the root.** The source root must be configured explicitly,
  must be a real directory, and must not be the server's own package
  directory. Every path handed back to callers is resolved and verified to be a
  descendant of ``workspace_root`` (:func:`resolve_inside`); a workspace id is
  validated against a strict pattern before it is ever joined to a path, so
  ``../`` in an id cannot reach anything.
* **No symlinks.** A tree containing a symlink is refused rather than copied,
  because a followed link could read outside the tree and a copied link could
  be used to write through later. This is checked before and after the copy.
* **Bounded.** The copied tree is capped by ``max_workspace_bytes``.
* **Recoverable.** ``baseline/`` is never modified, so rollback is "re-copy
  baseline over source, then verify the hashes equal the manifest".
* **Disposable.** Deleting a workspace is allowed only for a verified child of
  ``workspace_root``, and expired workspaces are cleaned up by age.

This module does not read proposals, decide what to edit or run commands; it
copies, hashes, verifies and deletes. See :mod:`app.patch_apply` for the
applier.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Mapping

from app.config import Settings
from app.patch_paths import path_rejection

logger = logging.getLogger("omnisight")

#: Directories that are never part of a source snapshot. Dependency trees and
#: build output are large, non-source, and would make every attempt slow.
IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "artifacts",
        "data",
        "coverage",
    }
)

_WORKSPACE_ID_RE = re.compile(r"^ws_[0-9a-f]{32}$")
_METADATA_NAME = "workspace.json"
_BASELINE_DIR = "baseline"
_SOURCE_DIR = "source"

#: The API server's own package directory — never a valid patch source.
_SERVER_PACKAGE_DIR = Path(__file__).resolve().parent


class WorkspaceError(RuntimeError):
    """A safe, user-facing workspace failure with a stable code.

    ``code`` is a machine-readable string returned to callers; ``message`` is a
    safe summary. Neither ever contains an absolute path, a traceback or a
    secret.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def resolve_inside(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and prove it stays inside ``root``.

    Raises :class:`WorkspaceError` (``WORKSPACE_PATH_ESCAPE``) otherwise. Used
    for every write and every delete, so traversal, symlink escape and Windows
    drive/UNC tricks are all caught at one place.
    """
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise WorkspaceError(
            "WORKSPACE_PATH_ESCAPE",
            "the resolved path is outside the workspace root",
        ) from None
    return resolved


def file_sha256(path: Path) -> str:
    """SHA-256 of a file's bytes."""
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contains_symlink(root: Path) -> Path | None:
    """Return the first symlink found under ``root``, or None."""
    for path in root.rglob("*"):
        if path.is_symlink():
            return path
    return None


@dataclass(frozen=True)
class Workspace:
    """One isolated attempt workspace, as persisted on disk."""

    workspace_id: str
    root: Path
    source: Path
    baseline: Path
    manifest: Mapping[str, dict]
    created_at: datetime
    source_root: str
    scan_id: str | None = None
    attempt_id: str | None = None
    status: str = "created"

    def baseline_hash(self, relpath: str) -> str | None:
        """Recorded SHA-256 for a relative path, or None when it was not copied."""
        entry = self.manifest.get(relpath)
        return entry.get("sha256") if entry else None

    @property
    def patchable_paths(self) -> list[str]:
        """Relative paths the allow-list says may be edited."""
        return sorted(p for p, e in self.manifest.items() if e.get("patchable"))


class WorkspaceManager:
    """Creates, verifies, restores and disposes isolated attempt workspaces."""

    def __init__(
        self,
        *,
        settings: Settings,
        source_root: Path | str | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.settings = settings
        raw_source = source_root if source_root is not None else settings.patch_source_root
        self._source_root = Path(raw_source) if str(raw_source or "").strip() else None
        self._root = Path(workspace_root or settings.workspace_root)

    # -- locations ---------------------------------------------------------

    @property
    def root(self) -> Path:
        """The configured workspace root (not necessarily created yet)."""
        return self._root

    @property
    def source_root(self) -> Path | None:
        """The configured patchable source tree, or None when unset."""
        return self._source_root

    def _validated_source_root(self) -> Path:
        """Resolve and vet the source tree, or raise a coded error."""
        if self._source_root is None:
            raise WorkspaceError(
                "WORKSPACE_SOURCE_UNSET",
                (
                    "no patch source root is configured — set "
                    "OMNISIGHT_PATCH_SOURCE_ROOT (e.g. ../demo-app) before "
                    "repair attempts can be applied"
                ),
            )
        source = self._source_root.resolve()
        if not source.is_dir():
            raise WorkspaceError(
                "WORKSPACE_SOURCE_MISSING", "the configured patch source root is not a directory"
            )
        if source == _SERVER_PACKAGE_DIR or _SERVER_PACKAGE_DIR.is_relative_to(source):
            # Writing into the running server's own tree is never allowed.
            raise WorkspaceError(
                "WORKSPACE_SOURCE_FORBIDDEN",
                "the configured patch source root contains the API server's own package",
            )
        return source

    def _workspace_dir(self, workspace_id: str) -> Path:
        """Path for an id, after rejecting anything that is not a workspace id."""
        if not _WORKSPACE_ID_RE.match(workspace_id):
            raise WorkspaceError(
                "WORKSPACE_ID_INVALID", "the workspace id is not a valid workspace identifier"
            )
        return resolve_inside(self._root, self._root / workspace_id)

    # -- lifecycle ---------------------------------------------------------

    def create(
        self,
        *,
        scan_id: str | None = None,
        attempt_id: str | None = None,
    ) -> Workspace:
        """Copy the source tree into a new isolated workspace.

        The source is copied twice: ``baseline/`` stays pristine (it is the
        rollback reference) and ``source/`` is the working copy. Both are
        produced from the same bytes so their hashes provably match.
        """
        source = self._validated_source_root()
        symlink = _contains_symlink(source)
        if symlink is not None:
            raise WorkspaceError(
                "WORKSPACE_UNSAFE_SOURCE",
                "the patch source contains a symlink; refusing to copy an escapable tree",
            )

        workspace_id = f"ws_{sha256(f'{source}{scan_id}{attempt_id}{datetime.now(timezone.utc).isoformat()}'.encode()).hexdigest()[:32]}"
        root = self._workspace_dir(workspace_id)
        baseline = root / _BASELINE_DIR
        working = root / _SOURCE_DIR
        try:
            root.mkdir(parents=True, exist_ok=False)
            ignore = shutil.ignore_patterns(*sorted(IGNORED_DIR_NAMES))
            shutil.copytree(source, baseline, ignore=ignore, symlinks=False)
            shutil.copytree(baseline, working, symlinks=False)
        except OSError as exc:
            self._rmtree_quietly(root, workspace_id)
            raise WorkspaceError(
                "WORKSPACE_CREATE_FAILED", f"could not create the workspace copy: {type(exc).__name__}"
            ) from None

        for directory in (baseline, working):
            if _contains_symlink(directory) is not None:
                self._rmtree_quietly(root, workspace_id)
                raise WorkspaceError(
                    "WORKSPACE_UNSAFE_SOURCE", "the copied source contains a symlink"
                )

        manifest = self._build_manifest(baseline)
        total = sum(entry["size"] for entry in manifest.values())
        if total > self.settings.max_workspace_bytes:
            self._rmtree_quietly(root, workspace_id)
            raise WorkspaceError(
                "WORKSPACE_SOURCE_TOO_LARGE",
                (
                    "the patch source exceeds the workspace size limit "
                    f"({total} > {self.settings.max_workspace_bytes} bytes)"
                ),
            )

        workspace = Workspace(
            workspace_id=workspace_id,
            root=root,
            source=working,
            baseline=baseline,
            manifest=manifest,
            created_at=datetime.now(timezone.utc),
            source_root=str(source),
            scan_id=scan_id,
            attempt_id=attempt_id,
            status="created",
        )
        self._write_metadata(workspace)
        logger.info(
            "workspace created",
            extra={
                "workspace_id": workspace_id,
                "files": len(manifest),
                "bytes": total,
            },
        )
        return workspace

    def get(self, workspace_id: str) -> Workspace | None:
        """Load a workspace from disk, or None when it does not exist."""
        try:
            root = self._workspace_dir(workspace_id)
        except WorkspaceError:
            return None
        metadata_path = root / _METADATA_NAME
        if not metadata_path.is_file():
            return None
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(data["created_at"])
        except (OSError, ValueError, KeyError):
            return None
        return Workspace(
            workspace_id=workspace_id,
            root=root,
            source=root / _SOURCE_DIR,
            baseline=root / _BASELINE_DIR,
            manifest=data.get("manifest", {}),
            created_at=created_at,
            source_root=data.get("source_root", ""),
            scan_id=data.get("scan_id"),
            attempt_id=data.get("attempt_id"),
            status=data.get("status", "created"),
        )

    def mark_status(self, workspace: Workspace, status: str) -> Workspace:
        """Record what happened to the workspace (``applied`` / ``failed`` / ...)."""
        updated = Workspace(
            workspace_id=workspace.workspace_id,
            root=workspace.root,
            source=workspace.source,
            baseline=workspace.baseline,
            manifest=workspace.manifest,
            created_at=workspace.created_at,
            source_root=workspace.source_root,
            scan_id=workspace.scan_id,
            attempt_id=workspace.attempt_id,
            status=status,
        )
        self._write_metadata(updated)
        return updated

    def verify_against_baseline(self, workspace: Workspace) -> list[str]:
        """Return relative paths whose bytes no longer match the baseline."""
        drifted: list[str] = []
        for relpath, entry in workspace.manifest.items():
            target = workspace.source / relpath
            if not target.is_file() or target.is_symlink():
                drifted.append(relpath)
                continue
            if file_sha256(target) != entry["sha256"]:
                drifted.append(relpath)
        return sorted(drifted)

    def restore_baseline(self, workspace: Workspace) -> Workspace:
        """Roll the working copy back to ``baseline/`` and prove it matches.

        Raises ``WORKSPACE_ROLLBACK_FAILED`` if the working copy does not end up
        byte-identical to the recorded manifest — a rollback that cannot be
        verified is a failure, not a success.
        """
        root = self._workspace_dir(workspace.workspace_id)
        working = resolve_inside(root, root / _SOURCE_DIR)
        baseline = resolve_inside(root, root / _BASELINE_DIR)
        if not baseline.is_dir():
            raise WorkspaceError(
                "WORKSPACE_ROLLBACK_FAILED", "the workspace baseline is missing"
            )
        shutil.rmtree(working, ignore_errors=True)
        shutil.copytree(baseline, working, symlinks=False)
        restored = Workspace(
            workspace_id=workspace.workspace_id,
            root=root,
            source=working,
            baseline=baseline,
            manifest=workspace.manifest,
            created_at=workspace.created_at,
            source_root=workspace.source_root,
            scan_id=workspace.scan_id,
            attempt_id=workspace.attempt_id,
            status="rolled_back",
        )
        drifted = self.verify_against_baseline(restored)
        if drifted:
            raise WorkspaceError(
                "WORKSPACE_ROLLBACK_FAILED",
                "the restored workspace does not match its baseline manifest",
            )
        self._write_metadata(restored)
        return restored

    def discard(self, workspace_id: str) -> bool:
        """Delete a workspace, refusing anything outside the configured root."""
        root = self._workspace_dir(workspace_id)
        if root == self._root.resolve():
            raise WorkspaceError(
                "WORKSPACE_PATH_ESCAPE", "refusing to delete the workspace root itself"
            )
        if not root.is_dir():
            return False
        shutil.rmtree(root)
        logger.info("workspace discarded", extra={"workspace_id": workspace_id})
        return True

    def cleanup_expired(self, *, now: datetime | None = None) -> list[str]:
        """Delete workspaces older than ``workspace_retention_hours``.

        Only directories that carry this module's metadata and resolve to a
        child of the root are deleted, so a stray directory (or a symlinked
        one) is left alone rather than followed.
        """
        if not self._root.is_dir():
            return []
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(
            hours=self.settings.workspace_retention_hours
        )
        removed: list[str] = []
        for candidate in sorted(self._root.iterdir()):
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            if not _WORKSPACE_ID_RE.match(candidate.name):
                continue
            workspace = self.get(candidate.name)
            if workspace is None or workspace.created_at > cutoff:
                continue
            try:
                if self.discard(candidate.name):
                    removed.append(candidate.name)
            except (OSError, WorkspaceError):  # pragma: no cover - best effort
                logger.warning(
                    "could not clean up workspace", extra={"workspace_id": candidate.name}
                )
        if removed:
            logger.info("cleaned up %d expired workspace(s)", len(removed))
        return removed

    # -- internals ---------------------------------------------------------

    def _build_manifest(self, baseline: Path) -> dict[str, dict]:
        """Hash every copied file; mark which ones the allow-list permits."""
        manifest: dict[str, dict] = {}
        for path in sorted(baseline.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relpath = path.relative_to(baseline).as_posix()
            manifest[relpath] = {
                "sha256": file_sha256(path),
                "size": path.stat().st_size,
                "patchable": path_rejection(relpath, self.settings) is None,
            }
            if len(manifest) > 5_000:
                raise WorkspaceError(
                    "WORKSPACE_SOURCE_TOO_LARGE", "the patch source has too many files"
                )
        return manifest

    def _write_metadata(self, workspace: Workspace) -> None:
        payload = {
            "schema_version": 1,
            "workspace_id": workspace.workspace_id,
            "created_at": workspace.created_at.isoformat(),
            "source_root": workspace.source_root,
            "scan_id": workspace.scan_id,
            "attempt_id": workspace.attempt_id,
            "status": workspace.status,
            "manifest": dict(workspace.manifest),
        }
        target = resolve_inside(workspace.root, workspace.root / _METADATA_NAME)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)

    def _rmtree_quietly(self, path: Path, workspace_id: str) -> None:
        try:
            if self._workspace_dir(workspace_id).is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except (OSError, WorkspaceError):  # pragma: no cover - best effort
            pass
