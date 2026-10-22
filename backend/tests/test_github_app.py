"""GitHub App JWT, least-privilege token, and repository verification tests."""

from __future__ import annotations

import json
import base64

import anyio
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.github_app import (
    GitHubAppClient,
    GitHubIntegrationFailure,
    _AppCredentials,
    create_app_jwt,
)
from app.patch_schemas import PatchEdit, PatchFile, PatchProposal


@pytest.fixture
def app_settings() -> Settings:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return Settings(
        enable_github_app=True,
        github_app_id="123456",
        github_app_installation_id="987654",
        github_app_private_key=pem,
    )


def test_github_app_jwt_is_short_lived_rs256_with_clock_drift(app_settings):
    creds = _AppCredentials(
        app_id=app_settings.github_app_id,
        installation_id=app_settings.github_app_installation_id,
        private_key=app_settings.github_app_private_key,
    )
    token = create_app_jwt(creds, now=1_800_000_000)
    public_key = serialization.load_pem_private_key(
        app_settings.github_app_private_key.get_secret_value().encode(), password=None
    ).public_key()
    claims = jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        options={"verify_iat": False, "verify_exp": False},
    )

    assert claims == {"iat": 1_799_999_940, "exp": 1_800_000_540, "iss": "123456"}


def test_repository_verification_scopes_installation_token_and_reads_base_sha(app_settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/app/installations/987654/access_tokens":
            claims = jwt.decode(
                request.headers["Authorization"].removeprefix("Bearer "),
                options={"verify_signature": False},
                algorithms=["RS256"],
            )
            assert claims["iss"] == "123456"
            assert json.loads(request.content) == {
                "repositories": ["omnisight"],
                "permissions": {"contents": "read"},
            }
            return httpx.Response(
                201,
                json={
                    "token": "installation-secret",
                    "repositories": [{"id": 42, "full_name": "VIVEK252575AI/omnisight"}],
                    "permissions": {"contents": "read", "metadata": "read"},
                },
            )
        assert request.headers["Authorization"] == "Bearer installation-secret"
        if request.url.path == "/repos/VIVEK252575AI/omnisight":
            return httpx.Response(200, json={"id": 42, "full_name": "VIVEK252575AI/omnisight", "default_branch": "main"})
        if request.url.path == "/repos/VIVEK252575AI/omnisight/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "a" * 40}})
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ) as http_client:
            return await GitHubAppClient(app_settings, http_client=http_client).verify_repository(
                "VIVEK252575AI", "omnisight"
            )

    result = anyio.run(run)

    assert result.repository_id == 42
    assert result.base_branch == "main"
    assert result.base_sha == "a" * 40
    assert len(calls) == 3
    assert "installation-secret" not in result.model_dump_json()


def test_repository_verification_uses_explicit_base_branch(app_settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.raw_path.decode())
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "token": "installation-secret",
                    "repositories": [{"id": 42, "full_name": "VIVEK252575AI/omnisight"}],
                    "permissions": {"contents": "read", "metadata": "read"},
                },
            )
        if request.url.path == "/repos/VIVEK252575AI/omnisight":
            return httpx.Response(200, json={"id": 42, "full_name": "VIVEK252575AI/omnisight", "default_branch": "main"})
        return httpx.Response(200, json={"object": {"sha": "b" * 40}})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ) as http_client:
            return await GitHubAppClient(app_settings, http_client=http_client).verify_repository(
                "VIVEK252575AI", "omnisight", base_branch="feature/qa"
            )

    result = anyio.run(run)

    assert result.base_branch == "feature/qa"
    assert calls[-1].endswith("git/ref/heads/feature%2Fqa")


def test_repository_verification_rejects_bad_identifiers_before_network(app_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid repository identifiers must not call GitHub")

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ) as http_client:
            client = GitHubAppClient(app_settings, http_client=http_client)
            with pytest.raises(GitHubIntegrationFailure) as failure:
                await client.verify_repository("owner/path", "repo")
            return failure.value

    failure = anyio.run(run)

    assert failure.code == "GITHUB_REPOSITORY_INVALID"


