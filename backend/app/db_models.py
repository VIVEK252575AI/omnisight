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
    Float,
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
    target_environments: Mapped[list["TargetEnvironment"]] = relationship(back_populates="project")
    test_flows: Mapped[list["TestFlow"]] = relationship(back_populates="project")
    qa_runs: Mapped[list["QARun"]] = relationship(back_populates="project")
    patch_attempts: Mapped[list["PatchAttempt"]] = relationship(back_populates="project")
    detection_attempts: Mapped[list["DetectionAttempt"]] = relationship(back_populates="project")
    findings: Mapped[list["FindingRecordRow"]] = relationship(back_populates="project")
    validation_reports: Mapped[list["ValidationReport"]] = relationship(back_populates="project")
    github_operations: Mapped[list["GitHubOperation"]] = relationship(back_populates="project")

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_projects_status"),
        CheckConstraint("version > 0", name="ck_projects_version"),
    )


class TargetEnvironment(Base):
    __tablename__ = "target_environments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    environment_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    authorization_status: Mapped[str] = mapped_column(String(24), nullable=False)
    allowed_hosts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="target_environments")
    qa_runs: Mapped[list["QARun"]] = relationship(back_populates="target_environment")

    __table_args__ = (
        CheckConstraint("environment_kind IN ('demo', 'development', 'staging', 'other')", name="ck_target_environment_kind"),
        CheckConstraint("authorization_status IN ('pending', 'approved', 'revoked')", name="ck_target_environment_authorization"),
        CheckConstraint("version > 0", name="ck_target_environment_version"),
        UniqueConstraint("project_id", "name", name="uq_target_environment_project_name"),
        Index("ix_target_environments_project", "project_id", "created_at"),
    )


class TestFlow(Base):
    __tablename__ = "test_flows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="test_flows")
    versions: Mapped[list["TestFlowVersion"]] = relationship(back_populates="test_flow")
    qa_runs: Mapped[list["QARun"]] = relationship(back_populates="test_flow")

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_test_flow_status"),
        CheckConstraint("version > 0", name="ck_test_flow_version"),
        UniqueConstraint("project_id", "name", name="uq_test_flow_project_name"),
    )


class TestFlowVersion(Base):
    __tablename__ = "test_flow_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    test_flow_id: Mapped[str] = mapped_column(ForeignKey("test_flows.id", ondelete="RESTRICT"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(120))

    test_flow: Mapped[TestFlow] = relationship(back_populates="versions")
    qa_runs: Mapped[list["QARun"]] = relationship(back_populates="test_flow_version")

    __table_args__ = (
        CheckConstraint("version_number > 0", name="ck_test_flow_version_number"),
        CheckConstraint("schema_version > 0", name="ck_test_flow_schema_version"),
        CheckConstraint("status IN ('draft', 'published', 'retired')", name="ck_test_flow_version_status"),
        UniqueConstraint("test_flow_id", "version_number", name="uq_test_flow_version_number"),
    )


class QARun(Base):
    __tablename__ = "qa_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    target_environment_id: Mapped[str] = mapped_column(ForeignKey("target_environments.id", ondelete="RESTRICT"), nullable=False)
    test_flow_id: Mapped[str] = mapped_column(ForeignKey("test_flows.id", ondelete="RESTRICT"), nullable=False)
    test_flow_version_id: Mapped[str] = mapped_column(ForeignKey("test_flow_versions.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="created")
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="not_applicable")
    current_stage: Mapped[str] = mapped_column(String(40), nullable=False, default="preflight")
    trigger_source: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    requested_by: Mapped[str | None] = mapped_column(String(120))
    viewport_matrix_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    target_revision: Mapped[str | None] = mapped_column(String(128))
    repository_base_revision: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message_safe: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="qa_runs")
    target_environment: Mapped[TargetEnvironment] = relationship(back_populates="qa_runs")
    test_flow: Mapped[TestFlow] = relationship(back_populates="qa_runs")
    test_flow_version: Mapped[TestFlowVersion] = relationship(back_populates="qa_runs")
    steps: Mapped[list["RunStep"]] = relationship(back_populates="run", order_by="RunStep.ordinal")
    patch_attempts: Mapped[list["PatchAttempt"]] = relationship(back_populates="run")
    detection_attempts: Mapped[list["DetectionAttempt"]] = relationship(back_populates="run")
    findings: Mapped[list["FindingRecordRow"]] = relationship(back_populates="run")
    workflow_events: Mapped[list["WorkflowEvent"]] = relationship(
        back_populates="run", order_by="(WorkflowEvent.occurred_at, WorkflowEvent.id)"
    )

    __table_args__ = (
        CheckConstraint("status IN ('created','queued','running','completed','failed','canceled','timed_out','partial')", name="ck_qa_run_status"),
        CheckConstraint("outcome IN ('passed','issues_found','test_failed','analysis_inconclusive','analysis_unavailable','mixed','not_applicable')", name="ck_qa_run_outcome"),
        CheckConstraint("trigger_source IN ('manual','github_webhook','scheduled')", name="ck_qa_run_trigger_source"),
        CheckConstraint("version > 0", name="ck_qa_run_version"),
        Index("ix_qa_runs_project_status_created", "project_id", "status", "created_at"),
    )


