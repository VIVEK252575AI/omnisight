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
from app.db_models import Project, TargetEnvironment
from app.errors import DomainError, ErrorCode
from app.ids import utcnow
from app.project_schemas import ProjectCreate, ProjectSummary, TargetSummary


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


def get_project_store(settings: Settings = Depends(get_settings)) -> ProjectStore:
    return ProjectStore(lambda: get_session_factory(settings)())
