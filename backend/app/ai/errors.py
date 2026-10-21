"""AI Gateway / VLM adapter error taxonomy.

Model providers fail in many different ways. Everything is normalised into a
single :class:`AiError` carrying a stable :class:`AiErrorCode`, so callers never
have to know which provider (or SDK) was behind it.

The adapter boundary exposes *typed* subclasses so call sites can catch a
specific failure class without inspecting ``code``. Adapter errors never carry
provider response bodies, secrets, or raw model output.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class AiErrorCode(str, Enum):
    """Stable machine-readable AI failure codes."""

    # Configuration / wiring problems (never the model's fault).
    AI_CONFIGURATION_ERROR = "AI_CONFIGURATION_ERROR"
    AI_CAPABILITY_UNSUPPORTED = "AI_CAPABILITY_UNSUPPORTED"

    # Provider transport failures.
    AI_PROVIDER_AUTH_ERROR = "AI_PROVIDER_AUTH_ERROR"
    AI_PROVIDER_RATE_LIMITED = "AI_PROVIDER_RATE_LIMITED"
    AI_PROVIDER_UNAVAILABLE = "AI_PROVIDER_UNAVAILABLE"
    AI_TIMEOUT = "AI_TIMEOUT"

    # Response-shape failures (the model answered, but not usably).
    AI_RESPONSE_EMPTY = "AI_RESPONSE_EMPTY"
    AI_RESPONSE_NOT_JSON = "AI_RESPONSE_NOT_JSON"
    AI_SCHEMA_VALIDATION_FAILED = "AI_SCHEMA_VALIDATION_FAILED"
    AI_EVIDENCE_VALIDATION_FAILED = "AI_EVIDENCE_VALIDATION_FAILED"

    # Model-declared outcomes.
    AI_INSUFFICIENT_CONTEXT = "AI_INSUFFICIENT_CONTEXT"
    AI_SCOPE_EXCEEDED = "AI_SCOPE_EXCEEDED"
    AI_REFUSED = "AI_REFUSED"

    # Budget / lifecycle.
    AI_BUDGET_EXCEEDED = "AI_BUDGET_EXCEEDED"
    AI_CANCELLED = "AI_CANCELLED"


# Codes where a single bounded "repair" re-ask may legitimately help.
_REPAIRABLE: frozenset[AiErrorCode] = frozenset(
    {
        AiErrorCode.AI_RESPONSE_EMPTY,
        AiErrorCode.AI_RESPONSE_NOT_JSON,
        AiErrorCode.AI_SCHEMA_VALIDATION_FAILED,
    }
)


class AiError(Exception):
    """A normalised AI Gateway failure.

    ``retryable`` marks transport hiccups a future caller may retry with
    backoff. ``details`` is a small, non-sensitive list (never raw output).
    """

    def __init__(
        self,
        code: AiErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or []
        super().__init__(message)

    @property
    def repairable(self) -> bool:
        """True when a bounded repair re-ask may help."""
        return self.code in _REPAIRABLE


# ---------------------------------------------------------------------------
# Typed adapter exceptions (one per error class the adapter can raise)
# ---------------------------------------------------------------------------


class VisionAdapterError(AiError):
    """Base class for all VLM adapter failures."""


class VisionConfigurationError(VisionAdapterError):
    """Adapter could not be constructed / selected (bad ``VLM_ADAPTER``, etc.)."""

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            AiErrorCode.AI_CONFIGURATION_ERROR, message, details=details
        )


class VisionUnavailableError(VisionAdapterError):
    """The provider is unreachable or not configured."""

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            AiErrorCode.AI_PROVIDER_UNAVAILABLE, message, retryable=True, details=details
        )


class VisionTimeoutError(VisionAdapterError):
    """The provider did not respond within the configured timeout."""

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            AiErrorCode.AI_TIMEOUT, message, retryable=True, details=details
        )


class VisionRateLimitedError(VisionAdapterError):
    """The provider rate-limited the request."""

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            AiErrorCode.AI_PROVIDER_RATE_LIMITED,
            message,
            retryable=True,
            details=details,
        )


class VisionAuthError(VisionAdapterError):
    """The provider rejected our credentials."""

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            AiErrorCode.AI_PROVIDER_AUTH_ERROR, message, details=details
        )


class VisionInvalidResponseError(VisionAdapterError):
    """The adapter returned output that is not usable text."""

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            AiErrorCode.AI_RESPONSE_NOT_JSON, message, details=details
        )


class VisionPayloadTooLargeError(VisionAdapterError):
    """The image payload exceeded the configured budget (raised before the call)."""

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            AiErrorCode.AI_BUDGET_EXCEEDED, message, details=details
        )
