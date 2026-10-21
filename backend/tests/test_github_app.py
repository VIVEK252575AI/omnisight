"""GitHub App JWT, least-privilege token, and repository verification tests."""

from __future__ import annotations

import json

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
