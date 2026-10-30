"""Application configuration, loaded from environment variables.

Secrets (API keys, tokens, webhook shared secrets) are read from env only and
must never be logged or included in API responses.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger("omnisight")

#: Hard ceiling on repair attempts per finding. Enforced in code, not just in
#: documentation: retrying is bounded, so a bad environment value can never turn
#: the self-healing loop unbounded.
MAX_REPAIR_ATTEMPTS_CEILING = 5


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
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
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
    enable_github_app: bool = False
    enable_github_publish_api: bool = False
    auth_mode: Literal["disabled", "local_single_user"] = "disabled"
    local_auth_token: SecretStr | None = None
    local_project_id: str = "b13a29c2-4669-5ac7-9ba6-ff7c1d32ae11"
    local_project_name: str = "OmniSight ShopLite Demo"
    database_url: SecretStr | None = None

    # GitHub App installation credentials (server-side only). These values
    # configure the local integration worker; they are never returned by APIs.
    github_app_id: str | None = None
    github_app_installation_id: str | None = None
    github_app_private_key: SecretStr | None = None
    github_target_owner: str | None = None
    github_target_repository: str | None = None
    github_target_branch: str | None = None

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

    # Safe action engine (W2.5). A model proposal is only accepted as a
    # validated, reviewable record. Validated edits are stored in PostgreSQL;
    # raw model output is never persisted.
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
    # Snapshot/verification lifecycle (W4 evidence). Whether an exported scan
    # snapshot should also be persisted to the canonical run store is the one
    # behaviour tuned here; other persistence paths are left untouched.
    store_exported_should_persist: bool = True

    @field_validator("store_exported_should_persist", mode="before")
    @classmethod
    def _parse_store_exported_should_persist(cls, value: object) -> object:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            return text in {"1", "true", "yes"}
        return bool(value)

    # Opt-in, fixed-target ShopLite sandbox. The approval token is required
    # before the API may start a Docker run; the sandbox itself has no network.
    enable_shoplite_sandbox: bool = False
    shoplite_sandbox_token: SecretStr | None = None
    shoplite_sandbox_timeout_seconds: float = Field(default=45.0, gt=0, le=120)

    # Self-healing orchestration (W3.1). Off by default: the patch applier,
    # validator and verifier components it drives do not exist yet, and
    # ``POST /scans/{id}/repair`` must refuse rather than fake progress.
    enable_repair_orchestrator: bool = False
    # Bounded attempts per finding (W3.4 retry loop). Values above
    # ``MAX_REPAIR_ATTEMPTS_CEILING`` are clamped with a warning at load time.
    max_repair_attempts: int = Field(default=3, ge=1)
    # Cap on the prior-attempt failure context handed to the next proposal.
    # Zero disables the feedback entirely. Bounded because it re-enters a model
    # prompt, and loop output must never grow without limit.
    repair_prior_failure_max_chars: int = Field(default=1000, ge=0, le=4000)

    @field_validator("max_repair_attempts", mode="before")
    @classmethod
    def clamp_max_repair_attempts(cls, value: object) -> object:
        """Clamp the retry budget to the hard ceiling instead of trusting it."""
        try:
            attempts = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return value
        if attempts > MAX_REPAIR_ATTEMPTS_CEILING:
            logger.warning(
                "OMNISIGHT_MAX_REPAIR_ATTEMPTS=%s exceeds the hard ceiling of %s; "
                "clamping to the ceiling",
                attempts,
                MAX_REPAIR_ATTEMPTS_CEILING,
            )
            return MAX_REPAIR_ATTEMPTS_CEILING
        return attempts

    # Cooperative watchdog for ONE background repair run: checked between
    # steps, so a runaway run is escalated instead of holding the scan in
    # ``repairing`` forever.
    repair_run_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)
    # A scan still in ``repairing`` after this long is assumed orphaned by a
    # process restart and is settled by the startup recovery sweep.
    repair_stuck_after_seconds: float = Field(default=900.0, gt=0, le=86_400)

    # Workspace isolation (W3.2). Patches are applied only in a copies tree;
    # the server's own source tree and the default branch are never touched.
    workspace_root: Path = Path("artifacts/workspaces")
    # A failed workspace is kept this long for debugging, then deleted.
    workspace_retention_hours: float = Field(default=24.0, gt=0, le=720)
    # Hard ceiling on a copied source tree (the ShopLite demo is ~4 KB).
    max_workspace_bytes: int = Field(default=5_242_880, ge=1_048_576)
    # A unified diff larger than this is refused, never silently truncated.
    max_diff_bytes: int = Field(default=16_384, ge=1024)

    # Build/test validation (W3.3). Argument lists only, never a shell string.
    # Empty by default: the ShopLite demo is static and has no build system, so
    # validation reports ``not_configured`` until the owner configures a check.
    validation_commands: Annotated[list[list[str]], NoDecode] = []
    # Extra environment variables for the child (still filtered for secrets).
    validation_env: Annotated[dict[str, str], NoDecode] = {}
    command_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    command_output_max_bytes: int = Field(default=8192, ge=256, le=131_072)

    # Serving the patched workspace for fresh capture. When unset, the built-in
    # loopback static server is used (correct for the static ShopLite demo).
    serve_command: Annotated[list[str], NoDecode] = []
    # Required with ``serve_command``: the loopback URL that command serves.
    serve_url: str = ""
    serve_ready_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    # Visual verification (W3.3): bounded VLM re-check of the ORIGINAL defect.
    vlm_verifier: str = "mock"
    verification_vlm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    verification_max_new_issues: int = Field(default=20, ge=1, le=100)
    # WCAG AA for normal text; used only when the collector captured colours.
    min_contrast_ratio: float = Field(default=4.5, gt=1.0, le=21.0)

    @field_validator("approved_patch_paths", mode="before")
    @classmethod
    def _split_approved_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("validation_commands", "serve_command", mode="before")
    @classmethod
    def _parse_command_list(cls, value: object) -> object:
        """Accept a JSON string from the environment or an explicit list.

        ``OMNISIGHT_VALIDATION_COMMANDS='[["node","run","build"]]'`` is the
        only accepted string form: a plain shell string is rejected because a
        command must never be split or interpolated.
        """
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            parsed = json.loads(text)
            return parsed
        return value

    @field_validator("validation_env", mode="before")
    @classmethod
    def _parse_env_map(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            return json.loads(text) if text else {}
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

    # Supabase Storage (optional; server-side service role key only).
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_storage_bucket: str = "scan-artifacts"

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

    # GitHub delivery service credentials (server-side only).
    github_token: str | None = None
    github_dry_run: bool = True


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance (dependency-injectable)."""
    return Settings()


def _store_exported_should_persist(settings: Settings = Depends(get_settings)) -> bool:
    """One-liner convenience for the snapshot->persisted-export shape toggle."""
    return settings.store_exported_should_persist
