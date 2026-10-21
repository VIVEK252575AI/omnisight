"""Application configuration, loaded from environment variables.

Secrets (API keys, tokens, webhook shared secrets) are read from env only and
must never be logged or included in API responses.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
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

    # VLM adapter boundary (Week 2) — provider-neutral, config-selected.
    # `vlm_adapter` selects the replaceable implementation: "mock" (default,
    # deterministic offline), "gemini" (server-side multimodal API), or
    # "teammate" (wraps the teammate VLM module once its callable is injected).
    # An unknown value fails fast at startup. Credentials and model selection
    # stay configurable and server-side.
    vlm_adapter: str = "mock"
    vlm_mock_scenario: str = "clipped_button"
    vlm_timeout_seconds: float = 30.0
    vlm_max_image_bytes: int = 5_242_880
    vlm_max_images_per_analysis: int = 6

    # AI Gateway (validation layer over the adapter).
    ai_max_repair_attempts: int = 1
    ai_max_output_tokens: int = 4096
    ai_max_findings: int = 20
    # Whether raw provider output may ever be persisted to disk. Off by
    # default; raw output is untrusted and must not be written out.
    ai_allow_raw_output_storage: bool = False

    # VLM detection workflow (W2.4). Bounded so a single detection can never
    # flood the adapter or pin memory:
    # - `detection_max_candidates` caps the DOM candidate selectors sent as
    #   context, keeping only the most relevant elements first.
    # - `detection_max_raw_output_bytes` caps the raw model text retained
    #   IN MEMORY for audit (it is never written to disk and never returned by
    #   an API unless `detection_expose_raw_output` is enabled).
    # - `detection_expose_raw_output` is the debug flag that opts raw model
    #   text into API responses; off by default.
    detection_max_candidates: int = 50
    detection_max_raw_output_bytes: int = 8192
    detection_expose_raw_output: bool = False

    # Safe action engine (W2.5). A model proposal is ONLY ever turned into a
    # stored record here — nothing is written to disk in this slice.
    #
    # `patch_generator` selects the replaceable generator ("mock" default);
    # `patch_mock_scenario` picks the deterministic offline behaviour. No
    # provider, model name or key is hardcoded anywhere.
    #
    # `approved_patch_paths` is the path allow-list (glob patterns matched with
    # fnmatch semantics against the normalised, repo-relative path). Its
    # DEFAULT IS EMPTY: with no approved paths every proposal is rejected.
    # It becomes populated by the ShopLite owner's handoff.
    patch_generator: str = "mock"
    patch_mock_scenario: str = "ok"
    patch_source_root: str = ""
    approved_patch_paths: Annotated[list[str], NoDecode] = []
    patch_allowed_extensions: Annotated[list[str], NoDecode] = [
        ".css",
        ".scss",
        ".jsx",
        ".tsx",
    ]
    max_patch_files: int = 5
    max_patch_bytes: int = 4096
    patch_context_max_bytes: int = 4096
    patch_timeout_seconds: float = 30.0
    # Cap on the raw generator/patch text handed to the extractor. Enforced
    # BEFORE parsing so a giant response cannot pin memory or drive a
    # pathological regex; an oversized response is rejected ``too_large``.
    patch_max_response_bytes: int = 16384

    @field_validator("approved_patch_paths", mode="before")
    @classmethod
    def _split_approved_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("patch_allowed_extensions", mode="before")
    @classmethod
    def _split_patch_extensions(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

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

    # Gemini-backed, review-only patch suggestions. The key stays server-side.
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.6-flash"
    gemini_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    gemini_max_output_tokens: int = Field(default=4096, ge=128, le=8192)


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance (dependency-injectable)."""
    return Settings()
