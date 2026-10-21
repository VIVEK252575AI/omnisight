"""The VLM adapter boundary.

``VisionAnalyzer`` is the single replaceable interface between OmniSight and a
vision model. Implementations:

- resolve screenshot artifact ids to on-disk paths (server-side only),
- enforce the image payload budget *before* any call,
- call the underlying provider under a timeout,
- return raw, untrusted text as :class:`RawVisionResult` (with a
  ``provider_label``).

Adapters MUST NOT parse, execute, persist, or interpret model output. That is
the gateway's job.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Protocol, runtime_checkable
from uuid import UUID

from app.ai.contracts import RawVisionResult, VisionRequest
from app.ai.errors import VisionPayloadTooLargeError

# Maps an artifact id to its validated on-disk path (or None if unknown).
ImageResolver = Callable[[UUID], "Path | None"]

_REDACTED = "[redacted]"


@runtime_checkable
class VisionAnalyzer(Protocol):
    """A pluggable vision backend (teammate module, hosted API, or test mock)."""

    provider_label: str
    is_mock: bool

    def analyze(
        self, request: VisionRequest, *, repair: bool = False
    ) -> RawVisionResult:
        """Analyze ``request`` and return the raw, untrusted result.

        ``repair`` requests a re-ask after a shape failure (the same task with a
        stricter output instruction). Transport failures raise a typed
        :class:`~app.ai.errors.VisionAdapterError`.
        """
        ...


def resolve_image_paths(
    request: VisionRequest, resolver: ImageResolver | None
) -> list[Path]:
    """Resolve the request's screenshot artifact ids to on-disk paths."""
    if resolver is None:
        return []
    paths: list[Path] = []
    for shot in request.screenshots:
        resolved = resolver(shot.artifact_id)
        if resolved is not None:
            paths.append(resolved)
    return paths


def enforce_image_budget(
    paths: Iterable[Path],
    *,
    max_total_bytes: int,
    max_count: int | None = None,
) -> None:
    """Raise :class:`VisionPayloadTooLargeError` if the payload is over budget.

    Runs before any provider call so oversized payloads never leave the process.
    """
    materialised = list(paths)
    if max_count is not None and len(materialised) > max_count:
        raise VisionPayloadTooLargeError(
            f"image count {len(materialised)} exceeds the {max_count} allowed",
        )
    total = 0
    for path in materialised:
        try:
            total += path.stat().st_size
        except OSError:
            continue
        if total > max_total_bytes:
            raise VisionPayloadTooLargeError(
                f"image payload exceeds the {max_total_bytes} byte budget",
            )


def call_with_timeout(fn: Callable[[], object], timeout_seconds: float) -> object:
    """Run ``fn`` under a wall-clock timeout.

    A timeout raises :class:`~app.ai.errors.VisionTimeoutError`. Other exceptions
    from ``fn`` propagate unchanged so the adapter can map them.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeoutError

    from app.ai.errors import VisionTimeoutError

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            future.cancel()
            raise VisionTimeoutError(
                f"provider did not respond within {timeout_seconds}s"
            ) from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def adapter_logger(name: str) -> logging.Logger:
    """Return a namespaced logger for an adapter."""
    return logging.getLogger(f"omnisight.ai.{name}")


def log_vision_call(
    logger: logging.Logger,
    *,
    provider_label: str,
    request: VisionRequest,
    image_count: int,
    repair: bool = False,
) -> None:
    """Log a *redacted* summary of a VLM call.

    Only non-sensitive metadata is logged: never image bytes/base64, prompts,
    credentials, or model output.
    """
    logger.info(
        "vlm call provider=%s request_id=%s repair=%s screenshots=%d "
        "measurements=%d category_allowlist=%s",
        provider_label,
        request.request_id,
        repair,
        image_count,
        len(request.measurements),
        [c.value for c in request.allowed_categories],
    )
