"""State store for the fixed local ShopLite demo workflow."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.local_state import LocalStateDB, get_local_state_db


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
    """Keep fixed-target runs in SQLite, or in memory for isolated unit tests."""

    def __init__(self, database: LocalStateDB | None = None) -> None:
        self._database = database
        self._runs: dict[str, DemoRun] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _from_row(row) -> DemoRun:
        return DemoRun(
            run_id=row["run_id"],
            status=row["status"],
            result=json.loads(row["result_json"]),
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _expire_stale(connection, now: datetime) -> None:
        cutoff = (now - RUN_TTL).isoformat()
        connection.execute(
            "UPDATE demo_runs SET status='failed', error=?, updated_at=? "
            "WHERE status IN ('queued','running','awaiting_sandbox_approval','validating') "
            "AND updated_at < ?",
            ("The demo run expired. Start a new ShopLite run.", now.isoformat(), cutoff),
        )

    def create(self) -> DemoRun | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            if self._database is not None:
                with self._database.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._expire_stale(connection, now)
                    active = connection.execute(
                        "SELECT 1 FROM demo_runs WHERE status IN "
                        "('queued','running','awaiting_sandbox_approval','validating') LIMIT 1"
                    ).fetchone()
                    if active:
                        return None
                    run = DemoRun(run_id=f"demo_{uuid4().hex}")
                    connection.execute(
                        "INSERT INTO demo_runs(run_id,status,result_json,error,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (run.run_id, run.status, "{}", None, run.created_at.isoformat(), run.updated_at.isoformat()),
                    )
                    return run
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
        if self._database is not None:
            now = datetime.now(timezone.utc)
            with self._database.connect() as connection:
                self._expire_stale(connection, now)
                row = connection.execute("SELECT * FROM demo_runs WHERE run_id=?", (run_id,)).fetchone()
                return self._from_row(row) if row else None
        with self._lock:
            return self._runs.get(run_id)

    def list(self) -> list[DemoRun]:
        """Return newest fixed-target runs first for the local dashboard."""
        if self._database is not None:
            now = datetime.now(timezone.utc)
            with self._database.connect() as connection:
                self._expire_stale(connection, now)
                rows = connection.execute("SELECT * FROM demo_runs ORDER BY created_at DESC").fetchall()
                return [self._from_row(row) for row in rows]
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
        if self._database is not None:
            with self._database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM demo_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    return None
                run = self._from_row(row)
                if status is not None:
                    run.status = status
                if result is not None:
                    run.result = result
                run.error = error
                run.updated_at = datetime.now(timezone.utc)
                connection.execute(
                    "UPDATE demo_runs SET status=?, result_json=?, error=?, updated_at=? WHERE run_id=?",
                    (run.status, json.dumps(run.result, ensure_ascii=False), run.error, run.updated_at.isoformat(), run_id),
                )
                return run
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


_demo_run_store = DemoRunStore(get_local_state_db())


def get_demo_run_store() -> DemoRunStore:
    return _demo_run_store
