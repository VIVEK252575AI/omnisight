"""Bounded subprocess execution for validation and serving (Week 3, W3.3).

Every command OmniSight runs against a patched workspace goes through here:

* **Argument lists only** — a command is ``list[str]`` from configuration. No
  string is ever split, interpolated or passed to a shell, so proposal or model
  text cannot become part of a command line.
* **Sanitised environment** — the child gets a small allowlist of OS variables
  (plus explicitly configured extras), never the server's environment. VLM
  keys, GitHub tokens and ``OMNISIGHT_*`` settings are therefore not visible to
  a build script.
* **Bounded** — a wall-clock timeout and a stdout/stderr byte cap. Output is
  decoded defensively and truncated with an explicit marker.
* **No leaked processes** — on timeout the whole process *tree* is terminated
  (``taskkill /F /T`` on Windows, process group kill on POSIX), so a build that
  spawns a watcher cannot outlive the attempt.

This module never decides whether a command is *allowed*; callers pass commands
that came from configuration.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence

logger = logging.getLogger("omnisight")

#: OS variables a child process needs to start at all. Nothing project-specific
#: and nothing secret is included.
BASE_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "TZ",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
    }
)

TRUNCATED_MARKER = "\n...[output truncated]"


class CommandError(RuntimeError):
    """A safe, coded failure while starting a configured command."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CommandResult:
    """The bounded outcome of one configured command."""

    command: list[str]
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def passed(self) -> bool:
        """True only for a clean, non-timed-out, zero exit."""
        return not self.timed_out and self.exit_code == 0

    def as_record(self) -> dict:
        """A JSON-safe record for an attempt (no environment, no absolute paths)."""
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 3),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "passed": self.passed,
        }


def sanitized_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the child environment: OS allowlist + explicitly configured extras.

    ``extra`` is configuration, never model or proposal output. Any key that
    looks like a secret (``*_KEY``, ``*_TOKEN``, ``*_SECRET``, ``*_PASSWORD``,
    ``OMNISIGHT_*``, ``GITHUB_*``, ``GEMINI_*``) is dropped even if a caller
    passes it, so a misconfiguration cannot leak credentials into a build.
    """
    env = {key: value for key, value in os.environ.items() if key in BASE_ENV_ALLOWLIST}
    for key, value in (extra or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if _looks_secret(key):
            logger.warning("dropping a secret-looking variable from the child environment")
            continue
        env[key] = value
    return env


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    if upper.startswith(("OMNISIGHT_", "GITHUB_", "GEMINI_", "OPENAI_", "ANTHROPIC_")):
        return True
    return any(
        token in upper for token in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "PRIVATE_KEY")
    )


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _cap(text: str, limit: int) -> tuple[str, bool]:
    """Truncate to ``limit`` bytes of UTF-8, keeping the text valid."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    clipped = encoded[:limit].decode("utf-8", errors="ignore")
    return clipped + TRUNCATED_MARKER, True


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process and every descendant it started."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            # taskkill on our own child needs no elevation; /T covers the tree.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - fallback
            process.kill()
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):  # pragma: no cover - fallback
            process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - last resort
        process.kill()


def run_command(
    command: Sequence[str],
    *,
    cwd,
    timeout_seconds: float,
    max_output_bytes: int,
    env_extra: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one configured command with a timeout, output cap and tree kill.

    Raises :class:`CommandError` only when the command cannot be started at all
    (empty command, missing executable); a non-zero exit or a timeout is a
    normal, reported outcome.
    """
    args = list(command)
    if not args or not all(isinstance(part, str) and part for part in args):
        raise CommandError(
            "COMMAND_INVALID", "a configured command must be a non-empty argument list"
        )

    started = datetime.now(timezone.utc)
    monotonic_start = time.monotonic()
    try:
        process = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=sanitized_env(env_extra),
            start_new_session=sys.platform != "win32",
            shell=False,
        )
    except FileNotFoundError:
        raise CommandError(
            "COMMAND_NOT_FOUND", f"the configured command '{args[0]}' was not found"
        ) from None
    except OSError as exc:
        raise CommandError(
            "COMMAND_START_FAILED", f"the configured command could not start: {type(exc).__name__}"
        ) from None

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - pipes held open
            stdout, stderr = b"", b""

    duration = time.monotonic() - monotonic_start
    out_text, out_truncated = _cap(_decode(stdout or b""), max_output_bytes)
    err_text, err_truncated = _cap(_decode(stderr or b""), max_output_bytes)

    logger.info(
        "command finished",
        extra={
            "argv0": args[0],
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "duration_seconds": round(duration, 3),
        },
    )
    return CommandResult(
        command=args,
        exit_code=None if timed_out else process.returncode,
        timed_out=timed_out,
        duration_seconds=duration,
        stdout=out_text,
        stderr=err_text,
        truncated=out_truncated or err_truncated,
        started_at=started,
    )
