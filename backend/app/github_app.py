"""Least-privilege GitHub App authentication and controlled draft publishing.

Credentials and installation tokens stay on the backend. Repository access
tokens are scoped to one repository with the narrow permissions each operation
needs. Candidate writes use a separate Git Database flow after validation and
explicit confirmation, and only create a draft pull request.
"""

from __future__ import annotations

import re
import time
from base64 import b64decode
from binascii import Error as Base64DecodeError
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from urllib.parse import quote

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, SecretStr

from app.config import Settings
from app.patch_schemas import PatchProposal
from app.patch_validation import validate_patch_proposal

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


class PublishedDraftPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: int
    proposal_id: str
    branch: str
    commit_sha: str
    pull_request_number: int
    pull_request_url: str
    draft: bool
    base_branch: str
    base_sha: str
    diff_sha256: str


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
        self,
        client: httpx.AsyncClient,
        credentials: _AppCredentials,
        owner: str,
        repository: str,
        *,
        permissions: dict[str, str] | None = None,
    ) -> str:
        app_jwt = create_app_jwt(credentials)
        response = await client.post(
            f"/app/installations/{credentials.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
            json={
                "repositories": [repository],
                "permissions": permissions or {"contents": "read"},
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
        granted_permissions = token_payload.get("permissions")
        requested_permissions = permissions or {"contents": "read"}
        if (
            not isinstance(granted_permissions, dict)
            or set(granted_permissions) - set(requested_permissions) - {"metadata"}
            or (
                "metadata" in granted_permissions
                and granted_permissions["metadata"] != "read"
            )
            or any(
                granted_permissions.get(name) != level for name, level in requested_permissions.items()
            )
        ):
            raise GitHubIntegrationFailure(
                "GITHUB_TOKEN_SCOPE_INVALID", "GitHub did not confirm the required repository permissions"
            )
        return token

    async def publish_shoplite_draft_pr(
        self,
        owner: str,
        repository: str,
        *,
        repository_id: int,
        base_branch: str,
        expected_base_sha: str,
        proposal_id: str,
        proposal: PatchProposal,
        confirmed: bool,
    ) -> PublishedDraftPullRequest:
        """Publish the one validated ShopLite CSS edit as an idempotent draft PR.

        This provider operation is not exposed as an HTTP route. Its caller
        must supply the result of a successful sandbox validation and explicit
        user confirmation, and the base branch must still match that revision.
        """
        credentials = _credentials(self._settings)
        if not confirmed:
            raise GitHubIntegrationFailure(
                "GITHUB_CONFIRMATION_REQUIRED", "Explicit confirmation is required to create a draft pull request"
            )
        if (
            owner in {".", ".."}
            or repository in {".", ".."}
            or not _NAME_PATTERN.fullmatch(owner)
            or not _NAME_PATTERN.fullmatch(repository)
        ):
            raise GitHubIntegrationFailure("GITHUB_REPOSITORY_INVALID", "Use a valid GitHub owner and repository name")
        if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0:
            raise GitHubIntegrationFailure("GITHUB_REPOSITORY_INVALID", "The connected repository ID is invalid")
        if not _BRANCH_PATTERN.fullmatch(base_branch) or base_branch.startswith("/") or ".." in base_branch:
            raise GitHubIntegrationFailure("GITHUB_BRANCH_INVALID", "The configured base branch name is invalid")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", expected_base_sha):
            raise GitHubIntegrationFailure("GITHUB_BASE_REVISION_INVALID", "The validated base revision is invalid")
        reasons = validate_patch_proposal(proposal, settings=self._settings)
        if reasons:
            raise GitHubIntegrationFailure(
                "GITHUB_PATCH_REJECTED", "The candidate patch failed repository path or size policy"
            )
        if (
            len(proposal.files) != 1
            or proposal.files[0].path != "styles/checkout.css"
            or len(proposal.files[0].edits) != 1
        ):
            raise GitHubIntegrationFailure(
                "GITHUB_PATCH_REJECTED", "Only one exact edit to the fixed ShopLite checkout stylesheet is supported"
            )

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
            token = await self._installation_token(
                client,
                credentials,
                owner,
                repository,
                permissions={"contents": "write", "pull_requests": "write"},
            )
            headers = {"Authorization": f"Bearer {token}"}
            repo_response = await client.get(f"/repos/{owner}/{repository}", headers=headers)
            self._check_response(repo_response)
            repo_data = repo_response.json()
            if repo_data.get("id") != repository_id:
                raise GitHubIntegrationFailure(
                    "GITHUB_REPOSITORY_CHANGED", "The connected repository identity no longer matches"
                )
            branch_path = quote(base_branch, safe="")
            base_ref_response = await client.get(
                f"/repos/{owner}/{repository}/git/ref/heads/{branch_path}", headers=headers
            )
            self._check_response(base_ref_response)
            current_base_sha = base_ref_response.json().get("object", {}).get("sha")
            if current_base_sha != expected_base_sha:
                raise GitHubIntegrationFailure(
                    "GITHUB_BASE_REVISION_CHANGED",
                    "The base branch moved since validation; rerun validation before publishing",
                )

            proposal_file = proposal.files[0]
            remote_path = "demo-app/styles/checkout.css"
            content_response = await client.get(
                f"/repos/{owner}/{repository}/contents/{remote_path}",
                params={"ref": expected_base_sha},
                headers=headers,
            )
            self._check_response(content_response)
            content_payload = content_response.json()
            if content_payload.get("type") != "file" or content_payload.get("encoding") != "base64":
                raise GitHubIntegrationFailure(
                    "GITHUB_PATCH_REJECTED", "The ShopLite stylesheet is not a supported regular text file"
                )
            try:
                encoded_content = content_payload.get("content")
                if not isinstance(encoded_content, str):
                    raise ValueError("missing encoded file content")
                base_content = b64decode("".join(encoded_content.split()), validate=True).decode("utf-8")
            except (Base64DecodeError, UnicodeDecodeError, ValueError):
                raise GitHubIntegrationFailure(
                    "GITHUB_PATCH_REJECTED", "The ShopLite stylesheet is not valid UTF-8 text"
                ) from None
            if len(base_content.encode("utf-8")) > 100_000:
                raise GitHubIntegrationFailure(
                    "GITHUB_PATCH_REJECTED", "The ShopLite stylesheet exceeds the publication size limit"
                )
            edit = proposal_file.edits[0]
            if base_content.count(edit.old_text) != 1:
                raise GitHubIntegrationFailure(
                    "GITHUB_PATCH_STALE", "The validated ShopLite edit no longer matches the base stylesheet exactly once"
                )
            candidate_content = base_content.replace(edit.old_text, edit.new_text, 1)
            patch_material = f"{remote_path}\0{edit.old_text}\0{edit.new_text}".encode("utf-8")
            diff_hash = sha256(patch_material).hexdigest()
            base_commit_response = await client.get(
                f"/repos/{owner}/{repository}/git/commits/{expected_base_sha}", headers=headers
            )
            self._check_response(base_commit_response)
            base_tree_sha = base_commit_response.json().get("tree", {}).get("sha")
            if not isinstance(base_tree_sha, str):
                raise GitHubIntegrationFailure("GITHUB_RESPONSE_INVALID", "GitHub did not return the base tree")
            tree_response = await client.post(
                f"/repos/{owner}/{repository}/git/trees",
                headers=headers,
                json={
                    "base_tree": base_tree_sha,
                    "tree": [
                        {
                            "path": remote_path,
                            "mode": "100644",
                            "type": "blob",
                            "content": candidate_content,
                        }
                    ],
                },
            )
            self._check_response(tree_response)
            candidate_tree_sha = tree_response.json().get("sha")
            if not isinstance(candidate_tree_sha, str):
                raise GitHubIntegrationFailure("GITHUB_RESPONSE_INVALID", "GitHub did not return the candidate tree")

            safe_proposal_id = re.sub(r"[^A-Za-z0-9-]", "-", proposal_id)[:32].strip("-") or "proposal"
            safe_finding_id = re.sub(r"[^A-Za-z0-9-]", "-", proposal.finding_id)[:24].strip("-") or "finding"
            branch = f"omnisight/fix/{safe_finding_id}-{safe_proposal_id}"
            branch_api_path = quote(branch, safe="")
            existing_ref = await client.get(
                f"/repos/{owner}/{repository}/git/ref/heads/{branch_api_path}", headers=headers
            )
            if existing_ref.status_code == 404:
                commit_response = await client.post(
                    f"/repos/{owner}/{repository}/git/commits",
                    headers=headers,
                    json={
                        "message": "fix(ui): resolve ShopLite checkout overflow\n\n"
                        f"OmniSight-Proposal: {safe_proposal_id}\nOmniSight-Diff-SHA256: {diff_hash}",
                        "tree": candidate_tree_sha,
                        "parents": [expected_base_sha],
                    },
                )
                self._check_response(commit_response)
                candidate_commit_sha = commit_response.json().get("sha")
                if not isinstance(candidate_commit_sha, str):
                    raise GitHubIntegrationFailure("GITHUB_RESPONSE_INVALID", "GitHub did not return the candidate commit")
                ref_response = await client.post(
                    f"/repos/{owner}/{repository}/git/refs",
                    headers=headers,
                    json={"ref": f"refs/heads/{branch}", "sha": candidate_commit_sha},
                )
                if ref_response.status_code in {409, 422}:
                    # A previous uncertain attempt may have created this ref.
                    ref_response = await client.get(
                        f"/repos/{owner}/{repository}/git/ref/heads/{branch_api_path}", headers=headers
                    )
                    self._check_response(ref_response)
                    candidate_commit_sha = ref_response.json().get("object", {}).get("sha")
                    candidate_tree_sha = await self._require_same_tree(
                        client, headers, owner, repository, candidate_commit_sha, candidate_tree_sha, expected_base_sha
                    )
                else:
                    self._check_response(ref_response)
                    if ref_response.json().get("object", {}).get("sha") != candidate_commit_sha:
                        raise GitHubIntegrationFailure(
                            "GITHUB_RESPONSE_INVALID", "GitHub did not confirm the requested branch revision"
                        )
                    await self._require_same_tree(
                        client, headers, owner, repository, candidate_commit_sha, candidate_tree_sha, expected_base_sha
                    )
            else:
                self._check_response(existing_ref)
                candidate_commit_sha = existing_ref.json().get("object", {}).get("sha")
                await self._require_same_tree(
                    client, headers, owner, repository, candidate_commit_sha, candidate_tree_sha, expected_base_sha
                )

            existing_pr_response = await client.get(
                f"/repos/{owner}/{repository}/pulls",
                headers=headers,
                params={"head": f"{owner}:{branch}", "base": base_branch, "state": "open"},
            )
            self._check_response(existing_pr_response)
            existing_prs = existing_pr_response.json()
            if isinstance(existing_prs, list) and existing_prs:
                pr = existing_prs[0]
            else:
                pr_response = await client.post(
                    f"/repos/{owner}/{repository}/pulls",
                    headers=headers,
                    json={
                        "title": "fix(ui): resolve ShopLite checkout overflow",
                        "head": branch,
                        "base": base_branch,
                        "draft": True,
                        "body": (
                            "## OmniSight validated ShopLite patch\n\n"
                            "This draft changes only `demo-app/styles/checkout.css`. "
                            "The fixed ShopLite candidate passed the isolated Playwright checks.\n\n"
                            f"- Base revision: `{expected_base_sha}`\n"
                            f"- Proposal: `{safe_proposal_id}`\n"
                            f"- Candidate diff SHA-256: `{diff_hash}`\n"
                            "- Verification: baseline mobile overflow reproduced; candidate mobile and desktop overflow cleared; "
                            "order confirmation passed; no browser errors or external requests.\n\n"
                            "This PR is a draft for human review. OmniSight does not merge or deploy it."
                        ),
                    },
                )
                self._check_response(pr_response)
                pr = pr_response.json()
            if not isinstance(pr, dict) or not isinstance(pr.get("number"), int) or not isinstance(pr.get("html_url"), str):
                raise GitHubIntegrationFailure("GITHUB_RESPONSE_INVALID", "GitHub did not confirm the pull request")
            head = pr.get("head")
            base = pr.get("base")
            if (
                not isinstance(head, dict)
                or head.get("ref") != branch
                or head.get("sha") != candidate_commit_sha
                or not isinstance(base, dict)
                or base.get("ref") != base_branch
                or base.get("sha") != expected_base_sha
            ):
                raise GitHubIntegrationFailure(
                    "GITHUB_PR_REVISION_CHANGED",
                    "The pull request head or base no longer matches the validated revisions",
                )
            if pr.get("draft") is not True:
                raise GitHubIntegrationFailure("GITHUB_PR_NOT_DRAFT", "GitHub did not confirm the pull request as a draft")
            return PublishedDraftPullRequest(
                repository_id=repository_id,
                proposal_id=proposal_id,
                branch=branch,
                commit_sha=candidate_commit_sha,
                pull_request_number=pr["number"],
                pull_request_url=pr["html_url"],
                draft=True,
                base_branch=base_branch,
                base_sha=expected_base_sha,
                diff_sha256=diff_hash,
            )
        except httpx.TimeoutException:
            raise GitHubIntegrationFailure(
                "GITHUB_REMOTE_OUTCOME_UNKNOWN",
                "GitHub timed out during publication; retry the same proposal to reconcile the branch and draft PR",
            ) from None
        except httpx.RequestError:
            raise GitHubIntegrationFailure(
                "GITHUB_REMOTE_OUTCOME_UNKNOWN",
                "GitHub became unreachable during publication; retry the same proposal to reconcile the remote result",
            ) from None
        finally:
            if own_client:
                await client.aclose()

    async def _require_same_tree(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        owner: str,
        repository: str,
        commit_sha: object,
        expected_tree_sha: str,
        expected_parent_sha: str,
    ) -> str:
        if not isinstance(commit_sha, str):
            raise GitHubIntegrationFailure("GITHUB_RESPONSE_INVALID", "GitHub did not return the candidate branch head")
        response = await client.get(
            f"/repos/{owner}/{repository}/git/commits/{commit_sha}", headers=headers
        )
        self._check_response(response)
        commit_data = response.json()
        tree_sha = commit_data.get("tree", {}).get("sha")
        if tree_sha != expected_tree_sha:
            raise GitHubIntegrationFailure(
                "GITHUB_BRANCH_COLLISION", "The deterministic OmniSight branch already contains different content"
            )
        parents = commit_data.get("parents")
        if (
            not isinstance(parents, list)
            or len(parents) != 1
            or not isinstance(parents[0], dict)
            or parents[0].get("sha") != expected_parent_sha
        ):
            raise GitHubIntegrationFailure(
                "GITHUB_BRANCH_COLLISION", "The deterministic OmniSight branch is not based on the validated revision"
            )
        return expected_tree_sha

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
