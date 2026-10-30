"""Delivery service tests: configuration status, redaction, fake, PyGithub lazy client.

Tests are meant to run without network access. The real PyGithub path is only
exercised through unittest.mock so it never reaches GitHub.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.github_delivery import (
    GitHubConfigurationStatus,
    GitHubDeliveryError,
    GitHubFile,
    GitHubService,
    PyGithubGitHubService,
    FakeGitHubService,
    build_github_service,
    build_configuration_status,
    github_status_response,
    redact_github_token,
    _safe_dict,
    _token_present_in_settings,
    GITHUB_BRANCH_PATTERN,
    GITHUB_REPO_PATTERN,
)
from app.config import Settings


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

class _Sentinel(str):
    pass


TOKEN_ABC = "ghp_8b3c7d92f1e4a2b5c6d8e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"


def test_redaction_replaces_token_like_strings():
    sentinel = _Sentinel("ghp_8b3c7d92f1e4a2b5c6d8e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b")
    value = "call with " + sentinel + " now"
    result = redact_github_token(value)
    assert _Sentinel("REDACTED_GITHUB_TOKEN") not in result
    assert sentinel not in result
    assert "REDACTED_GITHUB_TOKEN" in result
    assert TOKEN_ABC not in result

    # The utility must still remove a token standing alone.
    standalone = redact_github_token(sentinel)
    assert sentinel not in standalone
    assert "REDACTED_GITHUB_TOKEN" in standalone


def test_redaction_replaces_token_in_middle_of_line():
    secret = TOKEN_ABC
    stripped = redact_github_token("x" + secret + "x")
    assert stripped.startswith("x")
    assert secret not in stripped
    assert "REDACTED_GITHUB_TOKEN" in stripped


def test_redaction_replaces_token_with_trailing_prefix_similarity():
    secret = TOKEN_ABC
    combined = secret + "ghp_x"
    stripped = redact_github_token(combined)
    assert secret not in stripped
    assert stripped != combined
    assert "REDACTED_GITHUB_TOKEN" in stripped


def test_redaction_keeps_short_strings_intact():
    assert redact_github_token("short") == "short"
    assert redact_github_token("") == ""
    # The utility deliberately does not touch very short tokens because they
    # are not token-shaped; the surrounding code must not pass secrets here.
    assert redact_github_token("x" * 9) == "x" * 9
    assert redact_github_token("ghp_" + "x" * 9) == redact_github_token("ghp_" + "x" * 9)


def test_redaction_keeps_short_strings_intact():
    assert redact_github_token("short") == "short"
    assert redact_github_token("") == ""
    # The utility deliberately does not touch very short tokens because they
    # are not token-shaped; the surrounding code must not pass secrets here.
    assert redact_github_token("x" * 9) == "x" * 9


def test_redaction_does_not_leak_prefix():
    token = "ghp_" + "a" * 36
    assert token not in redact_github_token(token)
    assert token[:8] not in redact_github_token(token)


def test_safe_dict_redacts_nested_token():
    token = "ghp_" + "b" * 30
    bearer_token = "Bearer " + token
    payload = {
        "headers": {"Authorization": bearer_token},
        "body": ["keep me", token],
    }
    safe = _safe_dict(payload)
    assert bearer_token not in str(safe)
    assert token not in str(safe)
    assert "REDACTED_GITHUB_TOKEN" in str(safe)


# ---------------------------------------------------------------------------
# Configuration status
# ---------------------------------------------------------------------------

def test_status_without_credentials_is_not_configured():
    settings = Settings()
    status = build_configuration_status(settings)
    assert status.configured is False
    assert status.dry_run is True
    assert status.repository is None
    assert status.base_branch is None
    assert "GITHUB_TOKEN" in status.missing_keys
    assert "GITHUB_REPOSITORY" in status.missing_keys


def test_status_with_partial_credentials_is_partial():
    token = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    settings = Settings(
        github_token=token,
        github_target_owner="acme",
        github_target_repository="omnisight-demo",
    )
    status = build_configuration_status(settings)
    assert status.configured is False
    assert status.dry_run is True
    assert status.repository == "acme/omnisight-demo"
    assert status.base_branch is None
    assert "GITHUB_BASE_BRANCH" in status.missing_keys
    assert status.missing_keys == ["GITHUB_BASE_BRANCH"]


def test_token_present_in_settings_is_true_for_genuine_tokens():
    assert _token_present_in_settings("ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    assert _token_present_in_settings("ghp_8b3c7d92f1e4a2b5c6d8e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b")
    assert not _token_present_in_settings("")
    assert not _token_present_in_settings("short")
    assert not _token_present_in_settings("ghp_abc")
    assert not _token_present_in_settings("REDACTED_GITHUB_TOKEN")
    assert not _token_present_in_settings("b'ghp_abc'")


def test_token_present_in_settings_is_false_for_non_token_strings():
    assert not _token_present_in_settings("not a token")
    assert not _token_present_in_settings("github_pat_abcde")
    assert not _token_present_in_settings("ghp_ghp_")
    assert "GITHUB_TOKEN" not in status.missing_keys


def test_status_with_full_credentials_is_configured():
    settings = Settings(
        github_token="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        github_target_owner="acme",
        github_target_repository="omnisight-demo",
        github_target_branch="main",
    )
    status = build_configuration_status(settings)
    assert status.configured is True
    assert status.missing_keys == []
    assert status.dry_run is True
    assert status.repository == "acme/omnisight-demo"
    assert status.base_branch == "main"
    assert status.missing_keys == []


def test_status_endpoint_payload_has_no_token():
    settings = Settings(
        github_token="ghp_super_secret_value_never_leave_backend",
        github_target_owner="acme",
        github_target_repository="omnisight-demo",
        github_target_branch="main",
    )
    status = build_configuration_status(settings)
    assert status.configured is True
    assert status.missing_keys == []
    payload = github_status_response(status)
    assert payload["configured"] is True
    assert payload["missing_keys"] == []
    assert payload["dry_run"] is True
    assert payload["repository"] == "acme/omnisight-demo"
    assert payload["base_branch"] == "main"
    serialized = str(payload)
    assert "ghp_super_secret_value_never_leave_backend" not in serialized
    assert "super_secret" not in serialized


def test_dry_run_defaults_to_true():
    settings = Settings(
        github_token="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        github_target_owner="acme",
        github_target_repository="omnisight-demo",
        github_target_branch="main",
    )
    assert build_configuration_status(settings).dry_run is True


# ---------------------------------------------------------------------------
# Fake service
# ---------------------------------------------------------------------------

class TestFakeGitHubService:
    """Fake is usable even when PyGithub is not installed (if applicable)."""

    def test_fake_records_calls_and_returns_deterministic_branches(self):
        fake = FakeGitHubService()
        status = fake.validate_configuration()
        assert status.dry_run is True
        assert fake.find_existing_branch("feature/x") is None
        created = fake.create_branch("feature-omnisight-fix", "base-sha")
        assert created["ref"] == "refs/heads/feature-omnisight-fix"
        assert fake.find_existing_branch("feature-omnisight-fix")["sha"] == created["sha"]
        assert len(fake.calls) == 3

    def test_fake_rejects_unsafe_paths(self):
        fake = FakeGitHubService()
        with pytest.raises(GitHubDeliveryError) as exc:
            fake.update_files("main", "sha", [GitHubFile(path="../escape", content="x")], "msg")
        assert exc.value.code == "GITHUB_PATH_ESCAPE"

    def test_fake_rejects_invalid_branch_names(self):
        fake = FakeGitHubService()
        with pytest.raises(GitHubDeliveryError) as exc:
            fake.create_branch("bad branch name", "sha")
        assert exc.value.code == "GITHUB_BRANCH_INVALID"

    def test_fake_pr_is_a_draft_by_default(self):
        fake = FakeGitHubService()
        pr = fake.open_pull_request("title", "body", "feature/x", "main")
        assert pr["draft"] is True


# ---------------------------------------------------------------------------
# Lazy PyGithub service
# ---------------------------------------------------------------------------

class TestPyGithubService:
    """PyGithub path is only exercised through mocks so it never reaches GitHub."""

    @pytest.fixture()
    def configured_settings(self):
        return Settings(
            github_token="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            github_target_owner="acme",
            github_target_repository="omnisight-demo",
            github_target_branch="main",
            github_dry_run=False,
        )

    def test_lazy_client_creates_github_on_first_use(self, configured_settings):
        cfg = configured_settings.model_copy(update={"github_token": "ghp_test_token_for_spy_check"})
        svc = PyGithubGitHubService(settings=cfg)
        with patch("app.github_delivery.github", create=True) as gh_mod:
            client = MagicMock()
            gh_mod.Github = MagicMock(return_value=client)
            svc._ensure_client()
            gh_mod.Github.assert_called_once_with(login_or_token="REDACTED_GITHUB_TOKEN")

    def test_lazy_client_redacts_token_in_creation(self, configured_settings):
        cfg = configured_settings.model_copy(update={"github_token": "ghp_test_token_for_spy_check"})
        svc = PyGithubGitHubService(settings=cfg)
        call_kwargs = {}

        def spy(*args, **kwargs):
            nonlocal call_kwargs
            call_kwargs = kwargs
            return MagicMock()

        with patch("app.github_delivery.github", create=True) as gh_mod:
            gh_mod.Github = MagicMock(side_effect=spy)
            svc._ensure_client()
        assert call_kwargs.get("login_or_token") == "REDACTED_GITHUB_TOKEN"

    def test_lazy_client_raises_when_token_missing(self):
        svc = PyGithubGitHubService(settings=Settings())
        with pytest.raises(GitHubDeliveryError) as exc:
            svc._ensure_client()
        assert exc.value.code == "GITHUB_TOKEN_MISSING"

    def test_validation_status_is_unchanged_by_real_client(self, configured_settings):
        # validate_configuration must never create the client.
        svc = PyGithubGitHubService(settings=configured_settings)
        status = svc.validate_configuration()
        assert status.configured is True
        assert status.dry_run is False
        assert svc._client is None

    def test_get_base_branch_sha_uses_repo_and_branch(self, configured_settings):
        cfg = configured_settings.model_copy(update={"github_token": "ghp_test_token_for_spy_check"})
        svc = PyGithubGitHubService(settings=cfg)
        repo_mock = MagicMock()
        branch_mock = MagicMock()
        branch_mock.commit.sha = "abc123"
        repo_mock.get_branch.return_value = branch_mock
        client_mock = MagicMock()
        client_mock.get_repo.return_value = repo_mock

        with patch("app.github_delivery.github", create=True) as gh_mod:
            gh_mod.Github = MagicMock(return_value=client_mock)
            sha = svc.get_base_branch_sha("main")

        client_mock.get_repo.assert_called_once_with("acme/omnisight-demo")
        repo_mock.get_branch.assert_called_once_with("main")
        assert sha == "abc123"

    def test_create_branch_refuses_bad_name(self, configured_settings):
        svc = PyGithubGitHubService(settings=configured_settings)
        with pytest.raises(GitHubDeliveryError) as exc:
            svc.create_branch("bad name", "sha")
        assert exc.value.code == "GITHUB_BRANCH_INVALID"

    def test_update_files_refuses_path_escape(self, configured_settings):
        cfg = configured_settings.model_copy(update={"github_token": "ghp_test_token_for_path_check"})
        svc = PyGithubGitHubService(settings=cfg)
        with pytest.raises(GitHubDeliveryError) as exc:
            svc.update_files("main", "sha", [GitHubFile(path="../escape", content="x")], "msg")
        assert exc.value.code == "GITHUB_PATH_ESCAPE"


# ---------------------------------------------------------------------------
# Factory / interface compatibility
# ---------------------------------------------------------------------------

def test_fake_implements_the_service_protocol():
    # Static check that the fake satisfies the public interface used by mocks.
    fake = FakeGitHubService()
    assert isinstance(fake, GitHubService)


def test_build_service_returns_fake_in_dry_run_mode():
    settings = Settings(
        github_token="ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        github_target_owner="acme",
        github_target_repository="omnisight-demo",
        github_target_branch="main",
        github_dry_run=True,
    )
    svc = build_github_service(settings)
    assert isinstance(svc, FakeGitHubService)
    assert svc.validate_configuration().dry_run is True


# ---------------------------------------------------------------------------
# Schema / validation regex sanity
# ---------------------------------------------------------------------------

def test_repository_name_pattern():
    assert GITHUB_REPO_PATTERN.fullmatch("acme/omnisight-demo")
    assert not GITHUB_REPO_PATTERN.fullmatch("acme/omnisight demo")
    assert not GITHUB_REPO_PATTERN.fullmatch("a/b/c")


def test_branch_pattern_allows_simple_and_slash_names():
    assert GITHUB_BRANCH_PATTERN.fullmatch("main")
    assert GITHUB_BRANCH_PATTERN.fullmatch("feature/omnisight-fix")
    assert not GITHUB_BRANCH_PATTERN.fullmatch("../escape")
    assert not GITHUB_BRANCH_PATTERN.fullmatch("/root")
    assert not GITHUB_BRANCH_PATTERN.fullmatch("./backdoor")
    assert not GITHUB_BRANCH_PATTERN.fullmatch("folder/../../escape")
    assert not GITHUB_BRANCH_PATTERN.fullmatch("a//b")
    assert not GITHUB_BRANCH_PATTERN.fullmatch("//double")
