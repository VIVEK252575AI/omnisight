"""Persistence behavior tests using a disposable SQLite test database.

Production configuration is PostgreSQL-only; SQLite here is an isolated test
fixture and is never selected by application settings.
"""

from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.contracts import Finding as VlmFinding
from app.ai.contracts import FindingCategory, FindingStatus, Severity, VlmOutcome
from app.config import Settings
from app.db_models import Artifact, Base, DetectionAttempt, Evidence, FindingRecordRow, QARun, WorkflowEvent
from app.detection_schemas import DetectionRun, FindingRecord
from app.detection_storage import DetectionStore
from app.demo_run_store import DemoRunStore
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchFileSummary,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
)
from app.patch_storage import PatchStore
from app.evidence_storage import EvidenceStore
from app.schemas import EvidenceRecord, Observation, ScanCreateRequest, ScanStatus, Viewport
from app.storage import ScanStore


def _database():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _proposal() -> PatchProposal:
    return PatchProposal(
        finding_id="finding-1",
        files=[PatchFile(path="styles/checkout.css", edits=[
            PatchEdit(old_text="width: 520px;", new_text="width: 100%; max-width: 100%;")
        ])],
        rationale="Keep the action inside the mobile viewport.",
        generator_label="MOCK",
    )


def test_legacy_scan_api_records_are_durable_qa_runs():
    database = _database()
    settings = Settings()
    request = ScanCreateRequest(
        target_url="https://shop.example/checkout",
        viewports=[Viewport(width=390, height=844)],
        routes=["/checkout"],
    )
    first = ScanStore(database, settings=settings)
    scan = first.create(request)

    restarted = ScanStore(database, settings=settings)
    restored = restarted.get(scan.scan_id)
    assert restored is not None
    assert restored.target_url == request.target_url
    assert restored.routes == ["/checkout"]
    assert restored.status is ScanStatus.created
    assert restarted.list() == [restored]
    with database() as session:
        run = session.get(QARun, str(scan.scan_id))
        assert run is not None
        assert run.current_stage == "preflight"
        assert run.workflow_events[-1].payload["legacy_scan_status"] == "created"

    advanced = restarted.compare_and_transition(
        scan.scan_id,
        expected=frozenset({ScanStatus.created}),
        to_status=ScanStatus.evidence_collection,
    )
    assert advanced is not None
    assert advanced.status is ScanStatus.evidence_collection
    with database() as session:
        run = session.get(QARun, str(scan.scan_id))
        assert run.current_stage == "evidence_persistence"
        assert run.workflow_events[-1].payload["legacy_scan_status"] == "evidence_collection"
        assert session.query(WorkflowEvent).filter(WorkflowEvent.run_id == str(scan.scan_id)).count() == 2
    assert restarted.compare_and_transition(
        scan.scan_id,
        expected=frozenset({ScanStatus.created}),
        to_status=ScanStatus.evidence_collection,
    ) is None


def test_evidence_metadata_and_artifact_reference_survive_restart(tmp_path):
    database = _database()
    settings = Settings()
    scan = ScanStore(database, settings=settings).create(ScanCreateRequest(
        target_url="https://shop.example/checkout",
        viewports=[Viewport(width=390, height=844)],
        routes=["/checkout"],
    ))
    observation_path = Path(__file__).parent / "fixtures" / "observation_fixture.json"
    observation = Observation.model_validate_json(observation_path.read_bytes())
    screenshot = tmp_path / "checkout.png"
    screenshot.write_bytes(b"demo screenshot bytes, stored on disk only")
    artifact_id = uuid4()
    record = EvidenceRecord(
        evidence_id=uuid4(), scan_id=scan.scan_id, observation=observation,
        screenshot_artifact_id=artifact_id,
    )

    EvidenceStore(database, settings=settings, artifacts_root=tmp_path).add(
        record, artifact_id=artifact_id, artifact_path=screenshot,
    )
    restarted = EvidenceStore(database, settings=settings, artifacts_root=tmp_path)
    assert restarted.list_for_scan(scan.scan_id) == [record]
    assert restarted.get_artifact(artifact_id).read_bytes() == screenshot.read_bytes()
    with database() as session:
        artifact = session.get(Artifact, str(artifact_id))
        stored_evidence = session.get(Evidence, str(record.evidence_id))
        assert artifact is not None
        assert artifact.object_key == "checkout.png"
        assert artifact.byte_size == screenshot.stat().st_size
        assert len(artifact.sha256) == 64
        assert stored_evidence is not None
        assert "bytes" not in str(stored_evidence.observation_json)
        assert "demo screenshot" not in str(stored_evidence.observation_json)

    screenshot.write_bytes(b"tampered")
    assert restarted.get_artifact(artifact_id) is None


