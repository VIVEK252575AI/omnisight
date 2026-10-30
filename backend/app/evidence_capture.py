"""Fresh evidence capture from a patched workspace (Week 3, W3.3).

Verification needs NEW screenshots and NEW geometry taken from the patched
copy, at the same viewports as the original evidence. This module is the
boundary that produces them:

* :class:`ShopLiteCheckoutCollector` serves the workspace on loopback and drives
  the team's existing Playwright flow
  (:mod:`scripts.capture_demo_checkout`) — the module is imported, never
  re-implemented, and its ``observation/v1`` JSON is validated with the
  project's :class:`app.schemas.Observation` model before anything is stored.
* :class:`FixtureEvidenceCollector` returns canned observations for tests and
  offline development. It is labelled ``FIXTURE`` everywhere and can never be
  mistaken for a real capture (``is_fixture`` propagates onto the verification
  record).

Both store their results through
:meth:`app.evidence_storage.EvidenceStore.add`, so fresh evidence is linked to
the scan exactly like the original capture and its artifact ids can be resolved
by the VLM adapter.

The collector is deliberately narrow: it captures the fixed ShopLite checkout
flow (``/checkout``). A generic collector for arbitrary targets is the
Playwright owner's module and slots in behind the same
:class:`WorkspaceEvidenceCollector` protocol.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable
from uuid import UUID

from pydantic import ValidationError

from app.config import Settings
from app.evidence_storage import EvidenceStore
from app.ids import new_uuid, utcnow
from app.local_server import ServeError, served_workspace
from app.schemas import EvidenceRecord, Observation, Viewport

logger = logging.getLogger("omnisight")

#: The fixed demo flow the ShopLite collector drives.
SHOPLITE_ROUTE = "/checkout"
DEFAULT_MOBILE = (390, 844)
DEFAULT_DESKTOP = (1440, 900)
FIXTURE_LABEL = "FIXTURE"


class CaptureError(RuntimeError):
    """A safe, coded failure while capturing fresh evidence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CapturedScreen:
    """One freshly captured (route, viewport) observation."""

    route: str
    viewport: Viewport
    observation: Observation
    screenshot_path: Path
    artifact_id: UUID


@dataclass(frozen=True)
class CaptureResult:
    """All fresh evidence produced for one verification attempt."""

    screens: list[CapturedScreen]
    collector_label: str
    is_fixture: bool
    base_url: str | None

    @property
    def artifact_ids(self) -> list[UUID]:
        return [screen.artifact_id for screen in self.screens]

    def screen_for(self, viewport: Viewport | None) -> CapturedScreen | None:
        """Best match for a viewport; falls back to the narrowest screen."""
        if viewport is None:
            return self.screens[0] if self.screens else None
        for screen in self.screens:
            if (
                screen.viewport.width == viewport.width
                and screen.viewport.height == viewport.height
            ):
                return screen
        return min(self.screens, key=lambda item: item.viewport.width, default=None)


@runtime_checkable
class WorkspaceEvidenceCollector(Protocol):
    """Captures fresh evidence from a served workspace build."""

    label: str
    is_fixture: bool

    def capture(
        self,
        *,
        scan_id: UUID,
        source_dir: Path,
        viewports: Sequence[Viewport],
        attempt_id: str | None = None,
    ) -> CaptureResult:
        """Serve ``source_dir``, capture fresh evidence and persist it."""
        ...


def _flow_viewports(viewports: Sequence[Viewport]) -> list[dict[str, int]]:
    """Pick a mobile and a desktop viewport for the fixed demo flow.

    The demo collector requires exactly ``[mobile, desktop]`` with mobile
    ``<= 600`` wide. When the scan's viewports do not contain one of each, the
    demo defaults are used and the caller can see that from the captured
    screens themselves.
    """
    mobile = next((v for v in viewports if v.width <= 600), None)
    desktop = next((v for v in viewports if v.width >= 800), None)
    mobile_bounds = (
        (mobile.width, mobile.height) if mobile is not None else DEFAULT_MOBILE
    )
    desktop_bounds = (
        (desktop.width, desktop.height) if desktop is not None else DEFAULT_DESKTOP
    )
    return [
        {"width": mobile_bounds[0], "height": mobile_bounds[1]},
        {"width": desktop_bounds[0], "height": desktop_bounds[1]},
    ]


