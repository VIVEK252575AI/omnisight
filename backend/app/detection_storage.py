"""In-memory persistence for detection results (Week 2, W2.4).

Three separate concerns, deliberately not mixed:

- **validated findings** — the only thing the dashboard-facing API exposes;
- **the last detection run** — adapter label, latency, attempts, reason code;
- **raw model text** — retained in memory ONLY, size-capped by the caller, and
  never returned by an API unless the debug flag opts in. It is never written
  to disk.

Everything sits behind a small class interface so it can move to SQLite later
without touching the router or service, matching the existing store pattern.
"""

from __future__ import annotations

import threading
from uuid import UUID

from app.detection_schemas import DetectionRun, FindingRecord
from app.ai.contracts import FindingStatus


class DetectionStore:
    """Thread-safe store of findings, detection runs and raw model text."""

    def __init__(self) -> None:
        self._findings: dict[str, FindingRecord] = {}
        self._by_scan: dict[UUID, list[str]] = {}
        self._runs: dict[UUID, DetectionRun] = {}
        self._raw: dict[UUID, str] = {}
        self._lock = threading.Lock()

    # -- runs ---------------------------------------------------------------

    def put_run(self, run: DetectionRun) -> DetectionRun:
        """Store the latest detection run for a scan (replacing the previous).

        Raw text is managed separately (see :meth:`set_raw`/:meth:`clear_raw`);
        storing a run never silently discards the retained raw output.
        """
        with self._lock:
            self._runs[run.scan_id] = run
            if run.raw_output is not None:
                self._raw[run.scan_id] = run.raw_output
        return run

    def get_run(self, scan_id: UUID) -> DetectionRun | None:
        """Return the latest detection run for a scan, or None.

        The run's ``raw_output`` is merged in from the raw slot at read time so
        the retained text is not duplicated into every stored run record.
        """
        with self._lock:
            run = self._runs.get(scan_id)
            if run is None:
                return None
            if run.raw_output is None:
                raw = self._raw.get(scan_id)
                if raw is not None:
                    return run.model_copy(update={"raw_output": raw})
            return run

    def set_raw(self, scan_id: UUID, text: str) -> None:
        """Retain raw model text in memory for audit (already size-capped)."""
        with self._lock:
            self._raw[scan_id] = text

    def clear_raw(self, scan_id: UUID) -> None:
        """Drop retained raw text (a new run starts with no prior output)."""
        with self._lock:
            self._raw.pop(scan_id, None)

    def get_raw(self, scan_id: UUID) -> str | None:
        """Return the retained raw model text, or None."""
        with self._lock:
            return self._raw.get(scan_id)

    # -- findings -----------------------------------------------------------

    def replace_findings(
        self, scan_id: UUID, records: list[FindingRecord]
    ) -> list[FindingRecord]:
        """Replace all findings for a scan (a re-detection supersedes the last)."""
        with self._lock:
            for old_id in self._by_scan.get(scan_id, []):
                self._findings.pop(old_id, None)
            ids: list[str] = []
            for record in records:
                self._findings[record.finding.finding_id] = record
                ids.append(record.finding.finding_id)
            self._by_scan[scan_id] = ids
            return list(records)

    def list_for_scan(self, scan_id: UUID) -> list[FindingRecord]:
        """Return the findings for a scan (oldest first)."""
        with self._lock:
            return [
                self._findings[fid]
                for fid in self._by_scan.get(scan_id, [])
                if fid in self._findings
            ]

    def get_finding(self, finding_id: str) -> FindingRecord | None:
        """Return a single finding by its backend-assigned id, or None."""
        with self._lock:
            return self._findings.get(finding_id)

    def compare_and_set_finding_status(
        self,
        finding_id: str,
        *,
        expected: FindingStatus | frozenset[FindingStatus],
        to_status: FindingStatus,
    ) -> FindingRecord | None:
        """Atomically move a finding's status, or return None if it moved first.

        Single-writer guard mirroring :meth:`app.storage.ScanStore.compare_and_transition`:
        used by the action engine so two concurrent proposals cannot both claim
        the same finding.
        """
        expected_set = (
            frozenset({expected}) if isinstance(expected, FindingStatus) else expected
        )
        with self._lock:
            record = self._findings.get(finding_id)
            if record is None or record.finding.status not in expected_set:
                return None
            updated = record.model_copy(
                update={
                    "finding": record.finding.model_copy(update={"status": to_status})
                }
            )
            self._findings[finding_id] = updated
            return updated


_detection_store = DetectionStore()


def get_detection_store() -> DetectionStore:
    """Return the process-wide detection store (FastAPI dependency)."""
    return _detection_store
