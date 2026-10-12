"""In-memory scan record storage.

This is the persistence slot for the MVP: an in-memory, thread-safe store
written behind a small class interface so a SQLite (or similar) backend can
replace the internals later without touching route handlers or orchestration.

Note: in-memory state is per-process and intentionally NOT durable — scans
do not yet have a background pipeline, so nothing is written to disk. A
later task will move records to SQLite when the scan lifecycle gains
long-running execution.
"""

import threading
from uuid import UUID

from app.ids import new_uuid
from app.schemas import ScanCreateRequest, ScanStatus, ScanResponse


class ScanStore:
    """Thread-safe in-memory scan store behind a swappable interface."""

    def __init__(self) -> None:
        self._scans: dict[UUID, ScanResponse] = {}
        self._lock = threading.Lock()

    def create(self, request: ScanCreateRequest) -> ScanResponse:
        """Create a scan record in its initial state."""
        record = ScanResponse(
            scan_id=new_uuid(),
            status=ScanStatus.created,
            target_url=request.target_url,
            viewports=request.viewports,
            routes=request.routes,
        )
        with self._lock:
            self._scans[record.scan_id] = record
        return record

    def get(self, scan_id: UUID) -> ScanResponse | None:
        """Return the scan record, or None if unknown."""
        with self._lock:
            return self._scans.get(scan_id)

    def list(self) -> list[ScanResponse]:
        """Return all scans, oldest first."""
        with self._lock:
            return list(self._scans.values())

    def replace(self, record: ScanResponse) -> None:
        """Replace the full record (transition or update) atomically."""
        with self._lock:
            if record.scan_id not in self._scans:
                raise KeyError(f"scan {record.scan_id} does not exist")
            self._scans[record.scan_id] = record


_store = ScanStore()


def get_store() -> ScanStore:
    """Return the process-wide scan store (FastAPI dependency)."""
    return _store
