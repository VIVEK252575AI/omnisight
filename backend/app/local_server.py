"""Serve a patched workspace on loopback for fresh evidence capture (W3.3).

Fresh verification screenshots must be taken from the *patched* copy, not from
the repository, so the workspace's ``source/`` directory is served on an
ephemeral loopback port for the duration of one capture.

Two modes, chosen by configuration:

* **built-in static server** (default) — an in-process
  :class:`http.server.ThreadingHTTPServer` bound to ``127.0.0.1:0``. Correct for
  the static ShopLite demo, and it cannot leak a process because there isn't
  one.
* **configured serve command** (``OMNISIGHT_SERVE_COMMAND`` plus
  ``OMNISIGHT_SERVE_URL``) — for a workspace that needs a real dev/preview
  server. It is started as an argument list with a sanitised environment and is
  always terminated *with its whole process tree* in ``finally``.

Readiness is proven by polling the URL until it answers, never by sleeping a
fixed time, and a server that never becomes ready is a bounded failure
(:class:`ServeError`), with the process killed before the error propagates.
"""

from __future__ import annotations

import functools
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator, Sequence
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from app.command_runner import _terminate_tree, sanitized_env
from app.config import Settings

logger = logging.getLogger("omnisight")

LOOPBACK_HOST = "127.0.0.1"
READY_POLL_INTERVAL_SECONDS = 0.05


class ServeError(RuntimeError):
    """A safe, coded failure to serve a workspace."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _QuietHandler(SimpleHTTPRequestHandler):
    """Static file handler that does not log requests or leak server paths."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        return

    def version_string(self) -> str:
        return "omnisight-workspace"


@dataclass
class ServedWorkspace:
    """A running workspace server."""

    base_url: str
    port: int | None
    mode: str
    _stop: Callable[[], None]

    def stop(self) -> None:
        self._stop()


class StaticFileServer:
    """In-process loopback static server for one directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> ServedWorkspace:
        if not self.root.is_dir():
            raise ServeError(
                "SERVE_ROOT_MISSING", "the workspace source directory does not exist"
            )
        handler = functools.partial(_QuietHandler, directory=str(self.root))
        try:
            server = ThreadingHTTPServer((LOOPBACK_HOST, 0), handler)
        except OSError as exc:
            raise ServeError(
                "SERVE_BIND_FAILED",
                f"could not bind a loopback port: {type(exc).__name__}",
            ) from None
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever, name="omnisight-workspace-server", daemon=True
        )
        thread.start()
        self._server, self._thread = server, thread
        port = server.server_address[1]
        logger.info("static server started", extra={"port": port})
        return ServedWorkspace(
            base_url=f"http://{LOOPBACK_HOST}:{port}",
            port=port,
            mode="in_process",
            _stop=self.stop,
        )

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> ServedWorkspace:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()


class ServeProcess:
    """A configured serve command, terminated with its process tree."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        base_url: str,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.command = list(command)
        self.cwd = Path(cwd)
        self.base_url = base_url.rstrip("/")
        self.env_extra = env_extra

    def start(self) -> ServedWorkspace:
        import subprocess

        if not self.command:
            raise ServeError("SERVE_COMMAND_EMPTY", "the serve command is empty")
        try:
            process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=sanitized_env(self.env_extra),
                shell=False,
            )
        except FileNotFoundError:
            raise ServeError(
                "SERVE_COMMAND_NOT_FOUND",
                f"the configured serve command '{self.command[0]}' was not found",
            ) from None
        except OSError as exc:
            raise ServeError(
                "SERVE_START_FAILED",
                f"the serve command could not start: {type(exc).__name__}",
            ) from None

        def stop() -> None:
            _terminate_tree(process)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

        return ServedWorkspace(
            base_url=self.base_url, port=None, mode="process", _stop=stop
        )


def wait_until_ready(base_url: str, *, timeout_seconds: float, path: str = "/") -> None:
    """Poll ``base_url`` until it answers, or fail after ``timeout_seconds``."""
    import time

    deadline = time.monotonic() + timeout_seconds
    url = base_url.rstrip("/") + path
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310 - loopback only
                if 200 <= response.status < 400:
                    return
                last_error = f"HTTP {response.status}"
        except (URLError, OSError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(READY_POLL_INTERVAL_SECONDS)
    raise ServeError(
        "SERVE_NOT_READY",
        f"the workspace server did not become ready within {timeout_seconds:g}s",
    )


def _loopback_only(base_url: str) -> str:
    """Refuse to serve/capture anything that is not loopback HTTP."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ServeError("SERVE_URL_INVALID", "the configured serve URL is not an HTTP URL")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ServeError(
            "SERVE_URL_NOT_LOOPBACK",
            "the capture URL must be loopback; remote targets are out of scope",
        )
    if parsed.username or parsed.password:
        raise ServeError("SERVE_URL_INVALID", "credentials are not allowed in the serve URL")
    return base_url.rstrip("/")


@contextmanager
def served_workspace(
    settings: Settings, source_dir: Path
) -> Iterator[ServedWorkspace]:
    """Serve ``source_dir`` for the duration of one capture, then stop it.

    The server is always stopped, including when readiness fails or the capture
    raises, so no port or process survives a verification attempt.
    """
    if settings.serve_command:
        if not str(settings.serve_url).strip():
            raise ServeError(
                "SERVE_URL_UNSET",
                "OMNISIGHT_SERVE_URL is required when OMNISIGHT_SERVE_COMMAND is set",
            )
        base_url = _loopback_only(settings.serve_url)
        served = ServeProcess(
            settings.serve_command, cwd=source_dir, base_url=base_url
        ).start()
        try:
            wait_until_ready(
                base_url, timeout_seconds=settings.serve_ready_timeout_seconds
            )
        except ServeError:
            served.stop()
            raise
        try:
            yield served
        finally:
            served.stop()
        return

    server = StaticFileServer(source_dir)
    served = server.start()
    try:
        wait_until_ready(
            served.base_url, timeout_seconds=settings.serve_ready_timeout_seconds
        )
        yield served
    finally:
        server.stop()