def test_repository_verification_sanitizes_github_permission_error(app_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "token": "installation-secret",
                    "repositories": [{"id": 42, "full_name": "VIVEK252575AI/omnisight"}],
                    "permissions": {"contents": "read", "metadata": "read"},
                },
            )
        return httpx.Response(403, json={"message": "private provider payload"})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ) as http_client:
            with pytest.raises(GitHubIntegrationFailure, match="lacks required repository access") as failure:
                await GitHubAppClient(app_settings, http_client=http_client).verify_repository(
                    "VIVEK252575AI", "omnisight"
                )
            return failure.value

    failure = anyio.run(run)

    assert failure.code == "GITHUB_PERMISSION_DENIED"
    assert "private provider payload" not in str(failure)


def test_github_app_not_configured_fails_closed():
    settings = Settings(enable_github_app=True)
    async def run():
        async with httpx.AsyncClient(base_url="https://api.github.com") as http_client:
            with pytest.raises(GitHubIntegrationFailure) as failure:
                await GitHubAppClient(settings, http_client=http_client).verify_repository("owner", "repo")
            return failure.value

    failure = anyio.run(run)

    assert failure.code == "GITHUB_APP_NOT_CONFIGURED"


def test_draft_publisher_uses_git_database_and_reconciles_retries(app_settings):
    base_sha = "a" * 40
    calls = []
    state = {"branch_sha": None, "pull_request": None}
    original = "#submit { width: 520px; }"
    candidate_tree = "e" * 40
    candidate_commit = "c" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/app/installations/987654/access_tokens":
            assert json.loads(request.content) == {
                "repositories": ["omnisight"],
                "permissions": {"contents": "write", "pull_requests": "write"},
            }
            return httpx.Response(201, json={
                "token": "installation-secret",
                "repositories": [{"id": 42, "full_name": "VIVEK252575AI/omnisight"}],
                "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
            })
        assert request.headers.get("Authorization") == "Bearer installation-secret"
        if path == "/repos/VIVEK252575AI/omnisight":
            return httpx.Response(200, json={"id": 42, "full_name": "VIVEK252575AI/omnisight"})
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": base_sha}})
        if "/git/ref/heads/omnisight" in path:
            if state["branch_sha"]:
                return httpx.Response(200, json={"object": {"sha": state["branch_sha"]}})
            return httpx.Response(404, json={"message": "not found"})
        if path.endswith("/contents/demo-app/styles/checkout.css"):
            assert request.url.params["ref"] == base_sha
            encoded = base64.b64encode(original.encode()).decode()
            return httpx.Response(200, json={
                "type": "file", "encoding": "base64", "content": encoded[:8] + "\n" + encoded[8:]
            })
        if path.endswith(f"/git/commits/{base_sha}"):
            return httpx.Response(200, json={"tree": {"sha": "d" * 40}})
        if path == "/repos/VIVEK252575AI/omnisight/git/trees":
            payload = json.loads(request.content)
            assert payload["tree"][0]["path"] == "demo-app/styles/checkout.css"
            assert payload["tree"][0]["content"] == original.replace("520px", "100%")
            assert payload["tree"][0]["type"] == "blob"
            return httpx.Response(201, json={"sha": candidate_tree})
        if path == "/repos/VIVEK252575AI/omnisight/git/commits":
            payload = json.loads(request.content)
            assert payload["tree"] == candidate_tree
            assert payload["parents"] == [base_sha]
            assert "OmniSight-Diff-SHA256:" in payload["message"]
            return httpx.Response(201, json={"sha": candidate_commit})
        if path == "/repos/VIVEK252575AI/omnisight/git/refs":
            payload = json.loads(request.content)
            assert payload["ref"].startswith("refs/heads/omnisight/fix/")
            state["branch_sha"] = candidate_commit
            return httpx.Response(201, json={"object": {"sha": candidate_commit}})
        if path.endswith(f"/git/commits/{candidate_commit}"):
            return httpx.Response(
                200, json={"tree": {"sha": candidate_tree}, "parents": [{"sha": base_sha}]}
            )
        if path == "/repos/VIVEK252575AI/omnisight/pulls" and request.method == "GET":
            return httpx.Response(200, json=[state["pull_request"]] if state["pull_request"] else [])
        if path == "/repos/VIVEK252575AI/omnisight/pulls" and request.method == "POST":
            payload = json.loads(request.content)
            assert payload["draft"] is True
            assert payload["base"] == "main"
            assert payload["head"].startswith("omnisight/fix/")
            state["pull_request"] = {
                "number": 123,
                "html_url": "https://github.com/VIVEK252575AI/omnisight/pull/123",
                "draft": True,
                "head": {"ref": payload["head"], "sha": candidate_commit},
                "base": {"ref": payload["base"], "sha": base_sha},
            }
            return httpx.Response(201, json=state["pull_request"])
        pytest.fail(f"unexpected GitHub request: {request.method} {request.url}")

    settings = app_settings.model_copy(update={"approved_patch_paths": ["styles/checkout.css"]})
    proposal = PatchProposal(
        finding_id="finding-1",
        files=[PatchFile(path="styles/checkout.css", edits=[PatchEdit(old_text="520px", new_text="100%")])],
        rationale="Keep checkout within the viewport.",
        generator_label="gemini",
    )

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ) as http_client:
            publisher = GitHubAppClient(settings, http_client=http_client)
            first = await publisher.publish_shoplite_draft_pr(
                "VIVEK252575AI", "omnisight", repository_id=42, base_branch="main",
                expected_base_sha=base_sha, proposal_id="proposal-1", proposal=proposal, confirmed=True,
            )
            second = await publisher.publish_shoplite_draft_pr(
                "VIVEK252575AI", "omnisight", repository_id=42, base_branch="main",
                expected_base_sha=base_sha, proposal_id="proposal-1", proposal=proposal, confirmed=True,
            )
            return first, second

    first, second = anyio.run(run)

    assert first.draft is True
    assert first.pull_request_number == 123
    assert first.commit_sha == candidate_commit
    assert first.diff_sha256 == second.diff_sha256
    assert second.pull_request_url == first.pull_request_url
    assert sum(call.method == "POST" and call.url.path.endswith("/pulls") for call in calls) == 1
    assert all(not (call.method == "PATCH" and "/git/refs/heads/main" in call.url.path) for call in calls)