class ShopLiteCheckoutCollector:
    """Real capture: serve the workspace, drive the team's checkout flow."""

    label = "PLAYWRIGHT_SHOPLITE"
    is_fixture = False

    def __init__(self, *, settings: Settings, evidence_store: EvidenceStore) -> None:
        self.settings = settings
        self.evidence_store = evidence_store

    def capture(
        self,
        *,
        scan_id: UUID,
        source_dir: Path,
        viewports: Sequence[Viewport],
        attempt_id: str | None = None,
    ) -> CaptureResult:
        try:
            # Imported lazily: the app must start even where Playwright is not
            # installed, and the failure must be reported, not swallowed.
            from scripts.capture_demo_checkout import capture as shoplite_capture
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise CaptureError(
                "COLLECTOR_UNAVAILABLE",
                f"the Playwright collector is not importable: {type(exc).__name__}",
            ) from None

        artifact_root = Path(self.settings.artifacts_dir) / "verification" / (
            attempt_id or str(scan_id)
        )
        artifact_root.mkdir(parents=True, exist_ok=True)

        base_url = None
        try:
            with served_workspace(self.settings, source_dir) as served:
                base_url = served.base_url
                summary = shoplite_capture(
                    served.base_url,
                    artifact_root,
                    "overflow",
                    _flow_viewports(viewports),
                )
        except ServeError as exc:
            raise CaptureError(exc.code, str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - collector boundary
            logger.exception("fresh capture failed")
            raise CaptureError(
                "CAPTURE_FAILED", f"the collector failed: {type(exc).__name__}"
            ) from None

        return self._persist(scan_id, artifact_root, summary, base_url=base_url)

    def _persist(
        self,
        scan_id: UUID,
        artifact_root: Path,
        summary: dict,
        *,
        base_url: str | None,
    ) -> CaptureResult:
        screens: list[CapturedScreen] = []
        for name, payload in sorted((summary.get("viewports") or {}).items()):
            observation_path = artifact_root / str(payload.get("observation", ""))
            screenshot_path = artifact_root / str(payload.get("screenshot", ""))
            try:
                observation = Observation.model_validate_json(
                    observation_path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError, ValueError) as exc:
                raise CaptureError(
                    "CAPTURE_OBSERVATION_INVALID",
                    f"the collector's {name} observation is not valid observation/v1 "
                    f"({type(exc).__name__})",
                ) from None
            if not screenshot_path.is_file():
                raise CaptureError(
                    "CAPTURE_SCREENSHOT_MISSING",
                    f"the collector did not write a screenshot for {name}",
                )
            screens.append(
                self._store(scan_id, observation, screenshot_path)
            )
        if not screens:
            raise CaptureError(
                "CAPTURE_EMPTY", "the collector produced no observations"
            )
        logger.info(
            "fresh evidence captured",
            extra={"scan_id": str(scan_id), "screens": len(screens)},
        )
        return CaptureResult(
            screens=screens,
            collector_label=self.label,
            is_fixture=self.is_fixture,
            base_url=base_url,
        )

    def _store(
        self, scan_id: UUID, observation: Observation, screenshot_path: Path
    ) -> CapturedScreen:
        artifact_id = new_uuid()
        record = EvidenceRecord(
            evidence_id=new_uuid(),
            scan_id=scan_id,
            observation=observation,
            screenshot_artifact_id=artifact_id,
            created_at=utcnow(),
        )
        self.evidence_store.add(
            record, artifact_id=artifact_id, artifact_path=screenshot_path
        )
        return CapturedScreen(
            route=observation.route or SHOPLITE_ROUTE,
            viewport=observation.viewport,
            observation=observation,
            screenshot_path=screenshot_path,
            artifact_id=artifact_id,
        )


class FixtureEvidenceCollector:
    """TEST/FIXTURE collector: canned observations, no browser, no server.

    Labelled ``FIXTURE`` so a verification driven by it can never be presented
    as a real visual check.
    """

    label = FIXTURE_LABEL
    is_fixture = True

    def __init__(self, screens: Sequence[tuple[Observation, Path]]) -> None:
        self._screens = list(screens)

    def capture(
        self,
        *,
        scan_id: UUID,
        source_dir: Path | None = None,
        viewports: Sequence[Viewport] = (),
        attempt_id: str | None = None,
    ) -> CaptureResult:
        screens = [
            CapturedScreen(
                route=observation.route or SHOPLITE_ROUTE,
                viewport=observation.viewport,
                observation=observation,
                screenshot_path=path,
                artifact_id=new_uuid(),
            )
            for observation, path in self._screens
        ]
        if not screens:
            raise CaptureError("CAPTURE_EMPTY", "the fixture collector has no screens")
        return CaptureResult(
            screens=screens,
            collector_label=self.label,
            is_fixture=True,
            base_url=None,
        )


def load_observation(path: Path) -> Observation:
    """Read an ``observation/v1`` JSON file through the project's schema."""
    try:
        return Observation.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValidationError, ValueError) as exc:
        raise CaptureError(
            "OBSERVATION_INVALID", f"not a valid observation/v1 document: {type(exc).__name__}"
        ) from None
