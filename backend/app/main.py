"""OmniSight backend entry point (FastAPI).

The application is built by :func:`create_app`; ``app`` is the import-time
instance used by the ASGI server.

Start (from ``backend/``)::

    .\\.venv\\Scripts\\uvicorn.exe app.main:app --reload

Test (from ``backend/``)::

    .\\.venv\\Scripts\\python.exe -m pytest tests\\ -v

OpenAPI docs are served at ``/docs`` (and ``/openapi.json``).

The patch-proposal endpoints ARE wired (W2.5), but they only ever produce a
reviewable proposal record — approving and applying a patch to a source tree is
a later slice. Background execution (Playwright worker, patch apply) is
deliberately NOT wired here yet — those land in their own task slices and are
gated by feature flags that stay off until the modules actually exist. The VLM adapter is
selected and constructed at startup (fail-fast on unknown ``VLM_ADAPTER``) and
stored on ``app.state.vision_analyzer``. It is an early sanity check only; the
detection endpoints build their own adapter per run so image resolution targets
the evidence store in use for that request (see
``app.routers.detection.get_detection_analyzer``).
"""

import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.ai.adapters.factory import create_vision_analyzer
from app.config import Settings, get_settings
from app.errors import register_exception_handlers
from app.evidence_storage import get_evidence_store
from app.gemini import router as ai_router
from app.routers import ci_webhooks, demo_runs, detection, evidence, github_publish, patches, scans

logger = logging.getLogger("omnisight")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="OmniSight Backend",
        version=settings.app_version,
        description="Central coordinator for the Detect -> Fix -> Verify -> Deliver workflow.",
    )

    # VLM adapter boundary. Selecting an unknown adapter fails fast here so a
    # misconfigured deployment never starts. The adapter is stored on app.state
    # for later orchestration to consume; nothing calls it on a request yet.
    vision_analyzer = create_vision_analyzer(
        settings, resolver=get_evidence_store().get_artifact
    )
    app.state.vision_analyzer = vision_analyzer
    logger.info(
        "VLM adapter: %s (mock=%s)",
        vision_analyzer.provider_label,
        vision_analyzer.is_mock,
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
    app.include_router(detection.router)
    app.include_router(demo_runs.router)
    app.include_router(patches.router)
    app.include_router(github_publish.router)
    app.include_router(ai_router)

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

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        """Serve the local dashboard from the same origin as the API."""
        return FileResponse(REPOSITORY_ROOT / "code.html", media_type="text/html")

    @app.get("/styles.css", include_in_schema=False)
    def dashboard_styles() -> FileResponse:
        return FileResponse(REPOSITORY_ROOT / "styles.css", media_type="text/css")

    @app.get("/script.js", include_in_schema=False)
    def dashboard_script() -> FileResponse:
        return FileResponse(REPOSITORY_ROOT / "script.js", media_type="text/javascript")

    return app


app = create_app()
