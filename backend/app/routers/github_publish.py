"""Loopback-only local-owner connection, ShopLite validation, and draft PR API."""

from __future__ import annotations

from hashlib import sha256
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import LocalPrincipal, require_local_owner
from app.config import Settings, get_settings
from app.db import get_session
from app.db_models import GitHubOperation, Project, PullRequest, RepositoryConnection, ValidationReport, WorkflowEvent
from app.errors import DomainError, ErrorCode
from app.github_app import GitHubAppClient, GitHubIntegrationFailure
from app.ids import utcnow
from app.patch_integrity import shoplite_diff_hash
from app.patch_storage import PatchStore, get_patch_store
from app.shoplite_sandbox import SandboxFailure, run_shoplite_sandbox

router = APIRouter(prefix="/api/v1/local", tags=["local GitHub publishing"])


def _ensure_enabled(settings: Settings) -> None:
    if not settings.enable_github_publish_api:
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, "Local GitHub publishing is disabled", status_code=503)
    if not (settings.enable_github_app and settings.enable_shoplite_sandbox):
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, "GitHub App and ShopLite sandbox must both be enabled", status_code=503)


def _github(settings: Settings) -> GitHubAppClient:
    if not (settings.github_target_owner and settings.github_target_repository):
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, "Configure the fixed GitHub owner and repository", status_code=503)
    return GitHubAppClient(settings)


def _connection(session: Session, project_id: str) -> RepositoryConnection | None:
    return session.scalar(select(RepositoryConnection).where(
        RepositoryConnection.project_id == project_id,
        RepositoryConnection.revoked_at.is_(None),
    ).order_by(RepositoryConnection.updated_at.desc()))


class ConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    repository: str
    base_branch: str
    base_sha: str | None = None
    permission_status: str


@router.post("/github/connection/verify", response_model=ConnectionResponse)
async def verify_connection(
    settings: Settings = Depends(get_settings),
    principal: LocalPrincipal = Depends(require_local_owner),
    session: Session = Depends(get_session),
) -> ConnectionResponse:
    """Verify only the repository fixed in server configuration and persist it."""
    _ensure_enabled(settings)
    client = _github(settings)
    owner, repository, branch = settings.github_target_owner, settings.github_target_repository, settings.github_target_branch
    try:
        verified = await client.verify_repository(owner, repository, base_branch=branch)
    except GitHubIntegrationFailure as exc:
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, str(exc), status_code=503) from None
    project = session.get(Project, principal.project_id)
    now = utcnow()
    if project is None:
        project = Project(id=principal.project_id, name=settings.local_project_name, slug=f"local-{principal.project_id[:8]}", owner_actor_id=principal.actor_id)
        session.add(project)
        session.flush()
    connection = session.scalar(select(RepositoryConnection).where(
        RepositoryConnection.project_id == principal.project_id,
        RepositoryConnection.provider_repository_id == verified.repository_id,
    ))
    if connection is None:
        connection = RepositoryConnection(
            id=str(uuid4()), project_id=principal.project_id, provider="github",
            provider_repository_id=verified.repository_id, owner=owner, repository=repository,
            base_branch=verified.base_branch, installation_id=str(settings.github_app_installation_id),
            permission_status="verified", verified_at=now, version=1,
        )
        session.add(connection)
    else:
        connection.owner, connection.repository = owner, repository
        connection.base_branch, connection.permission_status = verified.base_branch, "verified"
        connection.verified_at, connection.updated_at = now, now
        connection.version += 1
    session.commit()
    return ConnectionResponse(project_id=principal.project_id, repository=verified.full_name,
                              base_branch=verified.base_branch, base_sha=verified.base_sha,
                              permission_status=connection.permission_status)


class ValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str
    proposal_id: str
    status: str
    repository: str
    base_revision: str
    diff_hash: str
    policy_version: str
    checks: dict


