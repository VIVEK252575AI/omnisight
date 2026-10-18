"""Tests for the VLM detection workflow (Week 2, W2.4).

Everything runs offline against the deterministic MOCK adapter (or a recording
subclass of it) — no network, no model SDK, no paid API. Background tasks
execute synchronously inside ``TestClient``, so a 202 response is followed by a
completed run by the time the call returns.

Fixtures are labelled FIXTURE: SHOPLITE-LIKE — synthetic evidence standing in
for the (not yet delivered) real Playwright collector.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.ai.adapters import MockVisionAnalyzer
from app.ai.contracts import VisionRequest
from app.config import Settings
from app.detection_service import DetectionService
from app.schemas import ScanCreateRequest, ScanStatus
from app.storage import ScanStore

SCANS = "/api/scans"
API_V1 = "/api/v1"
FIXTURES = Path(__file__).parent / "fixtures"

DETECTS_OK = {"detected", "detection_failed", "needs_inspection"}


class RecordingMock(MockVisionAnalyzer):
    """MOCK adapter that also records every :class:`VisionRequest` it was sent."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[VisionRequest] = []

    def analyze(self, request: VisionRequest, *, repair: bool = False):
        self.requests.append(request)
        return super().analyze(request, repair=repair)


class _BoomAnalyzer:
    """Not a VisionAnalyzer: raises an *unexpected* error to exercise crash safety."""

    provider_label = "BOOM"
    is_mock = True
    model_label = None

    def analyze(self, request, *, repair: bool = False):
        raise RuntimeError("kaboom")


def _settings(**overrides) -> Settings:
    return Settings(artifacts_dir=FIXTURES, **overrides)


def _create_scan(client) -> str:
    resp = client.post(SCANS, json={"target_url": "https://shoplite.local/checkout"})
    assert resp.status_code == 201, resp.text
    return resp.json()["scan_id"]


