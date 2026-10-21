"""Loopback-only local single-owner identity for controlled demos."""

from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, Request, status

from app.config import Settings, get_settings
from app.errors import DomainError, ErrorCode

LOCAL_OWNER_ACTOR_ID = "local-owner"


@dataclass(frozen=True)
class LocalPrincipal:
    actor_id: str
    project_id: str


def require_local_owner(
    request: Request,
    x_omnisight_local_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> LocalPrincipal:
    """Authenticate the one configured local owner; never trusts caller IDs."""
    if settings.auth_mode != "local_single_user":
        raise DomainError(
            ErrorCode.GITHUB_PUBLISH_UNAVAILABLE,
            "Local single-owner authentication is not enabled",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    expected = (
        settings.local_auth_token.get_secret_value()
        if settings.local_auth_token is not None
        else ""
    )
    if len(expected) < 32:
        raise DomainError(
            ErrorCode.GITHUB_PUBLISH_UNAVAILABLE,
            "Configure a local authentication token with at least 32 characters",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    peer = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise DomainError(
            ErrorCode.FORBIDDEN,
            "The local single-owner API only accepts loopback requests",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not x_omnisight_local_token or not secrets.compare_digest(x_omnisight_local_token, expected):
        raise DomainError(
            ErrorCode.UNAUTHORIZED,
            "A valid local owner token is required",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    try:
        project_id = str(UUID(settings.local_project_id))
    except (ValueError, TypeError):
        raise DomainError(
            ErrorCode.GITHUB_PUBLISH_UNAVAILABLE,
            "The configured local project ID is invalid",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from None
    return LocalPrincipal(actor_id=LOCAL_OWNER_ACTOR_ID, project_id=project_id)
