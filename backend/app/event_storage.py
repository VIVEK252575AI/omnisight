"""In-memory storage for SIMULATED CI/CD webhook events.

Persistence slot for the Week 1 webhook receiver: a thread-safe, in-memory
store behind a small interface so it can be swapped for SQLite later without
touching the router. As with the scan store, in-memory state is per-process
and intentionally not durable yet.

Idempotency guarantee: :meth:`CIEventStore.record` claims ``event_id`` under
the store lock *before* creating the scan stub, and only the winning call
invokes the scan factory. A failure while creating the scan aborts before
anything is stored, so a crash cannot leave a half-written record — and two
concurrent deliveries of the same ``event_id`` never create two scans.
"""

import threading
from typing import Callable, Protocol
from uuid import UUID

from app.ids import utcnow
from app.schemas import BuildEvent, BuildEventType, CIEventRecord


class CIEventStore(Protocol):
    """Swappable interface for CI event persistence."""

    def record(
        self, event: BuildEvent, make_scan: Callable[[], UUID | None]
    ) -> tuple[CIEventRecord, bool]:
        """Store ``event`` once; return ``(record, created)``."""
        ...

    def get(self, event_id: str) -> CIEventRecord | None:
        """Return the stored event, or ``None`` if unknown."""
        ...

    def list(
        self,
        *,
        event_type: BuildEventType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CIEventRecord]:
        """Return stored events (oldest first), optionally filtered."""
        ...


class InMemoryCIEventStore:
    """Thread-safe in-memory implementation of :class:`CIEventStore`."""

    def __init__(self) -> None:
        self._by_id: dict[str, CIEventRecord] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def record(
        self, event: BuildEvent, make_scan: Callable[[], UUID | None]
    ) -> tuple[CIEventRecord, bool]:
        with self._lock:
            existing = self._by_id.get(event.event_id)
            if existing is not None:
                return existing, False
            # Only the first delivery for this event_id reaches here, and the
            # scan stub is created before the record is written. If it raises,
            # nothing is persisted and a later retry can claim the id again.
            scan_id = (
                make_scan()
                if event.event_type is BuildEventType.succeeded
                else None
            )
            record = CIEventRecord(
                event_id=event.event_id,
                event_type=event.event_type,
                source=event.source,
                repository=event.repository,
                branch=event.branch,
                commit_sha=event.commit_sha,
                build_id=event.build_id,
                target_url=event.target_url,
                timestamp=event.timestamp,
                metadata=event.metadata,
                scan_id=scan_id,
                received_at=utcnow(),
            )
            self._by_id[event.event_id] = record
            self._order.append(event.event_id)
            return record, True

    def get(self, event_id: str) -> CIEventRecord | None:
        with self._lock:
            return self._by_id.get(event_id)

    def list(
        self,
        *,
        event_type: BuildEventType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CIEventRecord]:
        with self._lock:
            records = [self._by_id[event_id] for event_id in self._order]
        if event_type is not None:
            records = [r for r in records if r.event_type is event_type]
        return records[offset : offset + limit]


_event_store: CIEventStore = InMemoryCIEventStore()


def get_event_store() -> CIEventStore:
    """Return the process-wide CI event store (FastAPI dependency)."""
    return _event_store
