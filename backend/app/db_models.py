"""Durable publication-domain records.

These records deliberately contain references and safe summaries only. GitHub
private keys, installation tokens, patch source text, and raw provider payloads
must never be persisted in these tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.ids import utcnow


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    owner_actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    repository_connections: Mapped[list["RepositoryConnection"]] = relationship(back_populates="project")
    validation_reports: Mapped[list["ValidationReport"]] = relationship(back_populates="project")
    github_operations: Mapped[list["GitHubOperation"]] = relationship(back_populates="project")

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_projects_status"),
        CheckConstraint("version > 0", name="ck_projects_version"),
    )


class RepositoryConnection(Base):
    __tablename__ = "repository_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="github")
    provider_repository_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    repository: Mapped[str] = mapped_column(String(100), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(250), nullable=False)
    installation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    permission_status: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="repository_connections")
    validation_reports: Mapped[list["ValidationReport"]] = relationship(back_populates="connection")
    github_operations: Mapped[list["GitHubOperation"]] = relationship(back_populates="connection")

    __table_args__ = (
        UniqueConstraint("project_id", "provider", "provider_repository_id", name="uq_repo_connection_project_repo"),
        CheckConstraint("provider_repository_id > 0", name="ck_repo_connection_provider_id"),
        CheckConstraint("version > 0", name="ck_repo_connection_version"),
        Index("ix_repo_connections_project_updated", "project_id", "updated_at"),
    )


class ValidationReport(Base):
    __tablename__ = "validation_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    repository_connection_id: Mapped[str] = mapped_column(
        ForeignKey("repository_connections.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    base_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    diff_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="validation_reports")
    connection: Mapped[RepositoryConnection] = relationship(back_populates="validation_reports")
    github_operations: Mapped[list["GitHubOperation"]] = relationship(back_populates="validation_report")

    __table_args__ = (
        CheckConstraint("status IN ('verified', 'failed')", name="ck_validation_reports_status"),
        Index("ix_validation_reports_proposal_created", "proposal_id", "created_at"),
    )


class GitHubOperation(Base):
    __tablename__ = "github_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    repository_connection_id: Mapped[str] = mapped_column(
        ForeignKey("repository_connections.id", ondelete="RESTRICT"), nullable=False
    )
    validation_report_id: Mapped[str] = mapped_column(
        ForeignKey("validation_reports.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False, default="create_pull_request")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    diff_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String(250))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    pull_request_number: Mapped[int | None] = mapped_column(Integer)
    pull_request_url: Mapped[str | None] = mapped_column(Text)
    draft: Mapped[bool | None] = mapped_column(Boolean)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(80))
    safe_error_message: Mapped[str | None] = mapped_column(Text)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project] = relationship(back_populates="github_operations")
    connection: Mapped[RepositoryConnection] = relationship(back_populates="github_operations")
    validation_report: Mapped[ValidationReport] = relationship(back_populates="github_operations")

    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_github_operation_project_idempotency"),
        CheckConstraint(
            "operation_type IN ('check_permissions', 'create_branch', 'create_commit', "
            "'create_pull_request', 'reconcile_remote_state')",
            name="ck_github_operations_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'remote_outcome_unknown', 'canceled')",
            name="ck_github_operations_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_github_operations_attempts"),
        Index("ix_github_operations_project_state", "project_id", "status", "created_at"),
        Index("ix_github_operations_proposal", "proposal_id", "created_at"),
    )


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    github_operation_id: Mapped[str] = mapped_column(
        ForeignKey("github_operations.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    repository_connection_id: Mapped[str] = mapped_column(
        ForeignKey("repository_connections.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    branch_name: Mapped[str] = mapped_column(String(250), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(250), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    body_template_version: Mapped[str] = mapped_column(String(40), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("repository_connection_id", "provider_pr_number", name="uq_pull_request_remote"),
        CheckConstraint("status IN ('open', 'draft', 'closed', 'merged', 'unknown')", name="ck_pull_requests_status"),
        CheckConstraint("provider_pr_number > 0", name="ck_pull_requests_number"),
        Index("ix_pull_requests_project_updated", "project_id", "updated_at"),
    )


class WorkflowEvent(Base):
    __tablename__ = "workflow_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    github_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("github_operations.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("event_version > 0", name="ck_workflow_events_version"),
        Index("ix_workflow_events_project_occurred", "project_id", "occurred_at"),
    )
