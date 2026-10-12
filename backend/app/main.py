"""OmniSight backend entry point (FastAPI).

The application is built by :func:`create_app`; ``app`` is the import-time
instance used by the ASGI server.

Start (from ``backend/``)::

    .\\.venv\\Scripts\\uvicorn.exe app.main:app --reload

Test (from ``backend/``)::

    .\\.venv\\Scripts\\python.exe -m pytest tests\\ -v

OpenAPI docs are served at ``/docs`` (and ``/openapi.json``).

Background execution (Playwright worker, VLM, patch engine) is deliberately
NOT wired here yet — those land in their own task slices and are gated by
feature flags that stay off until the modules actually exist.
"""

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.errors import register_exception_handlers
from app.routers import ci_webhooks, evidence, scans

logger = logging.getLogger("omnisight")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="OmniSight Backend",
        version=settings.app_version,
        description="Central coordinator for the Detect -> Fix -> Verify -> Deliver workflow.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(scans.router)
    app.include_router(ci_webhooks.router)
    app.include_router(evidence.router)

    if settings.ci_webhook_secret:
        logger.info("CI webhook receiver: signature verification ENABLED")
    else:
        logger.warning(
            "CI webhook receiver is UNAUTHENTICATED "
            "(OMNISIGHT_CI_WEBHOOK_SECRET unset) — simulation/dev only"
        )

    @app.get("/health", tags=["system"])
    def health(settings: Settings = Depends(get_settings)) -> dict:
        """Liveness probe for the API process."""
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.app_version,
        }

    return app


app = create_app()
