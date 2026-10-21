"""Persistence behavior tests using a disposable SQLite test database.

Production configuration is PostgreSQL-only; SQLite here is an isolated test
fixture and is never selected by application settings.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.contracts import Finding as VlmFinding
from app.ai.contracts import FindingCategory, FindingStatus, Severity, VlmOutcome
from app.config import Settings
from app.db_models import Base, DetectionAttempt, FindingRecordRow, QARun, WorkflowEvent
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
from app.schemas import ScanCreateRequest, ScanStatus, Viewport
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
    proposal = _proposal()
    PatchStore(database, settings=settings).add(PatchProposalRecord(
        proposal_id="proposal-persisted",
        finding_id="finding-1",
        status=PatchProposalStatus.proposed,
        generator_label="MOCK",
        proposal=proposal,
    ), proposal)
    first.update(
        run.run_id,
        status="awaiting_sandbox_approval",
        result={"proposal": {"proposal_id": "proposal-persisted"}},
    )

    restarted = DemoRunStore(database, settings=settings)
    restored = restarted.get(run.run_id)
    assert restored is not None
    assert restored.status == "awaiting_sandbox_approval"
    assert restored.result["proposal"]["proposal_id"] == "proposal-persisted"
    assert restarted.list()[0].run_id == run.run_id
    assert restarted.create() is None


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
