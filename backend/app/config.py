"""Application configuration, loaded from environment variables.

Secrets (API keys, tokens) are read from env only and must never be logged
or included in API responses.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # CORS: explicit allow-list; no wildcard in production.
    allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

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

    # Where artifacts (screenshots, observations) will be stored later.
    artifacts_dir: Path = Path("artifacts")


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance (dependency-injectable)."""
    return Settings()
