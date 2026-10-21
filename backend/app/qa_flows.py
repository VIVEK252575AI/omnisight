"""Strict, bounded ShopLite test-flow schema and validation helpers."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FlowViewport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(ge=320, le=1920)
    height: int = Field(ge=480, le=2160)


class FlowTimeoutDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    navigation_timeout_ms: int = Field(default=15000, ge=1000, le=15000)
    action_timeout_ms: int = Field(default=5000, ge=100, le=15000)
    expect_timeout_ms: int = Field(default=5000, ge=100, le=15000)


class LocatorSpec(BaseModel):
    """A structured locator; never an executable selector or script."""

    model_config = ConfigDict(extra="forbid")

    strategy: Literal["role", "label", "placeholder", "text", "test_id", "css"]
    role: str | None = Field(default=None, min_length=1, max_length=40)
    value: str | None = Field(default=None, min_length=1, max_length=200)
    exact: bool = False

    @model_validator(mode="after")
    def validate_strategy_fields(self):
        if self.strategy == "role":
            if self.role not in {"button", "link", "heading", "textbox", "status", "main", "form"} or self.value is None:
                raise ValueError("role locators require a supported role and accessible name")
        elif self.role is not None or self.value is None:
            raise ValueError(f"{self.strategy} locators require only a value")
        if self.strategy == "css" and not re.fullmatch(r"[\w\s#.,>+~*:\-\[\]=\"']{1,200}", self.value or ""):
            raise ValueError("CSS locator contains unsupported syntax")
        return self


class FlowStepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    label: str = Field(min_length=1, max_length=120)


class NavigateStep(FlowStepBase):
    type: Literal["navigate"]
    url: Literal["/", "/checkout"]
    timeout_ms: int = Field(default=15000, ge=1000, le=15000)


class ClickStep(FlowStepBase):
    type: Literal["click"]
    locator: LocatorSpec
    timeout_ms: int = Field(default=5000, ge=100, le=15000)


class FillStep(FlowStepBase):
    type: Literal["fill"]
    locator: LocatorSpec
    value: str = Field(max_length=120)
    timeout_ms: int = Field(default=5000, ge=100, le=15000)


class SelectOptionStep(FlowStepBase):
    type: Literal["select_option"]
    locator: LocatorSpec
    value: str = Field(min_length=1, max_length=100)
    timeout_ms: int = Field(default=5000, ge=100, le=15000)


class WaitForStep(FlowStepBase):
    type: Literal["wait_for"]
    locator: LocatorSpec
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"
    timeout_ms: int = Field(default=5000, ge=100, le=15000)


class AssertVisibleStep(FlowStepBase):
    type: Literal["assert_visible"]
    locator: LocatorSpec
    timeout_ms: int = Field(default=5000, ge=100, le=15000)


class AssertTextStep(FlowStepBase):
    type: Literal["assert_text"]
    locator: LocatorSpec
    text: str = Field(min_length=1, max_length=200)
    exact: bool = False
    timeout_ms: int = Field(default=5000, ge=100, le=15000)


class AssertUrlStep(FlowStepBase):
    type: Literal["assert_url"]
    url: Literal["/", "/checkout"]


class ScreenshotStep(FlowStepBase):
    type: Literal["screenshot"]
    capture: Literal["viewport"] = "viewport"


class CaptureDomStep(FlowStepBase):
    type: Literal["capture_dom"]
    locator: LocatorSpec | None = None


FlowStep = Annotated[
    Union[
        NavigateStep,
        ClickStep,
        FillStep,
        SelectOptionStep,
        WaitForStep,
        AssertVisibleStep,
        AssertTextStep,
        AssertUrlStep,
        ScreenshotStep,
        CaptureDomStep,
    ],
    Field(discriminator="type"),
]


class ShopLiteFlowDefinition(BaseModel):
    """Version-two flow envelope for the loopback-only ShopLite application."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    flow_key: Literal["shoplite-checkout-visual-v1"] = "shoplite-checkout-visual-v1"
    name: str = Field(default="ShopLite checkout visual QA", min_length=1, max_length=120)
    description: str = Field(
        default="Capture and check the controlled ShopLite checkout at desktop and mobile sizes.",
        max_length=500,
    )
    route: Literal["/checkout"] = "/checkout"
    defaults: FlowTimeoutDefaults = Field(default_factory=FlowTimeoutDefaults)
    viewports: list[FlowViewport] = Field(min_length=2, max_length=2)
    assertions: list[Literal[
        "checkout_button_visible", "mobile_overflow_measured", "order_confirmation"
    ]] = Field(default_factory=list, max_length=3)
    steps: list[FlowStep] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_flow(self):
        mobile, desktop = self.viewports
        if mobile.width > 600 or desktop.width < 800:
            raise ValueError("viewports must be ordered mobile first, then desktop")
        keys = [step.key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("step keys must be unique")
        if sum(step.type == "screenshot" for step in self.steps) > 4:
            raise ValueError("a flow may contain at most four screenshot steps")
        if len(self.model_dump_json().encode("utf-8")) > 20_000:
            raise ValueError("flow definition exceeds 20000 bytes")
        return self


DEFAULT_FLOW_STEPS = [
    {"key": "open-checkout", "label": "Open checkout", "type": "navigate", "url": "/checkout"},
    {
        "key": "checkout-heading", "label": "Checkout heading is visible", "type": "assert_visible",
        "locator": {"strategy": "role", "role": "heading", "value": "Checkout", "exact": True},
    },
    {
        "key": "checkout-button", "label": "Place order control is visible", "type": "assert_visible",
        "locator": {"strategy": "role", "role": "button", "value": "Place demo order · $48.00", "exact": True},
    },
    {"key": "checkout-screenshot", "label": "Capture checkout screenshot", "type": "screenshot"},
    {
        "key": "submit-demo-order", "label": "Submit the no-payment demo order", "type": "click",
        "locator": {"strategy": "role", "role": "button", "value": "Place demo order · $48.00", "exact": True},
    },
    {
        "key": "order-confirmation", "label": "Confirm no payment was collected", "type": "assert_text",
        "locator": {"strategy": "css", "value": "#order-status"},
        "text": "No payment was collected",
    },
]


def validate_flow_definition(value: dict) -> dict:
    """Validate and return normalized JSON-compatible flow data."""
    return ShopLiteFlowDefinition.model_validate(value).model_dump(mode="json")


def validate_flow_steps(steps: list[dict]) -> list[dict]:
    """Validate a step list with the fixed ShopLite envelope and viewport policy."""
    definition = {
        "schema_version": 2,
        "flow_key": "shoplite-checkout-visual-v1",
        "name": "ShopLite checkout visual QA",
        "description": "Capture and check the controlled ShopLite checkout at desktop and mobile sizes.",
        "route": "/checkout",
        "defaults": {},
        "viewports": [{"width": 390, "height": 844}, {"width": 1440, "height": 900}],
        "assertions": ["checkout_button_visible", "mobile_overflow_measured", "order_confirmation"],
        "steps": steps,
    }
    return validate_flow_definition(definition)["steps"]


def flow_definition_json(value: dict) -> str:
    return json.dumps(validate_flow_definition(value), separators=(",", ":"))