def test_detection_attempts_and_current_findings_survive_restart_without_raw_text():
    database = _database()
    settings = Settings()
    scan = ScanStore(database, settings=settings).create(ScanCreateRequest(
        target_url="https://analysis.example/checkout",
        viewports=[Viewport(width=390, height=844)],
    ))
    finding = VlmFinding(
        category=FindingCategory.clipped_element,
        target_candidate="#checkout",
        observation="Checkout action extends past the viewport.",
        evidence="right edge is outside the 390px viewport",
        severity=Severity.high,
        confidence=0.92,
    )
    record = FindingRecord(
        finding=finding,
        scan_id=scan.scan_id,
        adapter_label="MOCK",
        is_mock=True,
        route="/checkout",
        viewport=Viewport(width=390, height=844),
    )
    attempt = DetectionRun(
        scan_id=scan.scan_id,
        status=ScanStatus.detected,
        adapter_label="MOCK",
        is_mock=True,
        prompt_version="visual-analysis-v2",
        outcome=VlmOutcome.defects_found,
        findings_count=1,
    )
    first = DetectionStore(database, settings=settings)
    first.put_run(attempt)
    first.set_raw(scan.scan_id, "private raw provider response")
    first.replace_findings(scan.scan_id, [record])

    restarted = DetectionStore(database, settings=settings)
    restored_attempt = restarted.get_run(scan.scan_id)
    restored_finding = restarted.get_finding(finding.finding_id)
    assert restored_attempt is not None and restored_attempt.findings_count == 1
    assert restored_attempt.raw_output is None
    assert restored_finding is not None and restored_finding.finding == finding
    assert restarted.list_for_scan(scan.scan_id) == [restored_finding]
    assert restarted.compare_and_set_finding_status(
        finding.finding_id,
        expected=FindingStatus.detected,
        to_status=FindingStatus.patch_proposed,
    ).finding.status is FindingStatus.patch_proposed

    replacement = record.model_copy(update={"finding": finding.model_copy(update={"finding_id": "finding-new"})})
    restarted.put_run(attempt.model_copy(update={"findings_count": 1}))
    restarted.replace_findings(scan.scan_id, [replacement])
    assert restarted.get_finding(finding.finding_id) is None
    assert restarted.list_for_scan(scan.scan_id)[0].finding.finding_id == "finding-new"
    with database() as session:
        assert session.query(DetectionAttempt).filter(DetectionAttempt.run_id == str(scan.scan_id)).count() == 2
        rows = session.query(FindingRecordRow).filter(FindingRecordRow.run_id == str(scan.scan_id)).all()
        assert len(rows) == 2 and sum(item.is_current for item in rows) == 1
        assert all("private raw provider response" not in str(item.record_json) for item in rows)