class RunStep(Base):
    __tablename__ = "run_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="RESTRICT"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str | None] = mapped_column(String(100))
    step_type: Mapped[str] = mapped_column(String(40), nullable=False)
    display_label: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message_safe: Mapped[str | None] = mapped_column(Text)
    input_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    run: Mapped[QARun] = relationship(back_populates="steps")

    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_run_step_ordinal"),
        CheckConstraint("status IN ('queued','running','passed','failed','skipped','timed_out','canceled','unavailable','inconclusive','blocked')", name="ck_run_step_status"),
        UniqueConstraint("run_id", "ordinal", name="uq_run_step_ordinal"),
        Index("ix_run_steps_run", "run_id", "ordinal"),
    )


class DetectionAttempt(Base):
    __tablename__ = "detection_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_label: Mapped[str] = mapped_column(String(100), nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model_label: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    outcome: Mapped[str | None] = mapped_column(String(32))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    reason_message: Mapped[str | None] = mapped_column(String(500))
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="detection_attempts")
    run: Mapped[QARun] = relationship(back_populates="detection_attempts")

    __table_args__ = (
        CheckConstraint("attempts > 0", name="ck_detection_attempts_count"),
        CheckConstraint("findings_count >= 0", name="ck_detection_attempts_findings"),
        CheckConstraint("duration_ms >= 0", name="ck_detection_attempts_duration"),
        Index("ix_detection_attempts_run_created", "run_id", "created_at", "id"),
    )


class FindingRecordRow(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="RESTRICT"), nullable=False)
    detection_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("detection_attempts.id", ondelete="RESTRICT"))
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(24))
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    project: Mapped[Project] = relationship(back_populates="findings")
    run: Mapped[QARun] = relationship(back_populates="findings")
    detection_attempt: Mapped[DetectionAttempt | None] = relationship()

    __table_args__ = (
        CheckConstraint("status IN ('detected','patch_proposed')", name="ck_findings_status"),
        CheckConstraint("version > 0", name="ck_findings_version"),
        Index("ix_findings_run_current_created", "run_id", "is_current", "created_at"),
        Index("ix_findings_project_status_created", "project_id", "status", "created_at"),
    )


class PatchAttempt(Base):
    __tablename__ = "patch_attempts"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    finding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("qa_runs.id", ondelete="RESTRICT"))
    repository_connection_id: Mapped[str | None] = mapped_column(ForeignKey("repository_connections.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_revision: Mapped[str | None] = mapped_column(String(128))
    branch_name: Mapped[str | None] = mapped_column(String(250))
    model_provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(160))
    prompt_version: Mapped[str | None] = mapped_column(String(80))
    patch_format_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    changed_files_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    diff_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_check_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    validated_patch_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    proposal_record_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    validation_report_id: Mapped[str | None] = mapped_column(String(36))
    requested_by: Mapped[str | None] = mapped_column(String(120))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message_safe: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="patch_attempts")
    run: Mapped[QARun | None] = relationship(back_populates="patch_attempts")
    repository_connection: Mapped["RepositoryConnection | None"] = relationship()

    __table_args__ = (
        CheckConstraint("status IN ('requested','queued','generating','proposed','rejected_by_policy','applying','validating','validation_passed','validation_failed','validation_inconclusive','blocked','pr_eligible','pr_requested','pr_created','pr_failed','remote_outcome_unknown','canceled')", name="ck_patch_attempt_status"),
        CheckConstraint("attempt_number > 0", name="ck_patch_attempt_number"),
        CheckConstraint("patch_format_version > 0", name="ck_patch_format_version"),
        Index("ix_patch_attempts_project_created", "project_id", "created_at"),
        Index("ix_patch_attempts_run", "run_id", "created_at"),
        Index("ix_patch_attempts_finding", "finding_id", "created_at"),
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
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("qa_runs.id", ondelete="RESTRICT", name="fk_workflow_events_run_id_qa_runs")
    )
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

    run: Mapped[QARun | None] = relationship(back_populates="workflow_events")

    __table_args__ = (
        CheckConstraint("event_version > 0", name="ck_workflow_events_version"),
        Index("ix_workflow_events_project_occurred", "project_id", "occurred_at"),
        Index("ix_workflow_events_run_occurred", "run_id", "occurred_at"),
    )
