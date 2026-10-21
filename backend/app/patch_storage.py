"""Storage for patch proposals (Week 2, W2.5).

The injectable class supports isolated in-memory tests. The app singleton uses
the local SQLite database so proposals survive a backend restart.

Two things are kept per proposal, deliberately separated:

- the **record** — status, reasons, file summaries, provenance (safe to return);
- the **validated proposal** — the exact ``old_text``/``new_text`` edits, used by
  the later publish step and never returned by the API. It is stored locally
  in a separate SQLite column; raw model output is never stored.
"""

from __future__ import annotations

import threading

from app.local_state import LocalStateDB, get_local_state_db
from app.patch_schemas import PatchProposal, PatchProposalRecord


class PatchStore:
    """Thread-safe store of patch-proposal records."""

    def __init__(self, database: LocalStateDB | None = None) -> None:
        self._database = database
        self._records: dict[str, PatchProposalRecord] = {}
        self._by_finding: dict[str, list[str]] = {}
        self._edits: dict[str, PatchProposal] = {}
        self._lock = threading.Lock()

    def add(
        self, record: PatchProposalRecord, proposal: PatchProposal | None = None
    ) -> PatchProposalRecord:
        """Store one proposal attempt (append-only: attempts are never replaced)."""
        with self._lock:
            if self._database is not None:
                with self._database.connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO patch_proposals "
                        "(proposal_id,finding_id,record_json,proposal_json,created_at) VALUES(?,?,?,?,?)",
                        (
                            record.proposal_id,
                            record.finding_id,
                            record.model_dump_json(exclude={"proposal"}),
                            proposal.model_dump_json() if proposal is not None else None,
                            record.created_at.isoformat(),
                        ),
                    )
            self._records[record.proposal_id] = record
            self._by_finding.setdefault(record.finding_id, []).append(record.proposal_id)
            if proposal is not None:
                self._edits[record.proposal_id] = proposal
        return record

    def list_for_finding(self, finding_id: str) -> list[PatchProposalRecord]:
        """Return every attempt for a finding, oldest first."""
        if self._database is not None:
            with self._database.connect() as connection:
                rows = connection.execute(
                    "SELECT record_json FROM patch_proposals WHERE finding_id=? ORDER BY created_at, rowid",
                    (finding_id,),
                ).fetchall()
                return [PatchProposalRecord.model_validate_json(row["record_json"]) for row in rows]
        with self._lock:
            return [
                self._records[pid]
                for pid in self._by_finding.get(finding_id, [])
                if pid in self._records
            ]

    def count_for_finding(self, finding_id: str) -> int:
        """How many attempts have been recorded for a finding."""
        if self._database is not None:
            with self._database.connect() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM patch_proposals WHERE finding_id=?",
                    (finding_id,),
                ).fetchone()
                return int(row["total"])
        with self._lock:
            return len(self._by_finding.get(finding_id, []))

    def get(self, proposal_id: str) -> PatchProposalRecord | None:
        """Return one record by id, or None."""
        with self._lock:
            record = self._records.get(proposal_id)
            if record is not None:
                return record
            if self._database is None:
                return None
            with self._database.connect() as connection:
                row = connection.execute(
                    "SELECT record_json FROM patch_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
            if row is None:
                return None
            record = PatchProposalRecord.model_validate_json(row["record_json"])
            self._records[proposal_id] = record
            self._by_finding.setdefault(record.finding_id, []).append(proposal_id)
            return record

    def get_proposal(self, proposal_id: str) -> PatchProposal | None:
        """Return the validated edits for a proposal (apply-step input)."""
        with self._lock:
            proposal = self._edits.get(proposal_id)
            if proposal is not None or self._database is None:
                return proposal
            with self._database.connect() as connection:
                row = connection.execute(
                    "SELECT proposal_json FROM patch_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
            if row is None or row["proposal_json"] is None:
                return None
            proposal = PatchProposal.model_validate_json(row["proposal_json"])
            self._edits[proposal_id] = proposal
            return proposal


_patch_store = PatchStore(get_local_state_db())


def get_patch_store() -> PatchStore:
    """Return the process-wide patch store (FastAPI dependency)."""
    return _patch_store