def _evidence_ready(client) -> str:
    """Create a scan and submit the fixture observation (-> evidence_collection)."""
    scan_id = _create_scan(client)
    raw = (FIXTURES / "observation_fixture.json").read_bytes()
    resp = client.post(
        f"{API_V1}/scans/{scan_id}/evidence",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 201, resp.text
    return scan_id


def _detect(client, scan_id: str):
    return client.post(f"{API_V1}/scans/{scan_id}/detect")


def _scan_status(client, scan_id: str) -> str:
    return client.get(f"{SCANS}/{scan_id}").json()["status"]


def _findings(client, scan_id: str) -> dict:
    resp = client.get(f"{API_V1}/scans/{scan_id}/findings")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


class TestDetectionSuccess:
    def test_detect_returns_202_and_completes(self, detect_client):
        client = detect_client()
        scan_id = _evidence_ready(client)

        resp = _detect(client, scan_id)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["accepted"] is True
        assert body["scan_id"] == scan_id
        assert body["detection"] is not None
        assert body["detection"]["adapter_label"] == "MOCK"
        assert body["detection"]["is_mock"] is True

        assert _scan_status(client, scan_id) == "detected"

    def test_validated_findings_are_persisted_and_labelled(self, detect_client):
        client = detect_client()
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        body = _findings(client, scan_id)
        assert body["scan_id"] == scan_id
        assert body["status"] == "detected"
        assert body["adapter_label"] == "MOCK"

        run = body["detection"]
        assert run["status"] == "detected"
        assert run["outcome"] == "defects_found"
        assert run["findings_count"] == 1
        assert run["is_mock"] is True
        assert run["adapter_label"] == "MOCK"
        assert run["prompt_version"].startswith("visual-analysis-")
        assert run["schema_version"] == 1
        assert run["attempts"] >= 1
        assert run["duration_ms"] >= 0.0
        assert run["started_at"] is not None
        assert run["finished_at"] is not None
        assert run["reason_code"] is None
        assert run["raw_output"] is None  # never exposed by default

        assert len(body["findings"]) == 1
        record = body["findings"][0]
        assert record["adapter_label"] == "MOCK"
        assert record["is_mock"] is True
        assert record["route"] == "/checkout"
        assert record["viewport"] == {"width": 390, "height": 844}
        assert len(record["evidence_ids"]) == 1
        assert len(record["screenshot_artifact_ids"]) == 1

        finding = record["finding"]
        assert finding["category"] == "clipped_element"
        assert finding["severity"] == "high"
        assert finding["status"] == "detected"
        assert finding["target_candidate"] == "#submit"
        # `#submit` is not a captured DOM candidate, so it is flagged unverified.
        assert finding["target_unverified"] is True
        assert finding["finding_id"]
        assert finding["observation"]
        assert finding["evidence"]

    def test_healthy_page_yields_zero_findings(self, detect_client):
        client = detect_client(scenario="healthy_page")
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        assert _scan_status(client, scan_id) == "detected"
        body = _findings(client, scan_id)
        assert body["detection"]["outcome"] == "no_defects"
        assert body["detection"]["findings_count"] == 0
        assert body["detection"]["reason_code"] is None
        assert body["findings"] == []

    def test_undecided_outcome_enters_needs_inspection(self, detect_client):
        client = detect_client(scenario="needs_inspection")
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        assert _scan_status(client, scan_id) == "needs_inspection"
        body = _findings(client, scan_id)
        assert body["detection"]["outcome"] == "needs_inspection"
        assert body["detection"]["status"] == "needs_inspection"
        assert body["detection"]["reason_code"] is None

    def test_dom_candidates_are_bounded_by_settings(self, detect_client, evidence_store):
        analyzer = RecordingMock(
            "clipped_button", resolver=evidence_store.get_artifact
        )
        settings = _settings(detection_max_candidates=1)
        client = detect_client(analyzer, settings=settings)
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        assert len(analyzer.requests) == 1
        request = analyzer.requests[0]
        # 1 viewport measurement + (box, in_viewport) per retained candidate.
        assert len(request.measurements) <= 1 + 2 * settings.detection_max_candidates
        assert len(request.measurements) == 3  # fixture has 2 elements, capped to 1
        assert request.prompt_version.startswith("visual-analysis-")
        assert request.schema_version == 1
        assert request.allowed_categories
        assert request.measurements[0].name == "viewport"
        assert request.screenshots and request.screenshots[0].artifact_id

    def test_screenshot_is_resolved_to_a_real_file(self, detect_client, evidence_store):
        analyzer = RecordingMock("clipped_button", resolver=evidence_store.get_artifact)
        client = detect_client(analyzer)
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        from app.ai.adapters.base import resolve_image_paths

        request = analyzer.requests[0]
        paths = resolve_image_paths(request, evidence_store.get_artifact)
        assert paths and all(p.is_file() for p in paths)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestDetectionFailures:
    @pytest.mark.parametrize(
        "scenario,expected_reason",
        [
            ("malformed_text", "not_json"),
            ("malformed_schema", "schema_invalid"),
            ("empty", "empty"),
            ("timeout", "timeout"),
            ("provider_error", "provider_unavailable"),
        ],
    )
    def test_failure_records_typed_reason(
        self, detect_client, scenario, expected_reason
    ):
        client = detect_client(scenario=scenario)
        scan_id = _evidence_ready(client)
        resp = _detect(client, scan_id)
        assert resp.status_code == 202, resp.text

        assert _scan_status(client, scan_id) == "detection_failed"
        scan = client.get(f"{SCANS}/{scan_id}").json()
        assert scan["error"]

        run = _findings(client, scan_id)["detection"]
        assert run["status"] == "detection_failed"
        assert run["reason_code"] == expected_reason
        assert run["reason_message"]
        assert run["findings_count"] == 0
        assert run["is_mock"] is True
        assert run["finished_at"] is not None

    def test_parse_rejection_is_bounded_to_repair_attempts(self, detect_client, evidence_store):
        analyzer = RecordingMock("malformed_schema", resolver=evidence_store.get_artifact)
        client = detect_client(analyzer)
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        run = _findings(client, scan_id)["detection"]
        assert run["reason_code"] == "schema_invalid"
        # one initial ask + the single bounded repair re-ask
        assert run["attempts"] == 1 + _settings().ai_max_repair_attempts
        assert len(analyzer.calls) == run["attempts"]

    def test_transport_failure_is_not_repaired(self, detect_client, evidence_store):
        analyzer = RecordingMock("timeout", resolver=evidence_store.get_artifact)
        client = detect_client(analyzer)
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        run = _findings(client, scan_id)["detection"]
        assert run["reason_code"] == "timeout"
        assert run["attempts"] == 1
        assert len(analyzer.calls) == 1

    def test_no_evidence_records_reason(self, detect_client):
        client = detect_client()
        scan_id = _create_scan(client)
        assert (
            client.post(f"{SCANS}/{scan_id}/transition?to_status=evidence_collection").status_code
            == 200
        )

        assert _detect(client, scan_id).status_code == 202

        assert _scan_status(client, scan_id) == "detection_failed"
        run = _findings(client, scan_id)["detection"]
        assert run["reason_code"] == "no_evidence"
        assert run["findings_count"] == 0

    def test_failed_rerun_is_allowed_from_detection_failed(self, detect_client, evidence_store):
        analyzer = RecordingMock(
            ["timeout", "healthy_page"], resolver=evidence_store.get_artifact
        )
        client = detect_client(analyzer)
        scan_id = _evidence_ready(client)

        _detect(client, scan_id)
        assert _scan_status(client, scan_id) == "detection_failed"

        second = _detect(client, scan_id)
        assert second.status_code == 202, second.text
        assert second.json()["accepted"] is True

        assert _scan_status(client, scan_id) == "detected"
        assert len(analyzer.calls) == 2


# ---------------------------------------------------------------------------
# Endpoint contracts
# ---------------------------------------------------------------------------


class TestDetectionEndpoints:
    def test_unknown_scan_404(self, detect_client):
        client = detect_client()
        resp = _detect(client, "00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SCAN_NOT_FOUND"

    def test_wrong_state_409(self, detect_client):
        client = detect_client()
        scan_id = _create_scan(client)  # status: created
        resp = _detect(client, scan_id)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "DETECTION_STATE_CONFLICT"
        assert _scan_status(client, scan_id) == "created"

    def test_terminal_state_409(self, detect_client):
        client = detect_client()
        scan_id = _create_scan(client)
        client.post(f"{SCANS}/{scan_id}/transition?to_status=failed")
        resp = _detect(client, scan_id)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "DETECTION_STATE_CONFLICT"
        assert _scan_status(client, scan_id) == "failed"

    def test_second_trigger_is_idempotent(self, detect_client, evidence_store):
        analyzer = RecordingMock("clipped_button", resolver=evidence_store.get_artifact)
        client = detect_client(analyzer)
        scan_id = _evidence_ready(client)

        first = _detect(client, scan_id)
        assert first.status_code == 202
        assert first.json()["accepted"] is True
        run_before = _findings(client, scan_id)["detection"]

        second = _detect(client, scan_id)
        assert second.status_code == 202
        assert second.json()["accepted"] is False

        run_after = _findings(client, scan_id)["detection"]
        assert run_after["finished_at"] == run_before["finished_at"]
        assert len(analyzer.calls) == 1
        assert len(_findings(client, scan_id)["findings"]) == 1

    def test_trigger_while_detecting_starts_no_second_run(
        self, detect_client, evidence_store
    ):
        analyzer = RecordingMock("clipped_button", resolver=evidence_store.get_artifact)
        client = detect_client(analyzer)
        scan_id = _evidence_ready(client)
        # Put the scan into `detecting` without running a detection (as a
        # concurrent trigger would have), then post a second trigger.
        assert (
            client.post(f"{SCANS}/{scan_id}/transition?to_status=detecting").status_code
            == 200
        )

        resp = _detect(client, scan_id)
        assert resp.status_code == 202
        body = resp.json()
        assert body["accepted"] is False
        assert body["detection"] is None  # no run was started
        assert analyzer.calls == []
        assert _scan_status(client, scan_id) == "detecting"

    def test_findings_list_unknown_scan_404(self, detect_client):
        client = detect_client()
        resp = client.get(f"{API_V1}/scans/00000000-0000-0000-0000-000000000000/findings")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SCAN_NOT_FOUND"

    def test_get_finding_by_id(self, detect_client):
        client = detect_client()
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        finding_id = _findings(client, scan_id)["findings"][0]["finding"]["finding_id"]
        resp = client.get(f"{API_V1}/findings/{finding_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["finding"]["finding_id"] == finding_id
        assert body["scan_id"] == scan_id
        assert body["adapter_label"] == "MOCK"
        assert body["is_mock"] is True
        assert body["evidence_ids"]
        assert body["screenshot_artifact_ids"]
        assert body["raw_output"] is None

    def test_get_finding_unknown_404(self, detect_client):
        client = detect_client()
        resp = client.get(f"{API_V1}/findings/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "FINDING_NOT_FOUND"

    def test_findings_empty_before_any_detection(self, detect_client):
        client = detect_client()
        scan_id = _evidence_ready(client)
        body = _findings(client, scan_id)
        assert body["status"] == "evidence_collection"
        assert body["detection"] is None
        assert body["findings"] == []
        assert body["adapter_label"] is None


# ---------------------------------------------------------------------------
# Raw model output (untrusted, size-capped, debug-gated)
# ---------------------------------------------------------------------------


class TestRawOutputExposure:
    def test_raw_hidden_even_when_query_asks_for_it(self, detect_client):
        client = detect_client(scenario="malformed_text")
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        body = _findings(client, scan_id)  # default: include_raw=false
        assert body["detection"]["raw_output"] is None

        resp = client.get(f"{API_V1}/scans/{scan_id}/findings?include_raw=true")
        assert resp.json()["detection"]["raw_output"] is None

    def test_raw_exposed_only_with_flag_and_query(self, detect_client):
        client = detect_client(
            scenario="malformed_text",
            settings=_settings(detection_expose_raw_output=True),
        )
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        default = _findings(client, scan_id)
        assert default["detection"]["raw_output"] is None

        resp = client.get(f"{API_V1}/scans/{scan_id}/findings?include_raw=true")
        raw = resp.json()["detection"]["raw_output"]
        assert raw and "not JSON" in raw

    def test_raw_is_size_capped(self, detect_client):
        client = detect_client(
            scenario="malformed_text",
            settings=_settings(
                detection_expose_raw_output=True, detection_max_raw_output_bytes=8
            ),
        )
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        resp = client.get(f"{API_V1}/scans/{scan_id}/findings?include_raw=true")
        raw = resp.json()["detection"]["raw_output"]
        assert raw is not None
        assert len(raw.encode("utf-8")) <= 8

    def test_single_finding_raw_respects_debug_flag(self, detect_client):
        client = detect_client(
            scenario="clipped_button",
            settings=_settings(detection_expose_raw_output=True),
        )
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)
        finding_id = _findings(client, scan_id)["findings"][0]["finding"]["finding_id"]

        default = client.get(f"{API_V1}/findings/{finding_id}").json()
        assert default["raw_output"] is None

        flagged = client.get(f"{API_V1}/findings/{finding_id}?include_raw=true").json()
        assert flagged["raw_output"]
        assert "defects_found" in flagged["raw_output"]
        assert flagged["finding"]["finding_id"] == finding_id

    def test_raw_is_never_attached_to_finding_records(self, detect_client):
        client = detect_client(
            scenario="clipped_button",
            settings=_settings(detection_expose_raw_output=True),
        )
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)

        default = _findings(client, scan_id)
        assert all(f["raw_output"] is None for f in default["findings"])

        flagged = client.get(f"{API_V1}/scans/{scan_id}/findings?include_raw=true").json()
        assert all(f["raw_output"] is None for f in flagged["findings"])
        # Raw text is carried by the run record only, and only when asked.
        assert flagged["detection"]["raw_output"]


# ---------------------------------------------------------------------------
# State-machine guarantees
# ---------------------------------------------------------------------------


class TestStateGuarantees:
    @pytest.mark.parametrize(
        "scenario",
        [
            "clipped_button",
            "healthy_page",
            "needs_inspection",
            "malformed_text",
            "malformed_schema",
            "empty",
            "timeout",
            "provider_error",
        ],
    )
    def test_no_scenario_leaves_the_scan_detecting(self, detect_client, scenario):
        client = detect_client(scenario=scenario)
        scan_id = _evidence_ready(client)
        assert _detect(client, scan_id).status_code == 202
        status = _scan_status(client, scan_id)
        assert status in DETECTS_OK
        assert status != "detecting"

    def test_crashed_run_settles_into_detection_failed(
        self, scan_store, evidence_store, detection_store, detect_client
    ):
        client = detect_client()
        scan_id = _evidence_ready(client)
        assert client.post(f"{SCANS}/{scan_id}/transition?to_status=detecting").status_code == 200

        service = DetectionService(
            _settings(),
            scan_store=scan_store,
            evidence_store=evidence_store,
            detection_store=detection_store,
            analyzer=_BoomAnalyzer(),
        )
        service.run_guarded(UUID(scan_id))

        scan = scan_store.get(UUID(scan_id))
        assert scan.status is ScanStatus.detection_failed
        assert scan.error == "detection crashed: RuntimeError"

        run = detection_store.get_run(UUID(scan_id))
        assert run is not None
        assert run.status is ScanStatus.detection_failed
        assert run.reason_code == "interrupted"
        assert run.finished_at is not None

    def test_run_is_skipped_when_scan_left_detecting(
        self, detect_client, scan_store, evidence_store, detection_store
    ):
        analyzer = RecordingMock("clipped_button", resolver=evidence_store.get_artifact)
        client = detect_client(analyzer)
        scan_id = _evidence_ready(client)
        _detect(client, scan_id)
        calls_before = len(analyzer.calls)

        service = DetectionService(
            _settings(),
            scan_store=scan_store,
            evidence_store=evidence_store,
            detection_store=detection_store,
            analyzer=analyzer,
        )
        service.run_guarded(UUID(scan_id))

        assert len(analyzer.calls) == calls_before
        assert scan_store.get(UUID(scan_id)).status is ScanStatus.detected


class TestAtomicTransition:
    """The single-writer guard that prevents two concurrent detections."""

    def test_wrong_expected_status_is_a_no_op(self, scan_store: ScanStore):
        record = scan_store.create(
            ScanCreateRequest(target_url="https://shoplite.local/")
        )
        assert record.status is ScanStatus.created

        assert (
            scan_store.compare_and_transition(
                record.scan_id,
                expected=frozenset({ScanStatus.evidence_collection}),
                to_status=ScanStatus.detecting,
            )
            is None
        )
        # The record was not modified by the rejected attempt.
        assert scan_store.get(record.scan_id).status is ScanStatus.created

    def test_expected_status_transitions_once(self, scan_store: ScanStore):
        record = scan_store.create(
            ScanCreateRequest(target_url="https://shoplite.local/")
        )
        first = scan_store.compare_and_transition(
            record.scan_id,
            expected=frozenset({ScanStatus.created}),
            to_status=ScanStatus.evidence_collection,
        )
        assert first is not None and first.status is ScanStatus.evidence_collection

        # A second writer expecting the *old* status loses the race.
        assert (
            scan_store.compare_and_transition(
                record.scan_id,
                expected=frozenset({ScanStatus.created}),
                to_status=ScanStatus.detecting,
            )
            is None
        )
        assert scan_store.get(record.scan_id).status is ScanStatus.evidence_collection

    def test_unknown_scan_returns_none(self, scan_store: ScanStore):
        assert (
            scan_store.compare_and_transition(
                uuid4(),
                expected=frozenset({ScanStatus.created}),
                to_status=ScanStatus.evidence_collection,
            )
            is None
        )