@router.post("/shoplite/proposals/{proposal_id}/validate", response_model=ValidationResponse)
async def validate_remote_proposal(
    proposal_id: str,
    settings: Settings = Depends(get_settings),
    principal: LocalPrincipal = Depends(require_local_owner),
    session: Session = Depends(get_session),
    patch_store: PatchStore = Depends(get_patch_store),
) -> ValidationResponse:
    """Validate one proposal against ShopLite files at the verified GitHub base SHA."""
    _ensure_enabled(settings)
    record, proposal = patch_store.get(proposal_id), patch_store.get_proposal(proposal_id)
    if record is None or proposal is None or record.status.value != "proposed":
        raise DomainError(ErrorCode.PATCH_PROPOSAL_NOT_FOUND, "A proposed patch with editable content was not found", status_code=404)
    connection = _connection(session, principal.project_id)
    if connection is None:
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, "Verify the configured GitHub connection first", status_code=409)
    client = _github(settings)
    try:
        verified = await client.verify_repository(connection.owner, connection.repository, base_branch=connection.base_branch)
        source = await client.read_shoplite_source(connection.owner, connection.repository, base_sha=verified.base_sha)
    except GitHubIntegrationFailure as exc:
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, str(exc), status_code=503) from None
    started = utcnow()
    try:
        report = await run_in_threadpool(
            run_shoplite_sandbox, proposal, proposal_id=proposal_id, settings=settings,
            base_revision=verified.base_sha, source_files=source,
        )
    except SandboxFailure as exc:
        raise DomainError(ErrorCode.SHOPLITE_SANDBOX_REJECTED, str(exc), status_code=409,
                          details=[{"field": "sandbox", "code": exc.code}]) from None
    if verified.repository_id != connection.provider_repository_id:
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, "The configured GitHub repository identity changed", status_code=409)
    finished = utcnow()
    validation = ValidationReport(
        id=report.report_id, project_id=principal.project_id,
        repository_connection_id=connection.id, proposal_id=proposal_id, status=report.status,
        base_revision=verified.base_sha, diff_hash=shoplite_diff_hash(proposal),
        policy_version=report.policy_version, checks=report.checks.model_dump(),
        started_at=started, finished_at=finished, created_by=principal.actor_id,
    )
    session.add(validation)
    connection.verified_at = verified.verified_at
    session.commit()
    return ValidationResponse(report_id=validation.id, proposal_id=proposal_id,
        status=validation.status, repository=f"{connection.owner}/{connection.repository}",
        base_revision=validation.base_revision, diff_hash=validation.diff_hash,
        policy_version=validation.policy_version, checks=validation.checks)


class DraftPRRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation_report_id: str
    confirmation: str


class DraftPRResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str
    status: str
    proposal_id: str
    validation_report_id: str
    idempotency_key: str
    draft: bool | None = None
    pull_request_number: int | None = None
    pull_request_url: str | None = None
    safe_error_code: str | None = None


def _operation_response(operation: GitHubOperation) -> DraftPRResponse:
    return DraftPRResponse(operation_id=operation.id, status=operation.status,
        proposal_id=operation.proposal_id, validation_report_id=operation.validation_report_id,
        idempotency_key=operation.idempotency_key, draft=operation.draft,
        pull_request_number=operation.pull_request_number,
        pull_request_url=operation.pull_request_url,
        safe_error_code=operation.safe_error_code)


class GitHubPublishingState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: ConnectionResponse | None = None
    validations: list[ValidationResponse]
    operations: list[DraftPRResponse]


