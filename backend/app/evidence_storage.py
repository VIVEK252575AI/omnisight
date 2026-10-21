"""Durable observation metadata with private image files stored outside PostgreSQL."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import timezone
from pathlib import Path
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifacts import artifact_root, media_type_for
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.db_models import Artifact, Evidence, QARun
from app.ids import utcnow
from app.schemas import EvidenceRecord, Observation


def _as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class EvidenceStore:
    """Persist evidence records while retaining a lightweight in-memory test mode."""

    def __init__(
        self,
        database: Callable[[], Session] | None = None,
        *,
        settings: Settings | None = None,
        artifacts_root: Path | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or get_settings()
        self._root = (artifacts_root or artifact_root(self._settings)).resolve()
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
        """Append evidence and its artifact reference in one database transaction."""
        if self._database is None:
            with self._lock:
                self._by_scan.setdefault(record.scan_id, []).append(record)
                if artifact_id is not None and artifact_path is not None:
                    self._artifacts[artifact_id] = artifact_path
            return

        if artifact_id is None or artifact_path is None:
            raise ValueError("persistent evidence requires an artifact id and file")
        resolved = artifact_path.resolve(strict=True)
        try:
            relative_key = resolved.relative_to(self._root).as_posix()
        except ValueError:
            raise ValueError("artifact file must be under the configured private artifact root") from None
        if not resolved.is_file():
            raise ValueError("artifact file is not a regular file")

        digest = hashlib.sha256()
        byte_size = 0
        with resolved.open("rb") as image:
            for chunk in iter(lambda: image.read(64 * 1024), b""):
                byte_size += len(chunk)
                digest.update(chunk)
        if byte_size <= 0:
            raise ValueError("artifact file is empty")

        with self._database() as session:
            run = session.scalar(select(QARun).where(
                QARun.id == str(record.scan_id),
                QARun.project_id == self._settings.local_project_id,
            ))
            if run is None:
                raise KeyError(f"QA run {record.scan_id} does not exist")
            session.add(Artifact(
                id=str(artifact_id),
                project_id=run.project_id,
                run_id=run.id,
                storage_backend="local",
                object_key=relative_key,
                content_type=media_type_for(resolved),
                byte_size=byte_size,
                sha256=digest.hexdigest(),
                created_at=record.created_at,
            ))
            session.add(Evidence(
                id=str(record.evidence_id),
                project_id=run.project_id,
                run_id=run.id,
                artifact_id=str(artifact_id),
                observation_json=record.observation.model_dump(mode="json"),
                created_at=record.created_at,
            ))
            session.commit()

    def add_artifact(
        self,
        run_id: str,
        *,
        artifact_id: UUID,
        artifact_path: Path,
    ) -> None:
        """Register a private run artifact that has no observation record."""
        resolved = artifact_path.resolve(strict=True)
        try:
            relative_key = resolved.relative_to(self._root).as_posix()
        except ValueError:
            raise ValueError("artifact file must be under the configured private artifact root") from None
        if not resolved.is_file():
            raise ValueError("artifact file is not a regular file")
        byte_size = resolved.stat().st_size
        if byte_size <= 0 or byte_size > self._settings.artifact_max_image_bytes:
            raise ValueError("artifact file is empty or exceeds the image size limit")
        digest = hashlib.sha256()
        with resolved.open("rb") as image:
            for chunk in iter(lambda: image.read(64 * 1024), b""):
                digest.update(chunk)

        if self._database is None:
            with self._lock:
                self._artifacts[artifact_id] = resolved
            return

        with self._database() as session:
            run = session.scalar(select(QARun).where(
                QARun.id == run_id,
                QARun.project_id == self._settings.local_project_id,
            ))
            if run is None:
                raise KeyError(f"QA run {run_id} does not exist")
            session.add(Artifact(
                id=str(artifact_id),
                project_id=run.project_id,
                run_id=run.id,
                storage_backend="local",
                object_key=relative_key,
                content_type=media_type_for(resolved),
                byte_size=byte_size,
                sha256=digest.hexdigest(),
                created_at=utcnow(),
            ))
            session.commit()

    def list_for_scan(self, scan_id: UUID) -> list[EvidenceRecord]:
        """Return all stored evidence for a scan (oldest first)."""
        if self._database is None:
            with self._lock:
                return list(self._by_scan.get(scan_id, []))
        with self._database() as session:
            rows = session.scalars(select(Evidence).where(
                Evidence.run_id == str(scan_id),
                Evidence.project_id == self._settings.local_project_id,
            ).order_by(Evidence.created_at, Evidence.id)).all()
            return [EvidenceRecord(
                evidence_id=UUID(row.id),
                scan_id=UUID(row.run_id),
                observation=Observation.model_validate(row.observation_json),
                screenshot_artifact_id=UUID(row.artifact_id),
                created_at=_as_utc(row.created_at),
            ) for row in rows]

    def get_artifact(self, artifact_id: UUID) -> Path | None:
        """Resolve an opaque id to a file beneath private storage, if still intact."""
        if self._database is None:
            with self._lock:
                return self._artifacts.get(artifact_id)
        with self._database() as session:
            row = session.scalar(select(Artifact).where(
                Artifact.id == str(artifact_id),
                Artifact.project_id == self._settings.local_project_id,
            ))
            if row is None or row.storage_backend != "local":
                return None
            key = Path(row.object_key)
            if key.is_absolute() or key.drive or any(part in {"..", ""} for part in key.parts):
                return None
            path = (self._root / key).resolve()
            try:
                path.relative_to(self._root)
            except ValueError:
                return None
            if not path.is_file() or path.stat().st_size != row.byte_size:
                return None
            digest = hashlib.sha256()
            with path.open("rb") as image:
                for chunk in iter(lambda: image.read(64 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != row.sha256:
                return None
            return path


def get_evidence_store(settings: Settings = Depends(get_settings)) -> EvidenceStore:
    """Return the PostgreSQL-backed evidence store used by request handlers."""
    return EvidenceStore(lambda: get_session_factory(settings)(), settings=settings)
