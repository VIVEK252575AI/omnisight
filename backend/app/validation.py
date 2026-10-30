"""Build/test validation of a patched workspace (Week 3, W3.3).

The validator answers one question only: *do the configured checks pass inside
this workspace?* It never decides that a defect is fixed — that is the
verifier's job, and a passing build is explicitly not evidence of a visual fix.

Commands come from configuration (``OMNISIGHT_VALIDATION_COMMANDS``), run with
:func:`app.command_runner.run_command` (argument lists, sanitised environment,
timeout, output cap, tree kill) and with the workspace's ``source/`` directory
as ``cwd``. Nothing from a proposal or a model reaches this module.

Statuses are deliberately explicit:

- ``passed``          — every configured command exited 0
- ``failed``           — a command exited non-zero, timed out, or could not start
- ``not_configured``   — no commands are configured, so nothing was checked

``not_configured`` is NOT a pass. The verification policy refuses to certify a
fix on it, which keeps "we didn't check" from being reported as "it works".
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.command_runner import CommandError, CommandResult, run_command
from app.config import Settings
from app.ids import utcnow

logger = logging.getLogger("omnisight")


class ValidationStatus(str, Enum):
    """Outcome of running the configured checks."""

    passed = "passed"
    failed = "failed"
    not_configured = "not_configured"


class CommandRun(BaseModel):
    """One bounded, reported command execution (safe to expose on the API)."""

    command: list[str] = Field(default_factory=list, max_length=50)
    exit_code: int | None = None
    timed_out: bool = False
    duration_seconds: float = Field(default=0.0, ge=0.0)
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    passed: bool = False

    @classmethod
    def from_result(cls, result: CommandResult) -> "CommandRun":
        record = result.as_record()
        return cls(**record)


class ValidationResult(BaseModel):
    """Everything the attempt needs to know about the build/test checks."""

    status: ValidationStatus
    commands: list[CommandRun] = Field(default_factory=list, max_length=20)
    reasons: list[str] = Field(default_factory=list, max_length=20)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    started_at: datetime
    finished_at: datetime

    @property
    def passed(self) -> bool:
        """True only for an explicitly successful run."""
        return self.status is ValidationStatus.passed

    def summary(self) -> str:
        """A one-line, non-sensitive summary for the attempt record."""
        if self.status is ValidationStatus.not_configured:
            return "validation not configured: no build/test commands are set"
        if self.status is ValidationStatus.passed:
            return f"validation passed ({len(self.commands)} command(s))"
        detail = "; ".join(self.reasons[:3]) or "a configured command failed"
        return f"validation failed: {detail}"


class ValidationRunner:
    """Runs the configured checks against a workspace's working copy."""

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    @property
    def commands(self) -> list[list[str]]:
        """The configured commands, exactly as configured (never derived)."""
        return [list(command) for command in self.settings.validation_commands]

    def run(self, workspace_root) -> ValidationResult:
        """Execute every configured command in ``workspace_root``.

        Execution stops at the first failure: a later command cannot make a
        failed build succeed, and running it would only cost time.
        """
        started = utcnow()
        monotonic_start = time.monotonic()
        commands = self.commands
        if not commands:
            return ValidationResult(
                status=ValidationStatus.not_configured,
                commands=[],
                reasons=["no validation commands are configured"],
                duration_seconds=0.0,
                started_at=started,
                finished_at=utcnow(),
            )

        runs: list[CommandRun] = []
        reasons: list[str] = []
        status = ValidationStatus.passed
        for command in commands:
            try:
                result = run_command(
                    command,
                    cwd=workspace_root,
                    timeout_seconds=self.settings.command_timeout_seconds,
                    max_output_bytes=self.settings.command_output_max_bytes,
                    env_extra=self.settings.validation_env,
                )
            except CommandError as exc:
                status = ValidationStatus.failed
                reasons.append(f"{exc.code}: {exc}")
                runs.append(
                    CommandRun(command=command, passed=False, stderr=str(exc))
                )
                break

            run = CommandRun.from_result(result)
            runs.append(run)
            if result.timed_out:
                status = ValidationStatus.failed
                reasons.append(
                    f"`{command[0]}` timed out after "
                    f"{self.settings.command_timeout_seconds:g}s"
                )
                break
            if not result.passed:
                status = ValidationStatus.failed
                reasons.append(
                    f"`{command[0]}` exited with code {result.exit_code}"
                )
                break

        finished = utcnow()
        result = ValidationResult(
            status=status,
            commands=runs,
            reasons=reasons,
            duration_seconds=round(time.monotonic() - monotonic_start, 3),
            started_at=started,
            finished_at=finished,
        )
        logger.info(
            "validation finished",
            extra={"status": status.value, "commands": len(runs)},
        )
        return result