def test_demo_runs_survive_store_restart_in_domain_tables():
    database = _database()
    settings = Settings()
    first = DemoRunStore(database, settings=settings)
    run = first.create()
    assert run is not None
    scan = ScanStore(database, settings=settings).create(ScanCreateRequest(
        target_url="http://127.0.0.1:4173/checkout",
        viewports=[Viewport(width=390, height=844)],
    ))
    finding = VlmFinding(
        finding_id="finding-1",
        category=FindingCategory.clipped_element,
        target_candidate="#checkout",
        observation="The checkout action extends past the viewport.",
        evidence="The right edge exceeds the viewport.",
        severity=Severity.high,
        confidence=0.9,
    )
    detection = DetectionStore(database, settings=settings)
    detection.put_run(DetectionRun(
        scan_id=scan.scan_id, status=ScanStatus.detected, adapter_label="MOCK",
        is_mock=True, prompt_version="visual-analysis-v2", findings_count=1,
    ))
    detection.replace_findings(scan.scan_id, [FindingRecord(
        finding=finding, scan_id=scan.scan_id, adapter_label="MOCK", is_mock=True,
    )])
    proposal = _proposal()
    PatchStore(database, settings=settings).add(PatchProposalRecord(
        proposal_id="proposal-persisted",
        finding_id="finding-1",
        scan_id=scan.scan_id,
        status=PatchProposalStatus.proposed,
        generator_label="MOCK",
        proposal=proposal,
    ), proposal)
    first.update(
        run.run_id,
        status="awaiting_sandbox_approval",
        result={
            "scan_id": str(scan.scan_id),
            "findings": [{"finding_id": "finding-1"}],
            "proposal": {"proposal_id": "proposal-persisted"},
        },
    )

    restarted = DemoRunStore(database, settings=settings)
    restored = restarted.get(run.run_id)
    assert restored is not None
    assert restored.status == "awaiting_sandbox_approval"
    assert restored.result["proposal"]["proposal_id"] == "proposal-persisted"
    assert [item.run_id for item in restarted.list()] == [run.run_id]
    assert restarted.create() is None
    first.update(
        run.run_id,
        status="patch_rejected",
        result={
            "scan_id": str(scan.scan_id),
            "findings": [{"finding_id": "finding-1"}],
            "proposal_attempts": [{"proposal_id": "proposal-persisted", "status": "rejected"}],
        },
    )
    rejected = restarted.get(run.run_id)
    assert rejected is not None and rejected.status == "patch_rejected"
    first.update(run.run_id, status="awaiting_sandbox_approval", result={
        "scan_id": str(scan.scan_id),
        "findings": [{"finding_id": "finding-1"}],
        "proposal": {"proposal_id": "proposal-persisted"},
    })
    assert restarted.get(run.run_id).status == "awaiting_sandbox_approval"
    first.update(run.run_id, status="validating")
    assert restarted.get(run.run_id).status == "validating"
    first.update(run.run_id, status="verified", result={
        "scan_id": str(scan.scan_id),
        "findings": [{"finding_id": "finding-1"}],
        "proposal": {"proposal_id": "proposal-persisted"},
        "sandbox": {"status": "verified"},
    })
    verified = restarted.get(run.run_id)
    assert verified is not None and verified.status == "verified"
    assert verified.result["sandbox"]["status"] == "verified"


def test_validated_patch_is_durable_but_not_in_safe_record():
    database = _database()
    settings = Settings()
    proposal = _proposal()
    record = PatchProposalRecord(
        proposal_id="proposal-persisted",
        finding_id="finding-1",
        status=PatchProposalStatus.proposed,
        files=[PatchFileSummary(path="styles/checkout.css", edits=1, bytes=42)],
        generator_label="MOCK",
        is_mock=True,
        proposal=proposal,
    )
    PatchStore(database, settings=settings).add(record, proposal)

    restarted = PatchStore(database, settings=settings)
    restored_record = restarted.get(record.proposal_id)
    restored_proposal = restarted.get_proposal(record.proposal_id)
    assert restored_record is not None
    assert restored_record.proposal is None
    assert restarted.list_for_finding("finding-1")[0].proposal is None
    assert restarted.count_for_finding("finding-1") == 1
    assert restored_proposal == proposal
    assert restored_proposal.files[0].edits[0].old_text == "width: 520px;"
