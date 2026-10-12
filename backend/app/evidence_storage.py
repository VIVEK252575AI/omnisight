"""In-memory storage for scan evidence records and their artifact references.

Week 1 evidence contract persistence slot: thread-safe, per-process, behind a
small class interface so it can move to SQLite later without touching the
router. Artifacts themselves live on disk (under the artifact root); this
store only maps opaque ``artifact_id`` values to already-validated paths, so a
client can never request an arbitrary filesystem path.
"""

import threading
from pathlib import Path
from uuid import UUID

from app.schemas import EvidenceRecord


class EvidenceStore:
    """Thread-safe store of evidence records, grouped by scan."""

    def __init__(self) -> None:
        self._by_scan: dict[UUID, list[EvidenceRecord]] = {}
        self._artifacts: dict[UUID, Path] = {}
        self._lock = threading.Lock()

    def add(
        self,
        record: EvidenceRecord,
        *,
        artifact_id: UUID | None = None,
        artifact_path: Path | None = None,
    ) -> None:
        """Append an evidence record and register its artifact, atomically."""
        with self._lock:
            self._by_scan.setdefault(record.scan_id, []).append(record)
            if artifact_id is not None and artifact_path is not None:
                self._artifacts[artifact_id] = artifact_path

    def list_for_scan(self, scan_id: UUID) -> list[EvidenceRecord]:
        """Return all evidence records for a scan (oldest first)."""
        with self._lock:
            return list(self._by_scan.get(scan_id, []))

    def get_artifact(self, artifact_id: UUID) -> Path | None:
        """Return the validated on-disk path for an artifact id, or None."""
        with self._lock:
            return self._artifacts.get(artifact_id)


_evidence_store = EvidenceStore()


def get_evidence_store() -> EvidenceStore:
    """Return the process-wide evidence store (FastAPI dependency)."""
    return _evidence_store
