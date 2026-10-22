"""W2.5 tests: the safe action engine (propose -> validate -> persist).

Nothing here applies a patch. The point of these tests is that a proposal can
only ever end up ``proposed`` when it is structured, allow-listed and within
budget — and that everything else is recorded as ``rejected`` with a reason
code, never silently dropped and never reported as success.

``JsonPatchExtractor`` below is an explicitly-labelled TEST DOUBLE standing in
for a generator-output parser; the production default is the real
``SourcePatchExtractor`` (see ``tests/test_patch_extraction.py``).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.ai.adapters.patch import MockPatchGenerator
from app.ai.contracts import Finding, FindingCategory, FindingStatus, Severity
from app.config import Settings
from app.detection_schemas import FindingRecord
from app.patch_extraction import ExtractionResult, ExtractionStatus
from app.patch_paths import normalize_relpath, path_rejection
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchProposal,
    RejectionReason,
)
from app.patch_validation import validate_patch_proposal
from app.source_context import SourceContext, SourceFile, build_source_context

CSS_TEXT = "#submit {\n  width: 220px;\n  color: red;\n}\n"

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Runtime output directories are not source: they are excluded from the
# byte-identity assertion below.
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        "artifacts",
        "data",
    }
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class JsonPatchExtractor:
    """TEST DOUBLE for the W2.6 extractor (not production code).

    Parses the MOCK generator's JSON into a :class:`PatchProposal` so the
    ``proposed`` path can be exercised end to end today.
    """

    label = "test-double-json"

    def __init__(self, mutate=None) -> None:
        self._mutate = mutate
        self.calls: list[str] = []

    def extract(self, raw_text, *, finding, source_context) -> ExtractionResult:
        self.calls.append(raw_text)
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError):
            return ExtractionResult(
                status=ExtractionStatus.rejected,
                reasons=[RejectionReason.schema_invalid.value],
                message="not JSON",
            )
        if self._mutate is not None:
            payload = self._mutate(payload)
        try:
            proposal = PatchProposal(**payload)
        except Exception as exc:  # pydantic ValidationError and friends
            return ExtractionResult(
                status=ExtractionStatus.rejected,
                reasons=[RejectionReason.schema_invalid.value],
                message=type(exc).__name__,
            )
        return ExtractionResult(status=ExtractionStatus.accepted, proposal=proposal)


def _make_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _settings(root: Path, **overrides) -> Settings:
    values: dict = {
        "patch_source_root": str(root),
        "approved_patch_paths": ["styles/**"],
    }
    values.update(overrides)
    return Settings(**values)


def _seed_finding(
    detection_store, *, target: str = "#submit", status: FindingStatus | None = None
) -> str:
    finding = Finding(
        category=FindingCategory.clipped_element,
        target_candidate=target,
        observation="MOCK-injected finding used by action-engine tests.",
        severity=Severity.high,
        confidence=0.9,
        status=status or FindingStatus.detected,
    )
    record = FindingRecord(
        finding=finding,
        scan_id=uuid4(),
        adapter_label="MOCK",
        is_mock=True,
        route="/checkout",
    )
    detection_store.replace_findings(record.scan_id, [record])
    return finding.finding_id


def _propose(client, finding_id: str):
    return client.post(f"/api/v1/findings/{finding_id}/patch-proposals")


def _snapshot(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in _EXCLUDED_DIRS for part in parts):
            continue
        manifest[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return manifest


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_mock_generator_valid_single_file_css_edit_is_proposed(
    tmp_path, patch_client, detection_store, patch_store
):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(extractor=JsonPatchExtractor(), settings=_settings(root))
    finding_id = _seed_finding(detection_store)

    response = _propose(client, finding_id)
    assert response.status_code == 201
    body = response.json()

    assert body["status"] == "proposed"
    assert body["rejection_reasons"] == []
    assert body["generator_label"] == "MOCK"
    assert body["is_mock"] is True
    assert body["schema_version"] == 1
    assert body["attempt"] == 1
    assert [f["path"] for f in body["files"]] == ["styles/checkout.css"]
    assert body["files"][0]["edits"] == 1
    assert body["files"][0]["bytes"] > 0

    # The editable payloads stay server-side.
    assert "old_text" not in json.dumps(body)

    # Exact-match replacement: the stored old_text really occurs in the file.
    stored = patch_store.get_proposal(body["proposal_id"])
    assert stored is not None
    edit = stored.files[0].edits[0]
    assert edit.old_text in CSS_TEXT
    assert edit.new_text != edit.old_text


def test_proposed_path_uses_selector_match(
    tmp_path, patch_client, detection_store
):
    root = _make_tree(
        tmp_path,
        {
            "styles/a.css": ".unrelated {\n  color: red;\n}\n",
            "styles/b.css": "#submit {\n  width: 220px;\n}\n",
        },
    )
    client = patch_client(extractor=JsonPatchExtractor(), settings=_settings(root))
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "proposed"
    assert [f["path"] for f in body["files"]] == ["styles/b.css"]


def test_proposal_is_listed_and_blocks_the_finding(
    tmp_path, patch_client, detection_store
):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(extractor=JsonPatchExtractor(), settings=_settings(root))
    finding_id = _seed_finding(detection_store)

    assert _propose(client, finding_id).json()["status"] == "proposed"

    listing = client.get(f"/api/v1/findings/{finding_id}/patch-proposals")
    assert listing.status_code == 200
    assert listing.json()["finding_status"] == "patch_proposed"
    assert len(listing.json()["proposals"]) == 1

    second = _propose(client, finding_id)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "FINDING_NOT_PATCHABLE"


def test_model_claimed_provenance_is_never_trusted(
    tmp_path, patch_client, detection_store
):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    generator = MockPatchGenerator(
        override_payload={
            "generator_label": "TEAMMATE",
            "files": [
                {
                    "path": "styles/checkout.css",
                    "edits": [{"old_text": "#submit {", "new_text": "#submit {"}],
                }
            ],
        }
    )
    client = patch_client(
        generator=generator, extractor=JsonPatchExtractor(), settings=_settings(root)
    )
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "proposed"
    assert body["generator_label"] == "MOCK"
    assert body["is_mock"] is True


# ---------------------------------------------------------------------------
# rejection: paths
# ---------------------------------------------------------------------------


def test_path_outside_allowlist_is_rejected(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        generator=MockPatchGenerator(
            override_payload={
                "files": [
                    {
                        "path": "src/components/Button.tsx",
                        "edits": [{"old_text": "a", "new_text": "b"}],
                    }
                ]
            }
        ),
        extractor=JsonPatchExtractor(),
        settings=_settings(root),
    )
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert RejectionReason.path_not_approved.value in body["rejection_reasons"]
    assert detection_store.get_finding(finding_id).finding.status.value == "detected"


def test_traversal_paths_are_rejected(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    for unsafe in ("../../etc/passwd.css", "styles/../../escape.css", "/abs/x.css"):
        client = patch_client(
            generator=MockPatchGenerator(
                override_payload={
                    "files": [
                        {"path": unsafe, "edits": [{"old_text": "a", "new_text": "b"}]}
                    ]
                }
            ),
            extractor=JsonPatchExtractor(),
            settings=_settings(root),
        )
        finding_id = _seed_finding(detection_store)
        body = _propose(client, finding_id).json()
        assert body["status"] == "rejected", unsafe
        assert RejectionReason.path_unsafe.value in body["rejection_reasons"], unsafe


def test_forbidden_files_are_rejected(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    for forbidden in ("package-lock.json", "styles/.env.local", ".git/config"):
        client = patch_client(
            generator=MockPatchGenerator(
                override_payload={
                    "files": [
                        {"path": forbidden, "edits": [{"old_text": "a", "new_text": "b"}]}
                    ]
                }
            ),
            extractor=JsonPatchExtractor(),
            settings=_settings(root, approved_patch_paths=["**"]),
        )
        finding_id = _seed_finding(detection_store)
        body = _propose(client, finding_id).json()
        assert body["status"] == "rejected", forbidden
        assert RejectionReason.forbidden_file.value in body["rejection_reasons"], forbidden


def test_disallowed_extension_is_rejected(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        generator=MockPatchGenerator(
            override_payload={
                "files": [
                    {"path": "styles/logo.png", "edits": [{"old_text": "a", "new_text": "b"}]}
                ]
            }
        ),
        extractor=JsonPatchExtractor(),
        settings=_settings(root),
    )
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert RejectionReason.unsupported_file_type.value in body["rejection_reasons"]


def test_empty_allowlist_rejects_everything(tmp_path, patch_client, detection_store):
    """Documents the ShopLite handoff: with no approved paths, nothing is patchable."""
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        extractor=JsonPatchExtractor(),
        settings=_settings(root, approved_patch_paths=[]),
    )
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert RejectionReason.path_not_approved.value in body["rejection_reasons"]


# ---------------------------------------------------------------------------
# rejection: budgets and structure
# ---------------------------------------------------------------------------


def test_too_many_files_is_rejected(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    files = [
        {
            "path": f"styles/patch{i}.css",
            "edits": [{"old_text": "a", "new_text": "b"}],
        }
        for i in range(6)
    ]
    client = patch_client(
        generator=MockPatchGenerator(override_payload={"files": files}),
        extractor=JsonPatchExtractor(),
        settings=_settings(root, max_patch_files=5),
    )
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert RejectionReason.too_many_files.value in body["rejection_reasons"]


def test_too_large_patch_is_rejected(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        generator=MockPatchGenerator(
            override_payload={
                "files": [
                    {
                        "path": "styles/checkout.css",
                        "edits": [{"old_text": "a", "new_text": "x" * 500}],
                    }
                ]
            }
        ),
        extractor=JsonPatchExtractor(),
        settings=_settings(root, max_patch_bytes=64),
    )
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert RejectionReason.too_large.value in body["rejection_reasons"]


def test_duplicate_and_empty_edits_are_rejected(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        generator=MockPatchGenerator(
            override_payload={
                "files": [
                    {
                        "path": "styles/checkout.css",
                        "edits": [{"old_text": "a", "new_text": "b"}],
                    },
                    {
                        "path": "styles/checkout.css",
                        "edits": [{"old_text": "   ", "new_text": "b"}],
                    },
                ]
            }
        ),
        extractor=JsonPatchExtractor(),
        settings=_settings(root),
    )
    finding_id = _seed_finding(detection_store)

    reasons = _propose(client, finding_id).json()["rejection_reasons"]
    assert RejectionReason.duplicate_path.value in reasons
    assert RejectionReason.empty_edit.value in reasons


# ---------------------------------------------------------------------------
# rejection: generator / extractor lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("timeout", RejectionReason.generator_timeout),
        ("error", RejectionReason.generator_error),
    ],
)
def test_generator_failure_is_a_clean_rejection(
    tmp_path, patch_client, detection_store, scenario, expected
):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        generator=MockPatchGenerator(scenario),
        extractor=JsonPatchExtractor(),
        settings=_settings(root),
    )
    finding_id = _seed_finding(detection_store)

    response = _propose(client, finding_id)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "rejected"
    assert expected.value in body["rejection_reasons"]
    assert body["files"] == []
    # A failure never claims the finding, so a corrected attempt is allowed.
    assert detection_store.get_finding(finding_id).finding.status.value == "detected"


def test_default_extractor_is_the_deterministic_extractor(
    tmp_path, patch_client, detection_store, patch_store
):
    """Production default: a valid MOCK proposal against an allow-listed file is accepted."""
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(settings=_settings(root))
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "proposed"
    assert detection_store.get_finding(finding_id).finding.status.value == "patch_proposed"
    assert patch_store.get_proposal(body["proposal_id"]) is not None


def test_default_extractor_rejects_without_an_allowlist(
    tmp_path, patch_client, detection_store, patch_store
):
    """With no approved paths, the default extractor rejects on the path layer."""
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(settings=_settings(root, approved_patch_paths=[]))
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert RejectionReason.path_not_approved.value in body["rejection_reasons"]
    assert detection_store.get_finding(finding_id).finding.status.value == "detected"
    assert patch_store.get_proposal(body["proposal_id"]) is None


def test_unparsable_generator_text_is_rejected(
    tmp_path, patch_client, detection_store
):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(
        generator=MockPatchGenerator("invalid_json"),
        extractor=JsonPatchExtractor(),
        settings=_settings(root),
    )
    finding_id = _seed_finding(detection_store)

    body = _propose(client, finding_id).json()
    assert body["status"] == "rejected"
    assert RejectionReason.schema_invalid.value in body["rejection_reasons"]


def test_retry_after_rejection_succeeds(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    settings = _settings(root)
    failing = patch_client(
        generator=MockPatchGenerator("timeout"),
        extractor=JsonPatchExtractor(),
        settings=settings,
    )
    finding_id = _seed_finding(detection_store)

    first = _propose(failing, finding_id).json()
    assert first["status"] == "rejected"
    assert first["attempt"] == 1

    working = patch_client(
        generator=MockPatchGenerator("ok"),
        extractor=JsonPatchExtractor(),
        settings=settings,
    )
    second = _propose(working, finding_id).json()
    assert second["status"] == "proposed"
    assert second["attempt"] == 2

    attempts = working.get(f"/api/v1/findings/{finding_id}/patch-proposals").json()
    assert [p["status"] for p in attempts["proposals"]] == ["rejected", "proposed"]


# ---------------------------------------------------------------------------
# error handling and transport
# ---------------------------------------------------------------------------


def test_unknown_finding_returns_404(patch_client, tmp_path):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(extractor=JsonPatchExtractor(), settings=_settings(root))

    response = _propose(client, "no-such-finding")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FINDING_NOT_FOUND"

    listing = client.get("/api/v1/findings/no-such-finding/patch-proposals")
    assert listing.status_code == 404
    assert listing.json()["error"]["code"] == "FINDING_NOT_FOUND"


def test_non_patchable_finding_returns_409(tmp_path, patch_client, detection_store):
    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    client = patch_client(extractor=JsonPatchExtractor(), settings=_settings(root))
    finding_id = _seed_finding(
        detection_store, status=FindingStatus.patch_proposed
    )

    response = _propose(client, finding_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FINDING_NOT_PATCHABLE"


# ---------------------------------------------------------------------------
# nothing is written to the source tree
# ---------------------------------------------------------------------------


def test_source_tree_is_byte_identical_across_a_full_propose_flow(
    tmp_path, patch_client, detection_store
):
    """A `proposed` outcome is still only a record — the repo must not change."""
    before = _snapshot(_REPO_ROOT)

    root = _make_tree(tmp_path, {"styles/checkout.css": CSS_TEXT})
    settings = _settings(root)
    client = patch_client(
        generator=MockPatchGenerator("ok"),
        extractor=JsonPatchExtractor(),
        settings=settings,
    )
    finding_id = _seed_finding(detection_store)
    assert _propose(client, finding_id).json()["status"] == "proposed"

    rejected_id = _seed_finding(detection_store)
    rejecting = patch_client(
        generator=MockPatchGenerator("prose_only"),
        settings=settings,
    )
    assert _propose(rejecting, rejected_id).json()["status"] == "rejected"
    client.get(f"/api/v1/findings/{finding_id}/patch-proposals")

    after = _snapshot(_REPO_ROOT)
    assert after == before


def test_shoplite_mock_scenario_proposes_only_the_small_approved_width_edit():
    finding = Finding(
        category=FindingCategory.clipped_element,
        target_candidate="#place-order",
        observation="The checkout button overflows the mobile viewport.",
        evidence="Right edge exceeds the viewport by 167px.",
        severity=Severity.high,
        confidence=0.9,
    )
    source = SourceContext(files=[SourceFile(
        path="styles/checkout.css",
        text="#place-order { width: 520px; height: 48px; }\n",
        bytes=48,
    )])

    payload = json.loads(MockPatchGenerator("shoplite_checkout_fix").propose(finding, source))

    assert payload["files"] == [{
        "path": "styles/checkout.css",
        "edits": [{
            "old_text": "width: 520px;",
            "new_text": "width: 100%; max-width: 100%;",
        }],
    }]


# ---------------------------------------------------------------------------
# unit level: pure path/validation logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("styles/checkout.css", "styles/checkout.css"),
        ("./styles/checkout.css", "styles/checkout.css"),
        ("styles\\checkout.css", "styles/checkout.css"),
        ("styles//checkout.css", "styles/checkout.css"),
        ("/etc/passwd", None),
        ("C:\\Windows\\win.ini", None),
        ("\\\\server\\share\\x.css", None),
        ("../secret.css", None),
        ("styles/../../secret.css", None),
        ("", None),
        ("   ", None),
        ("a\x00b.css", None),
        (None, None),
        (123, None),
    ],
)
def test_normalize_relpath(raw, expected):
    assert normalize_relpath(raw) == expected


def test_path_rejection_reason_codes(tmp_path):
    settings = _settings(tmp_path)
    assert path_rejection("styles/checkout.css", settings) is None
    assert (
        path_rejection("src/app.tsx", settings) is RejectionReason.path_not_approved
    )
    assert (
        path_rejection("styles/logo.png", settings)
        is RejectionReason.unsupported_file_type
    )
    assert path_rejection("package-lock.json", settings) is RejectionReason.forbidden_file
    assert path_rejection(".env", settings) is RejectionReason.forbidden_file
    assert path_rejection(".git/config", settings) is RejectionReason.forbidden_file
    assert (
        path_rejection(".github/workflows/ci.yml", settings)
        is RejectionReason.forbidden_file
    )


def test_forbidden_file_may_be_explicitly_allowlisted(tmp_path):
    settings = _settings(
        tmp_path,
        approved_patch_paths=["package-lock.json"],
        patch_allowed_extensions=[".json"],
    )
    assert path_rejection("package-lock.json", settings) is None
    # A broad glob is NOT an explicit allowlist entry.
    broad = _settings(
        tmp_path,
        approved_patch_paths=["**"],
        patch_allowed_extensions=[".json"],
    )
    assert path_rejection("package-lock.json", broad) is RejectionReason.forbidden_file


def test_validate_patch_proposal_reports_every_problem(tmp_path):
    settings = _settings(tmp_path, max_patch_files=1, max_patch_bytes=4)
    proposal = PatchProposal(
        finding_id="f1",
        rationale="test",
        generator_label="MOCK",
        files=[
            PatchFile(
                path="../escape.css",
                edits=[PatchEdit(old_text="aaaa", new_text="bbbb")],
            ),
            PatchFile(
                path="styles/a.css",
                edits=[PatchEdit(old_text="cccc", new_text="dddd")],
            ),
        ],
    )
    reasons = validate_patch_proposal(
        proposal, settings=settings, expected_finding_id="f1"
    )
    assert RejectionReason.path_unsafe in reasons
    assert RejectionReason.too_many_files in reasons
    assert RejectionReason.too_large in reasons

    mismatch = validate_patch_proposal(
        proposal, settings=settings, expected_finding_id="other"
    )
    assert RejectionReason.schema_invalid in mismatch


# ---------------------------------------------------------------------------
# unit level: source context
# ---------------------------------------------------------------------------


def test_source_context_reads_only_approved_non_secret_files(tmp_path):
    root = _make_tree(
        tmp_path,
        {
            "styles/checkout.css": CSS_TEXT,
            "styles/.env": "SECRET=1\n",
            "src/app.tsx": "export const App = () => null;\n",
            "notes.md": "# notes\n",
        },
    )
    finding = Finding(
        category=FindingCategory.clipped_element,
        target_candidate="#submit",
        observation="x",
    )

    context = build_source_context(finding, _settings(root))
    paths = [f.path for f in context.files]
    assert paths == ["styles/checkout.css"]
    assert context.matched_selectors == {"#submit": ["styles/checkout.css"], "submit": ["styles/checkout.css"]}


def test_source_context_notes_when_unavailable(tmp_path):
    finding = Finding(
        category=FindingCategory.clipped_element,
        target_candidate="#submit",
        observation="x",
    )
    missing = build_source_context(finding, _settings(tmp_path / "nope"))
    assert missing.files == []
    assert missing.note == "source_root_unavailable"

    empty = build_source_context(
        finding, _settings(tmp_path, approved_patch_paths=[])
    )
    assert empty.note == "approved_patch_paths_empty"


def test_source_context_never_follows_a_symlink_out_of_the_root(tmp_path):
    outside = _make_tree(tmp_path / "outside", {"secret.css": "color: red;\n"})
    root = _make_tree(tmp_path / "root", {"styles/checkout.css": CSS_TEXT})
    link = root / "styles" / "linked.css"
    try:
        os.symlink(outside / "secret.css", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

    finding = Finding(
        category=FindingCategory.clipped_element,
        target_candidate="#submit",
        observation="x",
    )
    context = build_source_context(finding, _settings(root))
    assert [f.path for f in context.files] == ["styles/checkout.css"]
