"""Least-privilege GitHub App authentication and repository verification.

Credentials and installation tokens stay on the backend. Repository access
tokens are scoped to one repository and read-only in this connection slice.
Candidate writes are implemented separately after validation and confirmation.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, SecretStr

from app.config import Settings

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9_./-]{1,250}$")


class GitHubIntegrationFailure(RuntimeError):
    """Safe integration failure without raw GitHub payloads or credentials."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class VerifiedGitHubRepository(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: int
    full_name: str
    default_branch: str
    base_branch: str
    base_sha: str
    verified_at: datetime


@dataclass(frozen=True)
class _AppCredentials:
    app_id: str
    installation_id: str
    private_key: SecretStr


def _credentials(settings: Settings) -> _AppCredentials:
    if not settings.enable_github_app:
        raise GitHubIntegrationFailure("GITHUB_APP_DISABLED", "GitHub App integration is disabled")
    if not (settings.github_app_id and settings.github_app_installation_id and settings.github_app_private_key):
        raise GitHubIntegrationFailure(
            "GITHUB_APP_NOT_CONFIGURED",
            "Set the GitHub App ID, installation ID, and private key on the backend",
        )
    if not settings.github_app_installation_id.strip().isdigit():
        raise GitHubIntegrationFailure(
            "GITHUB_APP_NOT_CONFIGURED", "The GitHub App installation ID must be numeric"
        )
    return _AppCredentials(
        app_id=settings.github_app_id.strip(),
        installation_id=settings.github_app_installation_id.strip(),
        private_key=settings.github_app_private_key,
    )


def create_app_jwt(credentials: _AppCredentials, *, now: int | None = None) -> str:
    """Create a short-lived RS256 JWT with clock-drift allowance."""
    issued_at = int(time.time()) if now is None else now
    pem = credentials.private_key.get_secret_value().replace("\\n", "\n")
    try:
        return jwt.encode(
            {"iat": issued_at - 60, "exp": issued_at + 540, "iss": credentials.app_id},
            pem,
            algorithm="RS256",
        )
    except (TypeError, ValueError, jwt.PyJWTError):
        raise GitHubIntegrationFailure(
            "GITHUB_APP_KEY_INVALID", "The configured GitHub App private key is invalid"
        ) from None


class GitHubAppClient:
    """GitHub App adapter for checking one repository and base revision."""

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http_client = http_client

    async def verify_repository(
        self, owner: str, repository: str, *, base_branch: str | None = None
    ) -> VerifiedGitHubRepository:
        credentials = _credentials(self._settings)
        if (
            owner in {".", ".."}
            or repository in {".", ".."}
            or not _NAME_PATTERN.fullmatch(owner)
            or not _NAME_PATTERN.fullmatch(repository)
        ):
            raise GitHubIntegrationFailure("GITHUB_REPOSITORY_INVALID", "Use a valid GitHub owner and repository name")
        branch = base_branch or ""
        if branch and (not _BRANCH_PATTERN.fullmatch(branch) or branch.startswith("/") or ".." in branch):
            raise GitHubIntegrationFailure("GITHUB_BRANCH_INVALID", "The configured base branch name is invalid")

        own_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            base_url=GITHUB_API_URL,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": "OmniSight-GitHub-App",
            },
        )
        try:
            token = await self._installation_token(client, credentials, owner, repository)
            headers = {"Authorization": f"Bearer {token}"}
            response = await client.get(f"/repos/{owner}/{repository}", headers=headers)
            self._check_response(response)
            repo_data = response.json()
            selected_branch = branch or repo_data.get("default_branch")
            default_branch = repo_data.get("default_branch")
            if (
                not isinstance(selected_branch, str)
                or not selected_branch
                or not isinstance(default_branch, str)
                or not default_branch
            ):
                raise GitHubIntegrationFailure(
                    "GITHUB_REPOSITORY_INVALID", "GitHub did not return a usable default branch"
                )
            encoded_branch = quote(selected_branch, safe="")
            ref_response = await client.get(
                f"/repos/{owner}/{repository}/git/ref/heads/{encoded_branch}", headers=headers
            )
            self._check_response(ref_response)
            ref_data = ref_response.json()
            sha = ref_data.get("object", {}).get("sha")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
                raise GitHubIntegrationFailure(
                    "GITHUB_BRANCH_INVALID", "GitHub did not return a valid base revision"
                )
            repo_id = repo_data.get("id")
            full_name = repo_data.get("full_name")
            if not isinstance(repo_id, int) or not isinstance(full_name, str):
                raise GitHubIntegrationFailure(
                    "GITHUB_REPOSITORY_INVALID", "GitHub returned incomplete repository identity"
                )
            return VerifiedGitHubRepository(
                repository_id=repo_id,
                full_name=full_name,
                default_branch=default_branch,
                base_branch=selected_branch,
                base_sha=sha,
                verified_at=datetime.now().astimezone(),
            )
        except httpx.TimeoutException:
            raise GitHubIntegrationFailure("GITHUB_TIMEOUT", "GitHub did not respond before the request timed out") from None
        except httpx.RequestError:
            raise GitHubIntegrationFailure("GITHUB_UNAVAILABLE", "GitHub could not be reached") from None
        finally:
            if own_client:
                await client.aclose()

    async def _installation_token(
        self, client: httpx.AsyncClient, credentials: _AppCredentials, owner: str, repository: str
    ) -> str:
        app_jwt = create_app_jwt(credentials)
        response = await client.post(
            f"/app/installations/{credentials.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
            json={
                "repositories": [repository],
                "permissions": {"contents": "read"},
            },
        )
        self._check_response(response)
        token_payload = response.json()
        token = token_payload.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubIntegrationFailure(
                "GITHUB_TOKEN_INVALID", "GitHub did not issue a repository access token"
            )
        scoped_repositories = token_payload.get("repositories")
        expected = f"{owner}/{repository}".casefold()
        if (
            not isinstance(scoped_repositories, list)
            or len(scoped_repositories) != 1
            or not isinstance(scoped_repositories[0], dict)
            or str(scoped_repositories[0].get("full_name", "")).casefold() != expected
        ):
            raise GitHubIntegrationFailure(
                "GITHUB_TOKEN_SCOPE_INVALID", "GitHub did not confirm a single-repository access token"
            )
        return token

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code == 429 or (
            response.status_code == 403
            and (response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers)
        ):
            raise GitHubIntegrationFailure("GITHUB_RATE_LIMITED", "GitHub rate limit was reached; retry later")
        if response.status_code in {401, 403}:
            raise GitHubIntegrationFailure(
                "GITHUB_PERMISSION_DENIED", "The GitHub App installation lacks required repository access"
            )
        if response.status_code == 404:
            raise GitHubIntegrationFailure(
                "GITHUB_RESOURCE_NOT_FOUND", "The repository or base branch was not found or is not accessible"
            )
        if response.status_code >= 500:
            raise GitHubIntegrationFailure("GITHUB_UNAVAILABLE", "GitHub is temporarily unavailable")
        raise GitHubIntegrationFailure("GITHUB_REQUEST_REJECTED", "GitHub rejected the repository verification request")
