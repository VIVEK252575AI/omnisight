"""W3.2 tests: isolated workspace creation, controlled patch application, rollback.

Two source trees are used:

* a synthetic mini-project (below) for every safety case, so each rule can be
  provoked deterministically;
* the ACTUAL ShopLite demo tree in this repository (`demo-app/`) for the
  end-to-end apply, proving the real CSS (which is CRLF) survives a patch
  unchanged apart from the edited declaration, and that the tracked file on
  disk is never touched.

No model is called and no Docker container is started.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from app.config import Settings
from app.ids import new_id
from app.patch_apply import PatchApplyError, SourcePatchApplier, build_patch_applier
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
)
from app.workspace import (
    WorkspaceError,
    WorkspaceManager,
    file_sha256,
    resolve_inside,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_ROOT = _REPO_ROOT / "demo-app"
_SERVER_PACKAGE = _REPO_ROOT / "backend" / "app"

CSS = (
    ":root {\r\n"
    "  color-scheme: light;\r\n"
    "}\r\n"
    "\r\n"
    ".checkout-submit {\r\n"
    "  width: 220px;\r\n"
    "  overflow: hidden;\r\n"
    "}\r\n"
)

FINDING_ID = "finding-w32-fixture"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(source_root: Path, workspace_root: Path, **overrides) -> Settings:
    values: dict = {
        "patch_source_root": str(source_root),
        "approved_patch_paths": ["styles/**"],
        "workspace_root": workspace_root,
    }
    values.update(overrides)
    return Settings(**values)


def _mini_tree(root: Path) -> Path:
    """A tiny static project: one approved CSS file plus untouched siblings."""
    (root / "styles").mkdir(parents=True, exist_ok=True)
    (root / "styles" / "checkout.css").write_bytes(CSS.encode("utf-8"))
    (root / "index.html").write_text("<html><body>ok</body></html>\n", encoding="utf-8")
    (root / "app.js").write_text("console.log('ok');\n", encoding="utf-8")
    return root


def _proposal(
    *,
    path: str = "styles/checkout.css",
    old: str = "width: 220px;",
    new: str = "width: 100%;",
    finding_id: str = FINDING_ID,
) -> PatchProposal:
    return PatchProposal(
        finding_id=finding_id,
        files=[PatchFile(path=path, edits=[PatchEdit(old_text=old, new_text=new)])],
        rationale="MOCK proposal used by the W3.2 applier tests.",
        generator_label="MOCK",
    )


def _record(proposal: PatchProposal, *, finding_id: str = FINDING_ID) -> PatchProposalRecord:
    return PatchProposalRecord(
        proposal_id=new_id("prop"),
        finding_id=finding_id,
        status=PatchProposalStatus.proposed,
        generator_label="MOCK",
    )


@pytest.fixture()
def source(tmp_path) -> Path:
    return _mini_tree(tmp_path / "source")


@pytest.fixture()
def workspace_root(tmp_path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture()
def manager(source, workspace_root) -> WorkspaceManager:
    return WorkspaceManager(
        settings=_settings(source, workspace_root),
        source_root=source,
        workspace_root=workspace_root,
    )


@pytest.fixture()
def applier(source, workspace_root) -> SourcePatchApplier:
    settings = _settings(source, workspace_root)
    return SourcePatchApplier(
        settings=settings,
        workspace_manager=WorkspaceManager(
            settings=settings, source_root=source, workspace_root=workspace_root
        ),
    )


def _hash_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_apply_changes_only_the_target_file(applier, source):
    proposal = _proposal()
    before = _hash_tree(source)

    result = applier.apply(_record(proposal), proposal)

    workspace = applier.workspaces.get(result.workspace_id)
    assert workspace is not None
    after = _hash_tree(workspace.source)

    assert set(after) == set(before)
    changed = {rel for rel in after if after[rel] != before[rel]}
    assert changed == {"styles/checkout.css"}
    assert "width: 100%;" in (workspace.source / "styles" / "checkout.css").read_text(
        encoding="utf-8"
    )
    # The source tree itself was only read.
    assert _hash_tree(source) == before


def test_apply_preserves_crlf_line_endings(applier):
    proposal = _proposal()

    result = applier.apply(_record(proposal), proposal)

    workspace = applier.workspaces.get(result.workspace_id)
    raw = (workspace.source / "styles" / "checkout.css").read_bytes()
    # Every newline in the patched file is still CRLF, and no lone LF was added.
    assert raw.count(b"\r\n") == raw.count(b"\n")
    assert b"\r\n" in raw


def test_diff_is_recorded_with_relative_paths_only(applier):
    proposal = _proposal()

    result = applier.apply(_record(proposal), proposal)

    assert result.diff is not None
    assert "--- baseline/styles/checkout.css" in result.diff
    assert "+++ workspace/styles/checkout.css" in result.diff
    assert "-  width: 220px;" in result.diff
    assert "+  width: 100%;" in result.diff
    # No absolute or workspace path leaks into an API-visible field.
    assert str(applier.workspaces.root) not in result.diff
    assert str(result.workspace_id) not in result.diff


def test_factory_builds_a_working_applier_from_configuration(source, workspace_root):
    """The registration seam can construct the real applier from Settings alone."""
    applier = build_patch_applier(_settings(source, workspace_root))
    proposal = _proposal()

    result = applier.apply(_record(proposal), proposal)

    assert result.workspace_id.startswith("ws_")
    assert result.diff is not None
    assert applier.workspaces.get(result.workspace_id) is not None


def test_baseline_snapshot_is_never_modified(applier):
    proposal = _proposal()
    result = applier.apply(_record(proposal), proposal)
    workspace = applier.workspaces.get(result.workspace_id)

    baseline_text = (workspace.baseline / "styles" / "checkout.css").read_text(
        encoding="utf-8"
    )
    assert "width: 220px;" in baseline_text
    assert "width: 100%;" not in baseline_text
    assert workspace.baseline_hash("styles/checkout.css") == sha256(
        (workspace.baseline / "styles" / "checkout.css").read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------------------
# exact-match refusal
# ---------------------------------------------------------------------------


def test_old_text_that_does_not_match_is_refused(applier):
    proposal = _proposal(old="width: 999px;")

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_EDIT_NOT_APPLICABLE"
    # Nothing was written to any workspace that survived the attempt.
    for candidate in applier.workspaces.root.iterdir():
        workspace = applier.workspaces.get(candidate.name)
        assert workspace is None or applier.workspaces.verify_against_baseline(workspace) == []


def test_ambiguous_old_text_is_refused(applier, source):
    """Two matching sites are refused instead of guessing a position."""
    (source / "styles" / "checkout.css").write_bytes(
        b".a { color: red; }\r\n.b { color: red; }\r\n"
    )
    proposal = _proposal(old="color: red;", new="color: blue;")

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_EDIT_AMBIGUOUS"


def test_edit_that_changes_nothing_is_refused(applier):
    proposal = _proposal(old="width: 220px;", new="width: 220px;")

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_NO_CHANGE"


def test_file_changed_since_baseline_is_refused(applier, monkeypatch):
    proposal = _proposal()
    workspace = applier.workspaces.create()
    # Simulate a file edited after the snapshot was taken.
    (workspace.source / "styles" / "checkout.css").write_text(
        ".checkout-submit { width: 1px; }\n", encoding="utf-8"
    )
    monkeypatch.setattr(applier.workspaces, "create", lambda **_kw: workspace)

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_BASELINE_MISMATCH"


def test_target_missing_from_snapshot_is_refused(applier):
    proposal = _proposal(path="styles/nope.css")

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_TARGET_MISSING"


# ---------------------------------------------------------------------------
# proposal re-validation (the applier cannot be used to skip the W2 gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../outside.css",
        "styles/../secret.css",
        "C:/Windows/styles.css",
        "//server/share/styles.css",
        "/etc/styles.css",
    ],
)
def test_unsafe_paths_never_reach_disk(applier, path):
    proposal = _proposal(path=path)

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_REJECTED"


def test_path_outside_the_allowlist_is_refused(applier):
    proposal = _proposal(path="index.html", old="<body>", new="<body class='x'>")

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_REJECTED"


def test_resolve_inside_rejects_traversal(workspace_root):
    workspace_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkspaceError) as exc:
        resolve_inside(workspace_root, workspace_root / ".." / "elsewhere")

    assert exc.value.code == "WORKSPACE_PATH_ESCAPE"


def test_symlink_in_the_source_is_refused(tmp_path):
    root = _mini_tree(tmp_path / "symlinked")
    link = root / "styles" / "escape.css"
    outside = tmp_path / "outside.css"
    outside.write_text(":root {}\n", encoding="utf-8")
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit creating symlinks")

    manager = WorkspaceManager(
        settings=_settings(root, tmp_path / "ws"),
        source_root=root,
        workspace_root=tmp_path / "ws",
    )
    with pytest.raises(WorkspaceError) as exc:
        manager.create()
    assert exc.value.code == "WORKSPACE_UNSAFE_SOURCE"


# ---------------------------------------------------------------------------
# oversized diff
# ---------------------------------------------------------------------------


def test_oversized_diff_is_refused(source, workspace_root):
    settings = _settings(source, workspace_root, max_diff_bytes=1024)
    applier = SourcePatchApplier(
        settings=settings,
        workspace_manager=WorkspaceManager(
            settings=settings, source_root=source, workspace_root=workspace_root
        ),
    )
    big = "/* " + ("wide " * 400) + "*/"
    proposal = _proposal(old="width: 220px;", new=big)

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_DIFF_TOO_LARGE"


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def test_rollback_restores_every_baseline_hash(applier):
    proposal = _proposal()
    result = applier.apply(_record(proposal), proposal)
    workspace = applier.workspaces.get(result.workspace_id)
    baseline = {rel: entry["sha256"] for rel, entry in workspace.manifest.items()}
    assert applier.workspaces.verify_against_baseline(workspace) != []

    restored = applier.workspaces.restore_baseline(workspace)

    assert restored.status == "rolled_back"
    assert applier.workspaces.verify_against_baseline(restored) == []
    assert {
        rel: entry["sha256"] for rel, entry in restored.manifest.items()
    } == baseline
    assert _hash_tree(restored.source) == {
        rel: entry["sha256"] for rel, entry in restored.manifest.items()
    }


def test_applier_rollback_confirms_every_baseline_hash(applier):
    """The W3.4 undo is proven by hash, never merely attempted."""
    proposal = _proposal()
    applied = applier.apply(_record(proposal), proposal)
    workspace = applier.workspaces.get(applied.workspace_id)
    baseline = {rel: entry["sha256"] for rel, entry in workspace.manifest.items()}
    assert _hash_tree(workspace.source) != baseline  # the patch really changed bytes

    result = applier.rollback(applied.workspace_id)

    assert result.confirmed is True
    assert result.detail["checked_paths"] == len(baseline)
    restored = applier.workspaces.get(applied.workspace_id)
    assert restored.status == "rolled_back"
    assert _hash_tree(restored.source) == baseline


def test_applier_rollback_of_a_missing_workspace_is_not_confirmed(applier):
    """A workspace that is gone cannot be verified, so it is not a clean undo."""
    result = applier.rollback("ws_00000000000000000000000000000000")

    assert result.confirmed is False
    assert "no longer present" in result.summary


def test_applier_rollback_of_an_unsafe_id_is_not_confirmed(applier):
    """Rollback never resolves a path outside the workspace root."""
    result = applier.rollback("../../../etc")

    assert result.confirmed is False


def test_write_failure_restores_the_baseline(applier, monkeypatch):
    """A partial apply never leaves a half-patched workspace behind."""
    proposal = _proposal()

    class BrokenReplace:
        """Fail the atomic swap, like a full or locked disk would."""

        fsync = staticmethod(os.fsync)

        @staticmethod
        def replace(*_args, **_kwargs):
            raise OSError("disk full")

    # Patched on this module only; metadata writes still use the real os.replace.
    monkeypatch.setattr("app.patch_apply.os", BrokenReplace)

    with pytest.raises(PatchApplyError) as exc:
        applier.apply(_record(proposal), proposal)

    assert exc.value.code == "PATCH_WRITE_FAILED"
    # The only workspace on disk is back at its baseline.
    workspace_id = next(p.name for p in applier.workspaces.root.iterdir())
    workspace = applier.workspaces.get(workspace_id)
    assert applier.workspaces.verify_against_baseline(workspace) == []


# ---------------------------------------------------------------------------
# workspace safety and cleanup
# ---------------------------------------------------------------------------


def test_source_root_must_be_configured(tmp_path):
    manager = WorkspaceManager(
        settings=Settings(workspace_root=tmp_path / "ws", patch_source_root=""),
        source_root="",
        workspace_root=tmp_path / "ws",
    )
    with pytest.raises(WorkspaceError) as exc:
        manager.create()
    assert exc.value.code == "WORKSPACE_SOURCE_UNSET"


def test_server_package_tree_is_not_a_valid_source(tmp_path):
    manager = WorkspaceManager(
        settings=_settings(_SERVER_PACKAGE, tmp_path / "ws"),
        source_root=_SERVER_PACKAGE,
        workspace_root=tmp_path / "ws",
    )
    with pytest.raises(WorkspaceError) as exc:
        manager.create()
    assert exc.value.code == "WORKSPACE_SOURCE_FORBIDDEN"


def test_cleanup_deletes_expired_workspaces_only(manager, workspace_root, tmp_path):
    old = manager.create()
    fresh = manager.create()
    # Age the workspace through its metadata rather than the filesystem clock.
    metadata = old.root / "workspace.json"
    data = json.loads(metadata.read_text(encoding="utf-8"))
    data["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    metadata.write_text(json.dumps(data), encoding="utf-8")

    outsider = tmp_path / "do-not-delete"
    outsider.mkdir()
    (outsider / "keep.txt").write_text("keep", encoding="utf-8")

    removed = manager.cleanup_expired()

    assert removed == [old.workspace_id]
    assert not old.root.exists()
    assert fresh.root.exists()
    # A directory outside the workspace root is never touched.
    assert outsider.exists()
    assert (outsider / "keep.txt").is_file()


def test_discard_refuses_a_traversing_id(manager):
    with pytest.raises(WorkspaceError) as exc:
        manager.discard("../../backend/app")
    assert exc.value.code == "WORKSPACE_ID_INVALID"


def test_manifest_marks_only_allowlisted_paths_patchable(manager):
    workspace = manager.create()

    assert workspace.patchable_paths == ["styles/checkout.css"]
    assert workspace.manifest["index.html"]["patchable"] is False
    assert workspace.manifest["app.js"]["patchable"] is False


# ---------------------------------------------------------------------------
# the real ShopLite tree
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DEMO_ROOT.is_dir(), reason="ShopLite demo tree is not present")
def test_real_shoplite_css_patch_on_a_copy(tmp_path):
    """End-to-end against the ACTUAL demo tree, without modifying it."""
    tracked = _DEMO_ROOT / "styles" / "checkout.css"
    tracked_hash = file_sha256(tracked)
    settings = _settings(
        _DEMO_ROOT,
        tmp_path / "ws",
        approved_patch_paths=["styles/checkout.css"],
    )
    manager = WorkspaceManager(
        settings=settings, source_root=_DEMO_ROOT, workspace_root=tmp_path / "ws"
    )
    applier = SourcePatchApplier(settings=settings, workspace_manager=manager)

    # The demo's real injected defect: `#place-order { width: 520px; }` clips
    # the checkout button at a 390px mobile viewport.
    real_text = tracked.read_bytes().decode("utf-8")
    assert "#place-order {" in real_text
    old = "#place-order { width: 520px;"
    assert real_text.count(old) == 1

    proposal = _proposal(old=old, new="#place-order { width: min(100%, 520px);")
    result = applier.apply(_record(proposal), proposal)

    workspace = manager.get(result.workspace_id)
    patched = (workspace.source / "styles" / "checkout.css").read_bytes()
    assert patched.count(b"#place-order { width: min(100%, 520px);") == 1
    assert patched.count(b"\r\n") == patched.count(b"\n")
    assert result.diff is not None
    assert "+#place-order { width: min(100%, 520px);" in result.diff

    # Rollback on the real tree restores every recorded baseline hash.
    restored = manager.restore_baseline(workspace)
    assert manager.verify_against_baseline(restored) == []
    assert file_sha256(restored.source / "styles" / "checkout.css") == tracked_hash

    # The tracked file in the repository is untouched.
    assert file_sha256(tracked) == tracked_hash


def test_server_source_tree_unchanged_by_a_full_cycle(source, workspace_root):
    """Acceptance: the API server's own source is byte-identical afterwards."""
    server_before = _hash_tree(_SERVER_PACKAGE)
    settings = _settings(source, workspace_root)
    applier = SourcePatchApplier(
        settings=settings,
        workspace_manager=WorkspaceManager(
            settings=settings, source_root=source, workspace_root=workspace_root
        ),
    )
    proposal = _proposal()
    result = applier.apply(_record(proposal), proposal)
    applier.workspaces.restore_baseline(applier.workspaces.get(result.workspace_id))

    assert _hash_tree(_SERVER_PACKAGE) == server_before
