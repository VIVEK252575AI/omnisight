from app.demo_run_store import DemoRunStore
from app.local_state import LocalStateDB
from app.patch_schemas import (
    PatchEdit,
    PatchFile,
    PatchFileSummary,
    PatchProposal,
    PatchProposalRecord,
    PatchProposalStatus,
)
from app.patch_storage import PatchStore


def test_demo_runs_survive_store_restart(tmp_path):
    database = LocalStateDB(tmp_path / "local-state.sqlite3")
    first_store = DemoRunStore(database)
    run = first_store.create()
    assert run is not None
    first_store.update(
        run.run_id,
        status="awaiting_sandbox_approval",
        result={"proposal": {"proposal_id": "proposal-persisted"}},
    )

    restarted_store = DemoRunStore(database)
    restored = restarted_store.get(run.run_id)
    assert restored is not None
    assert restored.status == "awaiting_sandbox_approval"
    assert restored.result["proposal"]["proposal_id"] == "proposal-persisted"
    assert restarted_store.list()[0].run_id == run.run_id
    assert restarted_store.create() is None


def test_validated_patch_payload_survives_store_restart_and_stays_separate(tmp_path):
    database = LocalStateDB(tmp_path / "local-state.sqlite3")
    proposal = PatchProposal(
        finding_id="finding-1",
        files=[PatchFile(path="styles/checkout.css", edits=[
            PatchEdit(old_text="width: 520px;", new_text="width: 100%; max-width: 100%;")
        ])],
        rationale="Keep the action inside the mobile viewport.",
        generator_label="MOCK",
    )
    record = PatchProposalRecord(
        proposal_id="proposal-persisted",
        finding_id="finding-1",
        status=PatchProposalStatus.proposed,
        files=[PatchFileSummary(path="styles/checkout.css", edits=1, bytes=42)],
        generator_label="MOCK",
        is_mock=True,
        proposal=proposal,
    )
    PatchStore(database).add(record, proposal)

    restarted_store = PatchStore(database)
    restored_record = restarted_store.get(record.proposal_id)
    restored_proposal = restarted_store.get_proposal(record.proposal_id)
    assert restored_record is not None
    assert restored_record.proposal is None
    assert restarted_store.list_for_finding("finding-1")[0].proposal is None
    assert restarted_store.count_for_finding("finding-1") == 1
    assert restored_proposal == proposal
    assert restored_proposal.files[0].edits[0].old_text == "width: 520px;"
