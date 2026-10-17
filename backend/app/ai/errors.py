"""AI Gateway error taxonomy.

Model providers fail in many different ways. The gateway normalises every
failure into a single :class:`AiError` carrying a stable :class:`AiErrorCode`,
so callers never have to know which provider (or SDK) was behind it.
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
