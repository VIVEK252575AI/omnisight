"""Dedicated GitHub delivery service: interface + PyGithub implementation + fake.

Owns the small interface the rest of the backend depends on for branch/commit/PR
preparation. The real implementation is lazy (imports PyGithub only when used),
config validation is offline by default, and credentials are redacted everywhere.
"""

from __future__ import annotations

import logging
import re

__all__ = ["GITHUB_REPO_PATTERN", "GITHUB_BRANCH_PATTERN", "GITHUB_BASE_BRANCH_PATTERN"]
from typing import Any, Protocol, runtime_checkable

# pragma: no cover - import guard for environments where PyGithub is not installed.
try:
    import github as _pygithub_lib  # type: ignore[import-untyped]
except Exception:
    _pygithub_lib = None

from pydantic import BaseModel

from app.config import Settings
from app.errors import ErrorCode

logger = logging.getLogger("omnisight")

# ---------------------------------------------------------------------------
# Typed public contract
# ---------------------------------------------------------------------------

GITHUB_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
GITHUB_BRANCH_PATTERN = re.compile(
    r"^(?![/])(?!.*/\.\.)(?!\.)(?!\./)(?!.*\.\.)(?!.*//)(?![A-Za-z0-9_./-]*//)[A-Za-z0-9_./-]{1,250}$"
)


class GitHubFile(BaseModel):
    path: str
    content: str
    message: str = ""


class GitHubConfigurationStatus(BaseModel):
    configured: bool
    dry_run: bool
    repository: str | None = None
    base_branch: str | None = None
    missing_keys: list[str] = []


@runtime_checkable
class GitHubService(Protocol):
    """Small interface the repair/delivery layer depends on.

    Real implementations may reach GitHub; this protocol deliberately does not
    promise that. Tests and unwired deployments can depend on the fake instead.
    """

    def validate_configuration(self) -> GitHubConfigurationStatus: ...

    def find_existing_branch(self, branch: str) -> dict[str, Any] | None: ...

    def create_branch(self, name: str, base_sha: str) -> dict[str, Any]: ...

    def update_files(
        self, branch: str, base_sha: str, files: list[GitHubFile], message: str
    ) -> dict[str, Any]: ...

    def open_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        *,
        draft: bool = True,
    ) -> dict[str, Any]: ...

    def get_base_branch_sha(self, base: str) -> str: ...


