"""W2.7 test matrix: valid, malformed, missing, unsupported and failed Week 2 calls.

This module is the *consolidation* layer over the Week 2 work. Rather than
re-listing inline cases (already covered by ``test_findings.py``,
``test_vlm_adapter.py``, ``test_detection.py``, ``test_action_engine.py`` and
``test_patch_extraction.py``), it drives a small **fixture corpus** described by
two manifests:

- ``tests/fixtures/vlm_responses/`` — raw VLM output -> :func:`parse_vlm_output`;
- ``tests/fixtures/ai_patch_responses/`` — raw generator output ->
  :class:`~app.patch_extraction.SourcePatchExtractor`.

Design goals:

- **Corpus-driven.** Adding a fixture file + a manifest entry adds a test; the
  manifest is the single source of truth for the expected result. Deleting a
  file or corrupting an expectation makes the matrix fail loudly.
- **No duplication.** Runners reuse the existing detection/patch helpers and the
  production services rather than re-implementing wiring.
- **Honest provenance.** Every fixture is labelled ``kind`` (``real`` = captured
  from a live model, ``fixture`` = hand-authored). No live model was available,
  so the corpus is entirely ``fixture`` and the integration rows run against
  offline ``MOCK`` analysers.

Nothing here executes, imports or evaluates model text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.adapters import MockVisionAnalyzer
from app.ai.errors import VisionRateLimitedError
from app.ai.parsing import parse_vlm_output
from app.config import Settings
from app.patch_extraction import SourcePatchExtractor
from app.source_context import build_source_context
from tests.test_detection import (
    _detect,
    _evidence_ready,
    _findings,
    _scan_status,
)
from tests.test_patch_extraction import _finding, _make_tree

FIXTURES = Path(__file__).parent / "fixtures"
VLM_DIR = FIXTURES / "vlm_responses"
PATCH_DIR = FIXTURES / "ai_patch_responses"

VALID_KINDS = frozenset({"real", "fixture"})


def _load_manifest(directory: Path) -> dict:
    """Read a corpus manifest (raises loudly if it is missing or malformed)."""
    raw = (directory / "manifest.json").read_text(encoding="utf-8")
    return json.loads(raw)


_VLM_MANIFEST = _load_manifest(VLM_DIR)
_PATCH_MANIFEST = _load_manifest(PATCH_DIR)
VLM_ENTRIES: list[dict] = _VLM_MANIFEST["entries"]
PATCH_ENTRIES: list[dict] = _PATCH_MANIFEST["entries"]


# ---------------------------------------------------------------------------
# Scenario coverage requirements (a case can never be silently dropped)
# ---------------------------------------------------------------------------

REQUIRED_VLM_SCENARIOS = frozenset(
    {
        "valid_single_finding",
        "valid_multiple_findings",
        "valid_no_defects",
        "valid_needs_inspection",
        "missing_required_field",
        "wrong_types",
        "unknown_category",
        "contradictory_outcome",
        "non_json_prose",
        "truncated_json",
        "json_in_prose",
        "empty_body",
    }
)

REQUIRED_PATCH_SCENARIOS = frozenset(
    {
        "valid_css_edit",
        "valid_tsx_edit",
        "unsupported_file_type",
        "unapproved_path",
        "ambiguous_blocks",
        "forbidden_construct",
        "edit_mismatch",
    }
)

# (scenario label, expected detection run reason_code)
INTEGRATION_ROWS: tuple[tuple[str, str], ...] = (
    ("timeout", "timeout"),
    ("provider_unavailable", "provider_unavailable"),
    ("rate_limited", "rate_limited"),
    ("adapter_exception", "interrupted"),
    ("oversized_image", "budget_exceeded"),
    ("malformed_output_not_json", "not_json"),
    ("malformed_output_schema_invalid", "schema_invalid"),
)
INTEGRATION_SCENARIOS = tuple(label for label, _ in INTEGRATION_ROWS)


# ---------------------------------------------------------------------------
# Offline analysers for the integration-failure rows (every one is MOCK)
# ---------------------------------------------------------------------------


class _RateLimitedAnalyzer:
    """MOCK analyser that raises a *transport* rate-limit (no mock scenario exists)."""

    provider_label = "MOCK"
    model_label = "mock-rate-limited"
    is_mock = True

    def __init__(self, resolver=None) -> None:
        self._resolver = resolver

    def analyze(self, request, *, repair: bool = False):
        raise VisionRateLimitedError("FIXTURE: simulated provider rate limit")


class _CrashAnalyzer:
    """MOCK analyser that raises an *unexpected* error (crash-safety path)."""

    provider_label = "BOOM"
    model_label = None
    is_mock = True

    def analyze(self, request, *, repair: bool = False):
        raise RuntimeError("FIXTURE: simulated adapter crash")


def _integration_analyzers() -> dict[str, object]:
    """Map each integration scenario to a factory taking the evidence store."""
    return {
        "timeout": lambda ev: MockVisionAnalyzer("timeout", resolver=ev.get_artifact),
        "provider_unavailable": lambda ev: MockVisionAnalyzer(
            "provider_error", resolver=ev.get_artifact
        ),
        "rate_limited": lambda ev: _RateLimitedAnalyzer(ev.get_artifact),
        "adapter_exception": lambda ev: _CrashAnalyzer(),
        # A 1-byte budget guarantees the fixture screenshot is "oversized".
        "oversized_image": lambda ev: MockVisionAnalyzer(
            "clipped_button", resolver=ev.get_artifact, max_image_bytes=1
        ),
        "malformed_output_not_json": lambda ev: MockVisionAnalyzer(
            "malformed_text", resolver=ev.get_artifact
        ),
        "malformed_output_schema_invalid": lambda ev: MockVisionAnalyzer(
            "malformed_schema", resolver=ev.get_artifact
        ),
    }


# ---------------------------------------------------------------------------
# Manifest integrity (the corpus is the source of truth)
# ---------------------------------------------------------------------------


def test_vlm_manifest_files_exist_and_no_orphans():
    listed = {entry["file"] for entry in VLM_ENTRIES}
    present = {p.name for p in VLM_DIR.iterdir() if p.name != "manifest.json"}
    assert listed == present, f"manifest/dir mismatch: {listed ^ present}"
    for entry in VLM_ENTRIES:
        assert (VLM_DIR / entry["file"]).is_file()


def test_patch_manifest_files_exist_and_no_orphans():
    listed = {entry["response_file"] for entry in PATCH_ENTRIES}
    present = {p.name for p in PATCH_DIR.iterdir() if p.name != "manifest.json"}
    assert listed == present, f"manifest/dir mismatch: {listed ^ present}"
    for entry in PATCH_ENTRIES:
        assert (PATCH_DIR / entry["response_file"]).is_file()


def test_manifests_cover_required_scenarios():
    vlm_scenarios = {entry["scenario"] for entry in VLM_ENTRIES}
    patch_scenarios = {entry["scenario"] for entry in PATCH_ENTRIES}
    assert REQUIRED_VLM_SCENARIOS <= vlm_scenarios, REQUIRED_VLM_SCENARIOS - vlm_scenarios
    assert REQUIRED_PATCH_SCENARIOS <= patch_scenarios, (
        REQUIRED_PATCH_SCENARIOS - patch_scenarios
    )
    assert set(INTEGRATION_SCENARIOS) <= set(_integration_analyzers())


def test_corpus_provenance_is_labelled():
    """Every entry is explicitly ``real`` or ``fixture``; report the counts."""
    all_entries = [*VLM_ENTRIES, *PATCH_ENTRIES]
    for entry in all_entries:
        assert entry["kind"] in VALID_KINDS, entry
        assert entry["kind"] == "fixture" or entry.get("source"), (
            "a 'real' fixture must record where it was captured from"
        )
    fixture_count = sum(1 for e in all_entries if e["kind"] == "fixture")
    real_count = sum(1 for e in all_entries if e["kind"] == "real")
    print(
        f"W2.7 corpus: {real_count} real / {fixture_count} fixture "
        f"({len(VLM_ENTRIES)} VLM + {len(PATCH_ENTRIES)} patch); "
        f"{len(INTEGRATION_SCENARIOS)} integration rows (all MOCK)."
    )


# ---------------------------------------------------------------------------
# VLM parsing runners
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry", VLM_ENTRIES, ids=[e["scenario"] for e in VLM_ENTRIES]
)
def test_vlm_parsing_fixture(entry):
    raw = (VLM_DIR / entry["file"]).read_text(encoding="utf-8")
    result = parse_vlm_output(raw)

    assert result.status == entry["expected_status"], (
        f"{entry['file']}: status={result.status} reason={result.reason} "
        f"message={result.message}"
    )

    if entry["expected_status"] == "ok":
        assert result.response is not None
        assert result.response.outcome.value == entry["expected_outcome"]
        assert len(result.response.findings) == entry["expected_findings"]
    else:
        assert result.reason is not None
        assert result.reason.value == entry["expected_reason"]


# ---------------------------------------------------------------------------
# Patch extraction runners
# ---------------------------------------------------------------------------


def _patch_settings(entry: dict, root: Path) -> Settings:
    return Settings(
        enable_playwright_worker=False,
        enable_vlm_detection=False,
        enable_patch_engine=False,
        enable_github_delivery=False,
        patch_source_root=str(root),
        approved_patch_paths=list(entry["approved_patch_paths"]),
    )


@pytest.mark.parametrize(
    "entry", PATCH_ENTRIES, ids=[e["scenario"] for e in PATCH_ENTRIES]
)
def test_patch_extraction_fixture(entry, tmp_path):
    root = _make_tree(tmp_path / "repo", entry["source_root"])
    settings = _patch_settings(entry, root)
    finding = _finding()

    raw = (PATCH_DIR / entry["response_file"]).read_text(encoding="utf-8")
    raw = raw.replace("__FINDING_ID__", finding.finding_id)

    result = SourcePatchExtractor(settings).extract(
        raw,
        finding=finding,
        source_context=build_source_context(finding, settings),
    )

    assert result.status.value == entry["expected_status"], (
        f"{entry['scenario']}: status={result.status.value} "
        f"reasons={result.reasons} message={result.message}"
    )
    if entry["expected_status"] == "accepted":
        assert result.proposal is not None
        assert result.reasons == []
    else:
        assert entry["expected_reason"] in result.reasons


# ---------------------------------------------------------------------------
# Integration failure rows (through the real detection API)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,expected_reason",
    INTEGRATION_ROWS,
    ids=[label for label, _ in INTEGRATION_ROWS],
)
def test_detection_failure_scenario(
    detect_client, evidence_store, scenario, expected_reason
):
    analyzer = _integration_analyzers()[scenario](evidence_store)
    client = detect_client(analyzer)
    scan_id = _evidence_ready(client)

    resp = _detect(client, scan_id)
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] is True

    assert _scan_status(client, scan_id) == "detection_failed"

    run = _findings(client, scan_id)["detection"]
    assert run["status"] == "detection_failed"
    assert run["reason_code"] == expected_reason
    assert run["findings_count"] == 0
