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
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai.adapters.factory import create_vision_analyzer
from app.config import Settings, get_settings
from app.errors import register_exception_handlers
from app.evidence_storage import get_evidence_store
from app.gemini import router as ai_router
from app.orchestrator import get_attempt_store, recover_orphaned_scans
from app.patch_apply import build_patch_applier
from app.routers import ci_webhooks, demo_runs, detection, evidence, frontend_bridge, github_publish, patches, projects, repair, scans
from app.storage import get_store
from app.workspace import WorkspaceError

logger = logging.getLogger("omnisight")


def _recovery_applier(settings: Settings):
    """Patch applier for the startup rollback sweep, when it can be built.

    A process killed mid-attempt can leave a workspace half-applied, so the
    sweep restores it with the same applier the repair loop uses. Configuring
    that applier needs a patch source root; when it is missing the sweep still
    runs, it just records that no rollback could be attempted.
    """
    if not settings.enable_repair_orchestrator:
        return None
    if not str(settings.patch_source_root or "").strip():
        return None
    try:
        return build_patch_applier(settings)
    except (WorkspaceError, OSError, ValueError) as exc:
        logger.warning("repair recovery cannot roll back workspaces: %s", exc)
        return None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup/shutdown hooks.

    A self-healing run lives in a background task of ONE API process, so after
    a restart any scan still marked ``repairing`` is orphaned by definition.
    The sweep settles those scans (retryable ``detected``, or ``escalated``)
    instead of leaving them running forever. Skipped when no durable database
    is configured — there is nothing persisted to recover.
    """
    settings = get_settings()
    if settings.database_url is not None:
        try:
            recovered = recover_orphaned_scans(
                scan_store=get_store(settings),
                attempt_store=get_attempt_store(),
                settings=settings,
                applier=_recovery_applier(settings),
            )
            if recovered:
                logger.warning(
                    "settled %d orphaned repair scan(s) at startup", len(recovered)
                )
        except Exception:  # noqa: BLE001 - startup must not be blocked
            logger.exception("repair crash-recovery sweep failed")
    yield


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="OmniSight Backend",
        version=settings.app_version,
        description="Central coordinator for the Detect -> Fix -> Verify -> Deliver workflow.",
        lifespan=_lifespan,
    )

    # VLM adapter boundary. Selecting an unknown adapter fails fast here so a
    # misconfigured deployment never starts. The adapter is stored on app.state
    # for later orchestration to consume; nothing calls it on a request yet.
    vision_analyzer = create_vision_analyzer(
        settings, resolver=get_evidence_store(settings).get_artifact
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
    app.include_router(projects.router)
    app.include_router(ai_router)
    app.include_router(frontend_bridge.router)
    app.include_router(repair.router)

    if not settings.enable_repair_orchestrator:
        logger.info(
            "repair orchestration: DISABLED "
            "(OMNISIGHT_ENABLE_REPAIR_ORCHESTRATOR off) — POST /repair answers 409"
        )

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
