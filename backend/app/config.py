"""Application configuration, loaded from environment variables.

Secrets (API keys, tokens, webhook shared secrets) are read from env only and
must never be logged or included in API responses.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Backend configuration via environment variables (12-factor style)."""

    model_config = SettingsConfigDict(
        env_prefix="OMNISIGHT_",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "OmniSight Backend"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: str = "INFO"

    # CORS: explicit allow-list; no wildcard in production.
    # Parsed from a comma-separated env value, e.g.
    #   OMNISIGHT_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5174
    allowed_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    # CI webhook hardening.
    # Max accepted request body size (bytes); oversized bodies get a 413.
    webhook_max_body_bytes: int = 65536
    # Optional host allow-list for `target_url` on successful builds. Empty
    # (the default) is permissive — appropriate for local simulation only.
    # Parsed from a comma-separated env value.
    target_host_allowlist: Annotated[list[str], NoDecode] = []

    @field_validator("target_host_allowlist", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
        return value

    # Scan execution limits (deterministic controls, enforced by orchestrator later).
    max_scan_url_length: int = 2048
    scan_create_rate_limit_per_minute: int = 60

    # Feature flags for integrations that do not exist yet.
    # These stay False until the corresponding module is really implemented,
    # so the API never advertises a capability it does not have.
    enable_playwright_worker: bool = False
    enable_vlm_detection: bool = False
    enable_patch_engine: bool = False
    enable_github_delivery: bool = False

    # Local directories (configurable; created on demand by later modules).
    artifacts_dir: Path = Path("artifacts")
    data_dir: Path = Path("data")

    # Optional shared secret for CI/CD webhook verification. Never logged.
    ci_webhook_secret: str | None = None

    # Evidence / artifact handling (Week 1 evidence contract).
    # Max accepted observation JSON body size (bytes).
    observation_max_bytes: int = 1_048_576
    # Max size of a referenced image / json artifact on disk (bytes).
    artifact_max_image_bytes: int = 5_242_880
    artifact_max_json_bytes: int = 1_048_576


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance (dependency-injectable)."""
    return Settings()
