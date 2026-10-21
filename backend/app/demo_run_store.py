"""Thread-safe in-memory state for the fixed local ShopLite demo workflow."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


ACTIVE_STATUSES = frozenset(
    {"queued", "running", "awaiting_sandbox_approval", "validating"}
)
RUN_TTL = timedelta(minutes=30)


@dataclass
class DemoRun:
    run_id: str
    status: str = "queued"
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def response(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DemoRunStore:
    """Keep one fixed-target ShopLite run and its approval state in memory."""

    def __init__(self) -> None:
        self._runs: dict[str, DemoRun] = {}
        self._lock = threading.Lock()

    def create(self) -> DemoRun | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            for run in self._runs.values():
                if run.status in ACTIVE_STATUSES and now - run.updated_at > RUN_TTL:
                    run.status = "failed"
                    run.error = "The demo run expired. Start a new ShopLite run."
                    run.updated_at = now
            if any(run.status in ACTIVE_STATUSES for run in self._runs.values()):
                return None
            run = DemoRun(run_id=f"demo_{uuid4().hex}")
            self._runs[run.run_id] = run
            return run

    def get(self, run_id: str) -> DemoRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> list[DemoRun]:
        """Return the newest fixed-target runs first for the local dashboard."""
        with self._lock:
            return sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)

    def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> DemoRun | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            if status is not None:
                run.status = status
            if result is not None:
                run.result = result
            run.error = error
            run.updated_at = datetime.now(timezone.utc)
            return run


_demo_run_store = DemoRunStore()


def get_demo_run_store() -> DemoRunStore:
    return _demo_run_store