@router.get("/github/state", response_model=GitHubPublishingState)
def get_publishing_state(
    settings: Settings = Depends(get_settings),
    principal: LocalPrincipal = Depends(require_local_owner),
    session: Session = Depends(get_session),
) -> GitHubPublishingState:
    """Restore the local owner's verified repository, validation reports, and PR operations."""
    _ensure_enabled(settings)
    connection = _connection(session, principal.project_id)
    if connection is None:
        return GitHubPublishingState(validations=[], operations=[])

    reports = session.scalars(select(ValidationReport).where(
        ValidationReport.project_id == principal.project_id,
        ValidationReport.repository_connection_id == connection.id,
    ).order_by(ValidationReport.created_at.desc()).limit(20)).all()
    validations = [ValidationResponse(
        report_id=report.id,
        proposal_id=report.proposal_id,
        status=report.status,
        repository=f"{connection.owner}/{connection.repository}",
        base_revision=report.base_revision,
        diff_hash=report.diff_hash,
        policy_version=report.policy_version,
        checks=report.checks,
    ) for report in reports]
    operations = session.scalars(select(GitHubOperation).where(
        GitHubOperation.project_id == principal.project_id,
        GitHubOperation.repository_connection_id == connection.id,
    ).order_by(GitHubOperation.created_at.desc()).limit(20)).all()
    latest_base = reports[0].base_revision if reports else None
    return GitHubPublishingState(
        connection=ConnectionResponse(
            project_id=connection.project_id,
            repository=f"{connection.owner}/{connection.repository}",
            base_branch=connection.base_branch,
            base_sha=latest_base,
            permission_status=connection.permission_status,
        ),
        validations=validations,
        operations=[_operation_response(operation) for operation in operations],
    )


