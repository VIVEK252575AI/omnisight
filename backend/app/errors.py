"""Single error envelope and stable error codes for every non-2xx response.

Every error path in the application returns the same shape::

    {"error": {"code": "...", "message": "...", "details": [...]}}

- ``code``    is a stable, machine-readable string (see :class:`ErrorCode`).
- ``message`` is a human-readable, non-sensitive summary.
- ``details`` is a list of field-level entries for validation failures.

Unexpected exceptions are logged server-side and returned as a generic
``INTERNAL_ERROR`` envelope — stack traces and exception text never reach the
client.
"""

import logging
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("omnisight")


class ErrorCode(str, Enum):
    """Stable machine-readable error codes."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    UNPROCESSABLE = "UNPROCESSABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"

    # Domain-specific codes.
    SCAN_NOT_FOUND = "SCAN_NOT_FOUND"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    TERMINAL_STATE = "TERMINAL_STATE"
    URL_TOO_LONG = "URL_TOO_LONG"
    CI_EVENT_NOT_FOUND = "CI_EVENT_NOT_FOUND"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    EVIDENCE_STATE_CONFLICT = "EVIDENCE_STATE_CONFLICT"
    DETECTION_STATE_CONFLICT = "DETECTION_STATE_CONFLICT"
    FINDING_NOT_FOUND = "FINDING_NOT_FOUND"
    FINDING_NOT_PATCHABLE = "FINDING_NOT_PATCHABLE"
    PATCH_PROPOSAL_NOT_FOUND = "PATCH_PROPOSAL_NOT_FOUND"
    SHOPLITE_SANDBOX_UNAVAILABLE = "SHOPLITE_SANDBOX_UNAVAILABLE"
    SHOPLITE_SANDBOX_REJECTED = "SHOPLITE_SANDBOX_REJECTED"


class DomainError(Exception):
    """A business-rule failure mapped to a stable code and HTTP status."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or []
        super().__init__(message)


def validation_details(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise pydantic/starlette error entries into envelope `details`."""
    return [
        {
            "field": ".".join(str(part) for part in err.get("loc", [])),
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in errors
    ]


def error_envelope(
    code: ErrorCode | str,
    message: str,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the single error envelope returned by every error path."""
    code_value = code.value if isinstance(code, ErrorCode) else str(code)
    return {
        "error": {
            "code": code_value,
            "message": message,
            "details": details or [],
        }
    }


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    status.HTTP_400_BAD_REQUEST: ErrorCode.BAD_REQUEST,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    status.HTTP_409_CONFLICT: ErrorCode.CONFLICT,
    status.HTTP_422_UNPROCESSABLE_ENTITY: ErrorCode.UNPROCESSABLE,
}


def register_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that produce the shared error envelope."""

    @app.exception_handler(DomainError)
    async def _handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_envelope(
                ErrorCode.VALIDATION_ERROR,
                "Request validation failed",
                validation_details(exc.errors()),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.HTTP_ERROR)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_envelope(
                ErrorCode.INTERNAL_ERROR,
                "Internal server error — check server logs for details.",
            ),
        )
