from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db_models import Base, QARun, RunStep, TestFlow as FlowEntity, TestFlowVersion as FlowVersionEntity
from app.demo_run_store import DEMO_FLOW_DEFINITION, DemoRunStore
from app.qa_flows import DEFAULT_FLOW_STEPS, ShopLiteFlowDefinition, validate_flow_steps


def test_default_shoplite_flow_is_valid_and_finite():
    flow = ShopLiteFlowDefinition.model_validate(DEMO_FLOW_DEFINITION)

    assert flow.schema_version == 2
    assert flow.name == "ShopLite checkout visual QA"
    assert flow.defaults.navigation_timeout_ms == 15000
    assert flow.defaults.action_timeout_ms == 5000
    assert [step.type for step in flow.steps] == [
        "navigate", "assert_visible", "assert_visible", "screenshot", "click", "assert_text"
    ]
    assert len(flow.steps) <= 20


@pytest.mark.parametrize(
    "mutation",
    [
        lambda flow: flow["steps"][0].update(url="https://attacker.example/"),
        lambda flow: flow["steps"][0].update(script="page.evaluate('alert(1)')"),
        lambda flow: flow["steps"][0].update(type="run_python"),
        lambda flow: flow["steps"][1]["locator"].update(strategy="css", value="body:has-text('x')"),
    ],
)
def test_flow_rejects_external_routes_executable_fields_unknown_steps_and_unsafe_css(mutation):
    flow = deepcopy(DEMO_FLOW_DEFINITION)
    mutation(flow)

    with pytest.raises(ValidationError):
        ShopLiteFlowDefinition.model_validate(flow)


def test_flow_rejects_duplicate_keys_oversized_steps_and_invalid_timeouts():
    duplicate = deepcopy(DEFAULT_FLOW_STEPS)
    duplicate[1]["key"] = duplicate[0]["key"]
    with pytest.raises(ValidationError, match="unique"):
        validate_flow_steps(duplicate)

    too_many = DEFAULT_FLOW_STEPS * 4
    with pytest.raises(ValidationError):
        validate_flow_steps(too_many)

    bad_timeout = deepcopy(DEFAULT_FLOW_STEPS)
    bad_timeout[1]["timeout_ms"] = 900_000
    with pytest.raises(ValidationError):
        validate_flow_steps(bad_timeout)

    screenshots = [
        {"key": f"screen-{index}", "label": "Capture viewport", "type": "screenshot"}
        for index in range(5)
    ]
    with pytest.raises(ValidationError, match="four screenshot"):
        validate_flow_steps(screenshots)


def test_flow_rejects_unknown_envelope_and_step_properties():
    flow = deepcopy(DEMO_FLOW_DEFINITION)
    flow["network_policy"] = "allow-all"
    with pytest.raises(ValidationError):
        ShopLiteFlowDefinition.model_validate(flow)

    flow = deepcopy(DEMO_FLOW_DEFINITION)
    flow["steps"][0]["headers"] = {"Authorization": "secret"}
    with pytest.raises(ValidationError):
        ShopLiteFlowDefinition.model_validate(flow)


def test_legacy_viewport_only_flow_gets_an_immutable_schema_v2_successor():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    database = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings()
    store = DemoRunStore(database, settings=settings)
    try:
        original = store.workspace()["test_flow"]
        with database() as session:
            flow = session.scalar(select(FlowEntity).where(FlowEntity.project_id == settings.local_project_id))
            old_version = session.scalar(select(FlowVersionEntity).where(FlowVersionEntity.test_flow_id == flow.id))
            legacy_definition = dict(old_version.definition_json)
            legacy_definition.pop("steps")
            legacy_definition["schema_version"] = 1
            old_version.schema_version = 1
            old_version.definition_json = legacy_definition
            old_version.definition_hash = sha256(json.dumps(legacy_definition, sort_keys=True).encode()).hexdigest()
            session.commit()

        upgraded = store.workspace()["test_flow"]

        assert original["version"] == 1
        assert upgraded["version"] == 2
        assert upgraded["definition"]["schema_version"] == 2
        assert upgraded["definition"]["steps"] == validate_flow_steps(DEFAULT_FLOW_STEPS)
        with database() as session:
            versions = session.scalars(select(FlowVersionEntity).order_by(FlowVersionEntity.version_number)).all()
            assert [version.status for version in versions] == ["retired", "published"]
            assert versions[0].schema_version == 1
            assert versions[1].schema_version == 2
    finally:
        engine.dispose()


def test_browser_flow_step_outcomes_are_persisted_as_run_steps_without_input_values():
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    database = sessionmaker(bind=engine, expire_on_commit=False)
    store = DemoRunStore(database, settings=Settings())
    try:
        run = store.create()
        assert run is not None
        store.update(
            run.run_id,
            status="awaiting_sandbox_approval",
            result={
                "status": "awaiting_sandbox_approval",
                "capture": {
                    "flow_steps": [
                        {"step_key": "fill-email", "label": "Fill email", "type": "fill", "viewport": "mobile", "status": "passed", "duration_ms": 18},
                        {"step_key": "submit", "label": "Submit", "type": "click", "viewport": "mobile", "status": "timeout", "duration_ms": 5000},
                    ]
                },
            },
        )
        with database() as session:
            rows = session.scalars(
                select(RunStep).where(RunStep.run_id == run.run_id, RunStep.ordinal >= 100).order_by(RunStep.ordinal)
            ).all()
            assert [row.status for row in rows] == ["passed", "timed_out"]
            assert [row.step_key for row in rows] == ["mobile: fill-email", "mobile: submit"]
            assert rows[0].input_summary_json == {}
            assert rows[0].result_summary_json["duration_ms"] == 18
            assert session.get(QARun, run.run_id).outcome == "test_failed"
    finally:
        engine.dispose()
