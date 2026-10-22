"""PostgreSQL-backed local-owner project and target setup."""

from __future__ import annotations

import re
from collections.abc import Callable
from uuid import uuid4

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.db_models import Project, TargetAuthorizationEvent, TargetEnvironment
from app.errors import DomainError, ErrorCode
from app.ids import utcnow
from app.project_schemas import (
    ProjectCreate,
    ProjectSummary,
    TargetAuthorizationEventSummary,
    TargetSummary,
)


class ProjectStore:
    """Persist projects while keeping the app's current single-owner boundary."""

    def __init__(self, database: Callable[[], Session], *, owner_actor_id: str = "local-owner") -> None:
        self._database = database
        self._owner_actor_id = owner_actor_id

    @staticmethod
    def _summary(project: Project) -> ProjectSummary:
        targets = sorted(project.target_environments, key=lambda target: (target.created_at, target.id))
        return ProjectSummary(
            id=project.id,
            name=project.name,
            slug=project.slug,
            status=project.status,
            version=project.version,
            created_at=project.created_at,
            updated_at=project.updated_at,
            targets=[TargetSummary(
                id=target.id,
                name=target.name,
                base_url=target.base_url,
                environment_kind=target.environment_kind,
                authorization_status=target.authorization_status,
                allowed_hosts=list(target.allowed_hosts or []),
                version=target.version,
                created_at=target.created_at,
            ) for target in targets],
        )

    def list(self) -> list[ProjectSummary]:
        with self._database() as session:
            projects = session.scalars(
                select(Project)
                .options(selectinload(Project.target_environments))
                .where(Project.owner_actor_id == self._owner_actor_id)
                .order_by(Project.created_at.desc(), Project.id)
            ).all()
            return [self._summary(project) for project in projects]

    def get(self, project_id: str) -> ProjectSummary | None:
        with self._database() as session:
            project = session.scalar(
                select(Project)
                .options(selectinload(Project.target_environments))
                .where(Project.id == project_id, Project.owner_actor_id == self._owner_actor_id)
            )
            return self._summary(project) if project else None

    def create(self, payload: ProjectCreate) -> ProjectSummary:
        with self._database() as session:
            duplicate = session.scalar(select(Project.id).where(
                Project.owner_actor_id == self._owner_actor_id,
                func.lower(Project.name) == payload.name.casefold(),
            ))
            if duplicate:
                raise DomainError(
                    ErrorCode.CONFLICT,
                    "A project with this name already exists",
                    status_code=409,
                )

            base_slug = re.sub(r"[^a-z0-9]+", "-", payload.name.casefold()).strip("-")[:64] or "project"
            slug = base_slug
            suffix = 2
            while session.scalar(select(Project.id).where(Project.slug == slug)):
                slug = f"{base_slug[:70 - len(str(suffix))]}-{suffix}"
                suffix += 1

            now = utcnow()
            project = Project(
                id=str(uuid4()),
                name=payload.name,
                slug=slug,
                owner_actor_id=self._owner_actor_id,
                status="active",
                version=1,
                created_at=now,
                updated_at=now,
            )
            target = TargetEnvironment(
                id=str(uuid4()),
                project_id=project.id,
                name="Primary target",
                base_url=payload.target_url,
                environment_kind=payload.environment_kind,
                authorization_status="pending",
                allowed_hosts=[],
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add_all([project, target])
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise DomainError(
                    ErrorCode.CONFLICT,
                    "A project with this name or address was created at the same time",
                    status_code=409,
                ) from None
            return self._summary(project)

    def authorize_target(self, project_id: str, target_id: str, allowed_host: str) -> ProjectSummary | None:
        with self._database() as session:
            project, target = self._owned_target(session, project_id, target_id)
            if project is None or target is None:
                return None
            if target.authorization_status == "approved" and target.allowed_hosts == [allowed_host]:
                return self._summary(project)
            now = utcnow()
            target.authorization_status = "approved"
            target.allowed_hosts = [allowed_host]
            target.version += 1
            target.updated_at = now
            session.add(TargetAuthorizationEvent(
                id=str(uuid4()),
                project_id=project.id,
                target_environment_id=target.id,
                action="authorized",
                actor_id=self._owner_actor_id,
                target_url_snapshot=target.base_url,
                allowed_hosts_snapshot=[allowed_host],
                created_at=now,
            ))
            session.commit()
            return self._summary(project)

    def revoke_target(self, project_id: str, target_id: str) -> ProjectSummary | None:
        with self._database() as session:
            project, target = self._owned_target(session, project_id, target_id)
            if project is None or target is None:
                return None
            if target.authorization_status == "revoked":
                return self._summary(project)
            now = utcnow()
            target.authorization_status = "revoked"
            target.allowed_hosts = []
            target.version += 1
            target.updated_at = now
            session.add(TargetAuthorizationEvent(
                id=str(uuid4()),
                project_id=project.id,
                target_environment_id=target.id,
                action="revoked",
                actor_id=self._owner_actor_id,
                target_url_snapshot=target.base_url,
                allowed_hosts_snapshot=[],
                created_at=now,
            ))
            session.commit()
            return self._summary(project)

    def authorization_events(
        self, project_id: str, target_id: str
    ) -> list[TargetAuthorizationEventSummary] | None:
        with self._database() as session:
            project, target = self._owned_target(session, project_id, target_id)
            if project is None or target is None:
                return None
            events = session.scalars(
                select(TargetAuthorizationEvent)
                .where(
                    TargetAuthorizationEvent.project_id == project_id,
                    TargetAuthorizationEvent.target_environment_id == target_id,
                )
                .order_by(TargetAuthorizationEvent.created_at, TargetAuthorizationEvent.id)
            ).all()
            return [TargetAuthorizationEventSummary(
                id=event.id,
                project_id=event.project_id,
                target_environment_id=event.target_environment_id,
                action=event.action,
                actor_id=event.actor_id,
                target_url_snapshot=event.target_url_snapshot,
                allowed_hosts_snapshot=list(event.allowed_hosts_snapshot),
                created_at=event.created_at,
            ) for event in events]

    def _owned_target(
        self, session: Session, project_id: str, target_id: str
    ) -> tuple[Project | None, TargetEnvironment | None]:
        project = session.scalar(select(Project).where(
            Project.id == project_id,
            Project.owner_actor_id == self._owner_actor_id,
        ))
        target = session.scalar(select(TargetEnvironment).where(
            TargetEnvironment.id == target_id,
            TargetEnvironment.project_id == project_id,
        )) if project is not None else None
        return project, target


def get_project_store(settings: Settings = Depends(get_settings)) -> ProjectStore:
    return ProjectStore(lambda: get_session_factory(settings)())