@router.post("/github/draft-prs", response_model=DraftPRResponse)
async def create_draft_pr(
    body: DraftPRRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    settings: Settings = Depends(get_settings),
    principal: LocalPrincipal = Depends(require_local_owner),
    session: Session = Depends(get_session),
    patch_store: PatchStore = Depends(get_patch_store),
) -> DraftPRResponse:
    """Explicitly confirmed, idempotent draft PR creation for one validated proposal."""
    _ensure_enabled(settings)
    if body.confirmation != "create-draft-pr":
        raise DomainError(ErrorCode.FORBIDDEN, "Confirm draft PR creation with confirmation='create-draft-pr'", status_code=403)
    if not idempotency_key or not (8 <= len(idempotency_key) <= 128):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "Provide an Idempotency-Key between 8 and 128 characters", status_code=422)
    report = session.get(ValidationReport, body.validation_report_id)
    connection = _connection(session, principal.project_id)
    if report is None or report.project_id != principal.project_id:
        raise DomainError(ErrorCode.GITHUB_OPERATION_NOT_FOUND, "Validation report not found", status_code=404)
    record, proposal = patch_store.get(report.proposal_id), patch_store.get_proposal(report.proposal_id)
    if connection is None or report.repository_connection_id != connection.id:
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, "Validation report is not bound to the active repository", status_code=409)
    if report.status != "verified" or proposal is None or record is None or record.status.value != "proposed":
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, "A successful validation and live proposal are required", status_code=409)
    current_hash = shoplite_diff_hash(proposal)
    if report.diff_hash != current_hash:
        raise DomainError(ErrorCode.GITHUB_PUBLISH_UNAVAILABLE, "The validated patch no longer matches the stored proposal", status_code=409)
    fingerprint = sha256(json.dumps({"report": report.id, "proposal": report.proposal_id,
        "confirmation": body.confirmation}, sort_keys=True).encode()).hexdigest()
    operation = session.scalar(select(GitHubOperation).where(
        GitHubOperation.project_id == principal.project_id,
        GitHubOperation.idempotency_key == idempotency_key,
    ))
    if operation is not None:
        if operation.request_fingerprint != fingerprint:
            raise DomainError(ErrorCode.GITHUB_IDEMPOTENCY_CONFLICT, "This idempotency key was already used for another request", status_code=409)
        if operation.status in {"succeeded", "running", "failed", "canceled"}:
            return _operation_response(operation)
        # Unknown remote outcome: the provider operation is designed to reconcile by stable proposal branch.
    else:
        operation = GitHubOperation(
            id=str(uuid4()), project_id=principal.project_id, repository_connection_id=connection.id,
            validation_report_id=report.id, proposal_id=report.proposal_id, actor_id=principal.actor_id,
            status="running", idempotency_key=idempotency_key, request_fingerprint=fingerprint,
            expected_base_sha=report.base_revision, diff_hash=report.diff_hash, attempt_count=0,
            started_at=utcnow(),
        )
        session.add(operation)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(select(GitHubOperation).where(
                GitHubOperation.project_id == principal.project_id,
                GitHubOperation.idempotency_key == idempotency_key,
            ))
            if existing is None or existing.request_fingerprint != fingerprint:
                raise DomainError(ErrorCode.GITHUB_IDEMPOTENCY_CONFLICT, "A concurrent request claimed this idempotency key", status_code=409) from None
            return _operation_response(existing)
    operation.status, operation.started_at = "running", utcnow()
    operation.attempt_count += 1
    session.commit()
    client = _github(settings)
    try:
        published = await client.publish_shoplite_draft_pr(
            connection.owner, connection.repository, repository_id=connection.provider_repository_id,
            base_branch=connection.base_branch, expected_base_sha=report.base_revision,
            proposal_id=report.proposal_id, proposal=proposal, confirmed=True,
        )
        operation.status, operation.draft = "succeeded", published.draft
        operation.branch_name, operation.commit_sha = published.branch, published.commit_sha
        operation.pull_request_number, operation.pull_request_url = published.pull_request_number, published.pull_request_url
        operation.finished_at = utcnow()
        now = utcnow()
        session.add(PullRequest(
            id=str(uuid4()), github_operation_id=operation.id, project_id=principal.project_id,
            repository_connection_id=connection.id, proposal_id=report.proposal_id,
            provider_pr_number=published.pull_request_number, url=published.pull_request_url,
            status="draft", branch_name=published.branch, base_branch=published.base_branch,
            base_sha=published.base_sha, head_sha=published.commit_sha,
            title="fix(ui): resolve ShopLite checkout overflow", body_template_version="shoplite-v1",
            last_synced_at=now, created_at=now, updated_at=now,
        ))
        event_status = "succeeded"
    except GitHubIntegrationFailure as exc:
        operation.status = "remote_outcome_unknown" if exc.code == "GITHUB_REMOTE_OUTCOME_UNKNOWN" else "failed"
        operation.safe_error_code, operation.safe_error_message = exc.code, str(exc)
        operation.finished_at = utcnow()
        event_status = operation.status
    except IntegrityError:
        session.rollback()
        raise DomainError(ErrorCode.GITHUB_IDEMPOTENCY_CONFLICT, "A concurrent request claimed this idempotency key", status_code=409) from None
    session.add(WorkflowEvent(
        id=str(uuid4()), project_id=principal.project_id, github_operation_id=operation.id,
        event_type="github.draft_pr." + event_status, event_version=1, actor_type="local_owner",
        actor_id=principal.actor_id, correlation_id=operation.id,
        payload={"proposal_id": report.proposal_id, "validation_report_id": report.id,
                 "draft": operation.draft, "safe_error_code": operation.safe_error_code}, occurred_at=utcnow(),
    ))
    session.commit()
    return _operation_response(operation)


@router.get("/github/operations/{operation_id}", response_model=DraftPRResponse)
def get_operation(
    operation_id: str,
    settings: Settings = Depends(get_settings),
    principal: LocalPrincipal = Depends(require_local_owner),
    session: Session = Depends(get_session),
) -> DraftPRResponse:
    _ensure_enabled(settings)
    operation = session.scalar(select(GitHubOperation).where(
        GitHubOperation.id == operation_id, GitHubOperation.project_id == principal.project_id))
    if operation is None:
        raise DomainError(ErrorCode.GITHUB_OPERATION_NOT_FOUND, "GitHub operation not found", status_code=404)
    return _operation_response(operation)
