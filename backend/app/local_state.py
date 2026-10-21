"""Small SQLite store for the controlled, single-owner local workflow.

GitHub publication records continue to use the configured PostgreSQL database.
This separate SQLite file keeps local demo runs and validated patch proposals
available across API restarts without making the fixed demo depend on Postgres.
Raw model output, credentials, and screenshots are never written here.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_demo_runs_created_at ON demo_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS patch_proposals (
    proposal_id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    proposal_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_patch_proposals_finding ON patch_proposals(finding_id, created_at);
"""


class LocalStateDB:
    """Open a short-lived SQLite connection for each local workflow operation."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(_SCHEMA)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def get_local_state_db() -> LocalStateDB:
    """Return the configured on-disk local workflow database."""
    from app.config import get_settings

    return LocalStateDB(get_settings().local_state_database_path)