def test_draft_publisher_requires_confirmation_before_network(app_settings):
    settings = app_settings.model_copy(update={"approved_patch_paths": ["styles/checkout.css"]})
    proposal = PatchProposal(
        finding_id="finding-1",
        files=[PatchFile(path="styles/checkout.css", edits=[PatchEdit(old_text="x", new_text="y")])],
        rationale="Test.", generator_label="gemini",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("unconfirmed draft creation must not call GitHub")

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ) as http_client:
            with pytest.raises(GitHubIntegrationFailure) as failure:
                await GitHubAppClient(settings, http_client=http_client).publish_shoplite_draft_pr(
                    "VIVEK252575AI", "omnisight", repository_id=42, base_branch="main",
                    expected_base_sha="a" * 40, proposal_id="proposal-1", proposal=proposal, confirmed=False,
                )
            return failure.value

    assert anyio.run(run).code == "GITHUB_CONFIRMATION_REQUIRED"


def test_draft_publisher_aborts_when_base_sha_moved(app_settings):
    settings = app_settings.model_copy(update={"approved_patch_paths": ["styles/checkout.css"]})
    proposal = PatchProposal(
        finding_id="finding-1",
        files=[PatchFile(path="styles/checkout.css", edits=[PatchEdit(old_text="x", new_text="y")])],
        rationale="Test.", generator_label="gemini",
    )
    writes = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={
                "token": "token", "repositories": [{"id": 42, "full_name": "VIVEK252575AI/omnisight"}],
                "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
            })
        if request.url.path == "/repos/VIVEK252575AI/omnisight":
            return httpx.Response(200, json={"id": 42})
        if "/git/ref/heads/main" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": "b" * 40}})
        if request.method == "POST":
            writes.append(request)
        pytest.fail(f"unexpected request after stale base: {request.method} {request.url}")

    async def run():
        async with httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        ) as http_client:
            with pytest.raises(GitHubIntegrationFailure) as failure:
                await GitHubAppClient(settings, http_client=http_client).publish_shoplite_draft_pr(
                    "VIVEK252575AI", "omnisight", repository_id=42, base_branch="main",
                    expected_base_sha="a" * 40, proposal_id="proposal-1", proposal=proposal, confirmed=True,
                )
            return failure.value

    failure = anyio.run(run)
    assert failure.code == "GITHUB_BASE_REVISION_CHANGED"
    assert writes == []
