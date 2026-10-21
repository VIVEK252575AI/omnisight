"""Persistence behavior tests using a disposable SQLite test database.

Production configuration is PostgreSQL-only; SQLite here is an isolated test
fixture and is never selected by application settings.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db_models import Base
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
