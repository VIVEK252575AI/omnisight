"""Loopback-only project setup API for the local single-owner application."""

from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, Request, status

from app.config import Settings, get_settings
from app.errors import DomainError, ErrorCode
from app.project_schemas import ProjectCreate, ProjectSummary, TargetAuthorizationRequest
from app.project_store import ProjectStore, get_project_store
from app.target_policy import TargetSafetyError, admit_target

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _require_local_dashboard(request: Request, settings: Settings, *, require_origin: bool = True) -> None:
    client_host = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(client_host).is_loopback:
            raise ValueError
    except ValueError:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "Project setup is available only from this computer",
            status_code=status.HTTP_403_FORBIDDEN,
        ) from None

    origin = request.headers.get("origin")
    if (require_origin and not origin) or (origin and origin not in settings.allowed_origins):
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "Open OmniSight from an allowed local application address",
            status_code=status.HTTP_403_FORBIDDEN,
        )


@router.get("", response_model=list[ProjectSummary])
def list_projects(
    request: Request,
    settings: Settings = Depends(get_settings),
    store: ProjectStore = Depends(get_project_store),
) -> list[ProjectSummary]:
    _require_local_dashboard(request, settings, require_origin=False)
    return store.list()


@router.post("", response_model=ProjectSummary, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: ProjectStore = Depends(get_project_store),
) -> ProjectSummary:
    _require_local_dashboard(request, settings)
    return store.create(payload)


@router.get("/{project_id}", response_model=ProjectSummary)
def get_project(
    project_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: ProjectStore = Depends(get_project_store),
) -> ProjectSummary:
    _require_local_dashboard(request, settings, require_origin=False)
    project = store.get(project_id)
    if project is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Project not found", status_code=status.HTTP_404_NOT_FOUND)
    return project


@router.post("/{project_id}/targets/{target_id}/authorize", response_model=ProjectSummary)
def authorize_project_target(
    project_id: str,
    target_id: str,
    payload: TargetAuthorizationRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: ProjectStore = Depends(get_project_store),
) -> ProjectSummary:
    _require_local_dashboard(request, settings)
    if not payload.confirm_authorization:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "Confirm that you own or are authorized to test this staging target",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    project = store.get(project_id)
    if project is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Project not found", status_code=status.HTTP_404_NOT_FOUND)
    target = next((item for item in project.targets if item.id == target_id), None)
    if target is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Target not found", status_code=status.HTTP_404_NOT_FOUND)
    try:
        allowed_host = admit_target(target.base_url, target.environment_kind)
    except TargetSafetyError as exc:
        raise DomainError(
            ErrorCode.TARGET_NOT_AUTHORIZABLE,
            str(exc),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        ) from None
    updated = store.authorize_target(project_id, target_id, allowed_host)
    if updated is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Target not found", status_code=status.HTTP_404_NOT_FOUND)
    return updated


@router.post("/{project_id}/targets/{target_id}/revoke", response_model=ProjectSummary)
def revoke_project_target(
    project_id: str,
    target_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: ProjectStore = Depends(get_project_store),
) -> ProjectSummary:
    _require_local_dashboard(request, settings)
    updated = store.revoke_target(project_id, target_id)
    if updated is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Target not found", status_code=status.HTTP_404_NOT_FOUND)
    return updated


@router.get("/{project_id}/targets/{target_id}/authorization-events")
def list_target_authorization_events(
    project_id: str,
    target_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    store: ProjectStore = Depends(get_project_store),
) -> list[dict]:
    _require_local_dashboard(request, settings, require_origin=False)
    events = store.authorization_events(project_id, target_id)
    if events is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Target not found", status_code=status.HTTP_404_NOT_FOUND)
    return [event.model_dump(mode="json") for event in events]