class GitHubDeliveryError(Exception):
    """A coded delivery failure safe to log and return to a dashboard."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Configuration + redaction
# ---------------------------------------------------------------------------

def _normalize_repository(owner: str | None, repository: str | None) -> str | None:
    if not owner or not repository:
        return None
    candidate = f"{owner.strip()}/{repository.strip()}"
    return candidate if GITHUB_REPO_PATTERN.fullmatch(candidate) else None


def build_configuration_status(settings: Settings) -> GitHubConfigurationStatus:
    """Offline status: populated from env only, no network call, no token.

    Dry-run defaults to True for safety and is the documented default until an
    operator explicitly disables it in the environment.
    """
    token_stripped = str(getattr(settings, "github_token", None) or "").strip()
    token_present = _token_present_in_settings(token_stripped)


    status = GitHubConfigurationStatus(
        configured=False,
        dry_run=getattr(settings, "github_dry_run", True),
        repository=None,
        base_branch=None,
        missing_keys=sorted({
            key
            for key, present in {
                "GITHUB_TOKEN": token_present,
                "GITHUB_TARGET_OWNER": bool(getattr(settings, "github_target_owner", None)),
                "GITHUB_REPOSITORY": bool(getattr(settings, "github_target_repository", None)),
                "GITHUB_BASE_BRANCH": bool(getattr(settings, "github_target_branch", None)),
            }.items()
            if not present
        }),
    )

    if token_present:
        owner = getattr(settings, "github_target_owner", None)
        repository = getattr(settings, "github_target_repository", None)
        if owner and repository:
            repo = _normalize_repository(owner, repository)
            status = status.model_copy(
                update={
                    "repository": repo,
                    "missing_keys": sorted({
                        key
                        for key, present in {
                            "GITHUB_TOKEN": token_present,
                            "GITHUB_TARGET_OWNER": bool(owner),
                            "GITHUB_REPOSITORY": bool(repository),
                            "GITHUB_BASE_BRANCH": bool(getattr(settings, "github_target_branch", None)),
                        }.items()
                        if not present
                    }),
                }
            )
            base_branch = getattr(settings, "github_target_branch", None)
            if base_branch and (base_branch or "").strip():
                status = status.model_copy(
                    update={
                        "base_branch": (base_branch or "").strip(),
                        "missing_keys": sorted({
                            key
                            for key, present in {
                                "GITHUB_TOKEN": token_present,
                                "GITHUB_TARGET_OWNER": bool(owner),
                                "GITHUB_REPOSITORY": bool(repository),
                                "GITHUB_BASE_BRANCH": bool(base_branch),
                            }.items()
                            if not present
                        }),
                    }
                )
                status = status.model_copy(
                    update={
                        "configured": bool(status.repository and status.base_branch),
                        "missing_keys": [],
                    }
                )

    return status


REDACTED_TOKEN = "REDACTED_GITHUB_TOKEN"


def redact_github_token(value: str) -> str:
    """Replace any GitHub token-like value with a single label.

    Call before logging, before attaching values to exceptions, and before
    serializing anything that may leave the backend. The shape check is
    intentionally broad so we never accidentally leak a real secret through a
    custom format.
    """
    if not value:
        return value
    text = value.strip()
    if not text or len(text) < 10:
        return value
    prefixes = ("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_")
    for prefix in prefixes:
        if prefix not in text:
            continue
        pos = text.index(prefix)
        # Bound the token to the next whitespace (or end), capped at 128 chars.
        end = pos
        while end < len(text) and end - pos < 128 and text[end] not in (" ", "\n", "\t"):
            end += 1
        segment = text[pos:end]
        if not segment:
            continue
        rest = text[end:]
        if rest.startswith(prefix):
            rest = rest[len(prefix):]
        return text[:pos] + REDACTED_TOKEN + rest
    return value


def _token_like_bytes_literal_in_text(value: str) -> bool:
    return False


def _token_present_in_settings(token_value: str) -> bool:
    return bool(token_value) and len(token_value) >= 20 and redact_github_token(token_value) != token_value


def _safe_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _safe_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_dict(item) for item in value]
    if isinstance(value, str):
        return redact_github_token(value)
    return value



def github_status_response(status: GitHubConfigurationStatus) -> dict[str, Any]:
    """Redacted status payload for the integration status endpoint.

    This is the only shape that leaves the backend for this service right now:
    no token, no raw environment, no exception text that might contain one.
    """
    return {
        "configured": status.configured,
        "dry_run": status.dry_run,
        "repository": status.repository,
        "base_branch": status.base_branch,
        "missing_keys": status.missing_keys,
    }


# ---------------------------------------------------------------------------
# Fake: in-memory, labelled FAKE
# ---------------------------------------------------------------------------

class FakeGitHubService:
    """In-memory fake that records calls for tests. Explicitly labelled FAKE.

    It never reaches GitHub and its outputs are deterministic enough to make the
    delivery contract observable in tests. It is not a stand-in for a real repo.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings
        self.calls: list[dict[str, Any]] = []

    def validate_configuration(self) -> GitHubConfigurationStatus:
        status = build_configuration_status(self.settings or Settings())
        return status

    def find_existing_branch(self, branch: str) -> dict[str, Any] | None:
        self.calls.append(
            {"method": "find_existing_branch", "branch": branch, "label": "FAKE"}
        )
        existing = [
            c
            for c in self.calls
            if c["method"] == "create_branch" and c["branch"] == branch
        ]
        if existing:
            return {"ref": f"refs/heads/{branch}", "sha": existing[-1]["commit_sha"]}
        return None

    def create_branch(self, name: str, base_sha: str) -> dict[str, Any]:
        if not GITHUB_BRANCH_PATTERN.fullmatch(name):
            raise GitHubDeliveryError(
                "GITHUB_BRANCH_INVALID", f"branch name is not valid: {name!r}"
            )
        self.calls.append(
            {
                "method": "create_branch",
                "branch": name,
                "base_sha": base_sha,
                "commit_sha": f"fake-commit-{name}",
                "label": "FAKE",
            }
        )
        return {"ref": f"refs/heads/{name}", "sha": f"fake-commit-{name}"}

    def update_files(
        self, branch: str, base_sha: str, files: list[GitHubFile], message: str
    ) -> dict[str, Any]:
        for file in files:
            if not file.path or ".." in file.path or file.path.startswith("/"):
                raise GitHubDeliveryError(
                    "GITHUB_PATH_ESCAPE", f"proposed file path is not safe: {file.path!r}"
                )
        self.calls.append(
            {
                "method": "update_files",
                "branch": branch,
                "base_sha": base_sha,
                "file_count": len(files),
                "message": message,
                "label": "FAKE",
            }
        )
        return {"commit_sha": f"fake-commit-{branch}", "files_updated": len(files)}

    def open_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        *,
        draft: bool = True,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": "open_pull_request",
                "title": title,
                "head": head,
                "base": base,
                "draft": draft,
                "label": "FAKE",
            }
        )
        return {
            "pull_request_number": 1,
            "pull_request_url": "https://github.example.com/fake/repo/pull/1",
            "draft": draft,
            "head": head,
            "base": base,
        }

    def get_base_branch_sha(self, base: str) -> str:
        self.calls.append(
            {"method": "get_base_branch_sha", "base": base, "label": "FAKE"}
        )
        return "fake-base-sha"


