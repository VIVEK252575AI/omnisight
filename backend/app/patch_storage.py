"""In-memory persistence for patch proposals (Week 2, W2.5).

Mirrors the existing store pattern (``app.storage``, ``app.detection_storage``)
so it can move to SQLite later without touching the router or engine.

Two things are kept per proposal, deliberately separated:

- the **record** — status, reasons, file summaries, provenance (safe to return);
- the **validated proposal** — the exact ``old_text``/``new_text`` edits, used by
  the later apply step and never returned by the API.
"""

from __future__ import annotations

import threading

from app.patch_schemas import PatchProposal, PatchProposalRecord


class PatchStore:
    """Thread-safe store of patch-proposal records."""

    def __init__(self) -> None:
        self._records: dict[str, PatchProposalRecord] = {}
        self._by_finding: dict[str, list[str]] = {}
        self._edits: dict[str, PatchProposal] = {}
        self._lock = threading.Lock()

    def add(
        self, record: PatchProposalRecord, proposal: PatchProposal | None = None
    ) -> PatchProposalRecord:
        """Store one proposal attempt (append-only: attempts are never replaced)."""
        with self._lock:
            self._records[record.proposal_id] = record
            self._by_finding.setdefault(record.finding_id, []).append(record.proposal_id)
            if proposal is not None:
                self._edits[record.proposal_id] = proposal
        return record

    def list_for_finding(self, finding_id: str) -> list[PatchProposalRecord]:
        """Return every attempt for a finding, oldest first."""
        with self._lock:
            return [
                self._records[pid]
                for pid in self._by_finding.get(finding_id, [])
                if pid in self._records
            ]

    def count_for_finding(self, finding_id: str) -> int:
        """How many attempts have been recorded for a finding."""
        with self._lock:
            return len(self._by_finding.get(finding_id, []))

    def get(self, proposal_id: str) -> PatchProposalRecord | None:
        """Return one record by id, or None."""
        with self._lock:
            return self._records.get(proposal_id)

    def get_proposal(self, proposal_id: str) -> PatchProposal | None:
        """Return the validated edits for a proposal (apply-step input)."""
        with self._lock:
            return self._edits.get(proposal_id)


_patch_store = PatchStore()


def get_patch_store() -> PatchStore:
    """Return the process-wide patch store (FastAPI dependency)."""
    return _patch_store