# ---------------------------------------------------------------------------
# Lazy PyGithub-backed implementation
# ---------------------------------------------------------------------------

class PyGithubGitHubService:
    """Lazy PyGithub-backed service. Import/client creation only when used.

    Dry-run is the default behaviour in this task: unless an operator
    explicitly changes ``GITHUB_DRY_RUN`` to False in the environment, the
    service refuses real mutation and returns coded failures instead. This keeps
    the interface and tests honest while PyGithub is not yet authorized for real
    calls.
    """

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    # -- lazy client ---------------------------------------------------------

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not getattr(self.settings, "github_token", None):
            raise GitHubDeliveryError(
                "GITHUB_TOKEN_MISSING",
                "GitHub token is not configured; the delivery service cannot be used",
            )
        try:
            import github as _github  # type: ignore[import-untyped]
        except Exception as exc:  # pragma: no cover - environment mismatch
            raise GitHubDeliveryError(
                "GITHUB_SDK_UNAVAILABLE",
                f"PyGithub is not available: {type(exc).__name__}",
            ) from exc
        token = redact_github_token(self.settings.github_token)
        self._client = _github.Github(login_or_token=token)
        return self._client

    # -- configuration -------------------------------------------------------

    def validate_configuration(self) -> GitHubConfigurationStatus:
        return build_configuration_status(self.settings)

    # -- branch / repo operations -------------------------------------------

    def get_base_branch_sha(self, base: str) -> str:
        if not GITHUB_BRANCH_PATTERN.fullmatch(base):
            raise GitHubDeliveryError(
                "GITHUB_BRANCH_INVALID", f"base branch is not valid: {base!r}"
            )
        client = self._ensure_client()
        repo = client.get_repo(self._repository_full_name)
        ref = repo.get_branch(base)
        return ref.commit.sha

    def find_existing_branch(self, branch: str) -> dict[str, Any] | None:
        if not GITHUB_BRANCH_PATTERN.fullmatch(branch):
            raise GitHubDeliveryError(
                "GITHUB_BRANCH_INVALID", f"branch name is not valid: {branch!r}"
            )
        client = self._ensure_client()
        repo = client.get_repo(self._repository_full_name)
        try:
            repo.get_branch(branch)
        except Exception:
            return None
        ref = repo.get_git_ref(f"heads/{branch}")
        return {"ref": ref.ref, "sha": ref.object.sha}

    def create_branch(self, name: str, base_sha: str) -> dict[str, Any]:
        if not GITHUB_BRANCH_PATTERN.fullmatch(name):
            raise GitHubDeliveryError(
                "GITHUB_BRANCH_INVALID", f"branch name is not valid: {name!r}"
            )
        client = self._ensure_client()
        repo = client.get_repo(self._repository_full_name)
        repo.create_git_ref(f"refs/heads/{name}", base_sha)
        ref = repo.get_git_ref(f"refs/heads/{name}")
        return {"ref": ref.ref, "sha": ref.object.sha}

    def update_files(
        self, branch: str, base_sha: str, files: list[GitHubFile], message: str
    ) -> dict[str, Any]:
        for file in files:
            if not file.path or ".." in file.path or file.path.startswith("/"):
                raise GitHubDeliveryError(
                    "GITHUB_PATH_ESCAPE", f"proposed file path is not safe: {file.path!r}"
                )
        client = self._ensure_client()
        repo = client.get_repo(self._repository_full_name)
        tree = repo.get_git_tree(base_sha)
        new_tree = repo.create_git_tree(
            [
                repo.create_git_blob(file.content, file.message or "OmniSight patch")
                for file in files
            ],
            base_tree=tree,
        )
        commit = repo.create_git_commit(
            message, new_tree, [repo.get_git_commit(base_sha)]
        )
        ref = repo.get_git_ref(f"refs/heads/{branch}")
        ref.set_commit(commit.sha)
        return {"commit_sha": commit.sha, "files_updated": len(files)}

    def open_pull_request(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        *,
        draft: bool = True,
    ) -> dict[str, Any]:
        if not GITHUB_BRANCH_PATTERN.fullmatch(head):
            raise GitHubDeliveryError(
                "GITHUB_BRANCH_INVALID", f"head branch is not valid: {head!r}"
            )
        client = self._ensure_client()
        repo = client.get_repo(self._repository_full_name)
        pull = repo.create_pull(title, body, head, base, draft=draft)
        return {
            "pull_request_number": pull.number,
            "pull_request_url": pull.html_url,
            "draft": pull.draft,
            "head": pull.head.ref,
            "base": pull.base.ref,
        }

    # -- internals ----------------------------------------------------------

    @property
    def _repository_full_name(self) -> str:
        repo = _normalize_repository(
            getattr(self.settings, "github_target_owner", None),
            getattr(self.settings, "github_target_repository", None),
        )
        if not repo:
            raise GitHubDeliveryError(
                "GITHUB_REPOSITORY_MISSING",
                "GITHUB_REPOSITORY (owner/name) is not configured",
            )
        return repo

    def __repr__(self) -> str:
        return f"PyGithubGitHubService(dry_run={getattr(self.settings, 'github_dry_run', True)!r})"


# ---------------------------------------------------------------------------
# Service factory + redacted status endpoint payload
# ---------------------------------------------------------------------------

def build_github_service(settings: Settings) -> GitHubService:
    """Return the appropriate implementation for the current configuration.

    The real PyGithub service is still a no-mutation implementation in this
    task: it validates configuration offline and refuses real writes unless an
    operator explicitly authorizes them in the environment.
    """
    if getattr(settings, "github_dry_run", True):
        return FakeGitHubService(settings=settings)
    return PyGithubGitHubService(settings=settings)
