"""The sandbox runner: hosts one collector plugin in a child process.

For each third-party collector (ADR 0017: telegram/twitter/zhihu/zsxq), the
host keeps a long-lived child spawned via `python -m plugins.collectors
<id>` and proxies hook calls to it over the frozen RPC protocol. The child
owns the fragile, origin-specific knowledge; a crash, hang, or memory leak
in it costs the host nothing.

Lifecycle:
- `start()` spawns the child and pings it (a child that cannot serve its
  collector dies at spawn; a missing ping means a broken environment).
- Calls go through `call()`, which serialises per plugin and treats a
  transport failure as `network`.
- `stop()` closes stdin and reaps the child; `restart()` covers the
  crash-recovery path and the future `:reload` operation.

Resource limits are applied at spawn via `resource.setrlimit` in the child
(CPU seconds, address space); network egress is the child's own business —
the host never proxies plugin fetches in P2b, which keeps the trust
boundary honest: the child cannot reach anything the host machine cannot,
and it sees only the config the host injected at spawn.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any

from core.plugins import protocol
from core.plugins.protocol import ProtocolError

LOGGER = logging.getLogger(__name__)

# Defaults, overridable per deployment via env (read by the factory).
DEFAULT_CALL_TIMEOUT_SECONDS = 120.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxLimits:
    """Resource ceilings applied to the child at spawn (E4).

    CPU is enforced via RLIMIT_CPU everywhere. Address-space limits are
    applied only where the platform honours them at fork time — macOS raises
    in preexec_fn for RLIMIT_AS (Python's allocator reserves large virtual
    ranges), so memory capping is Linux-only; on macOS the CPU ceiling and
    the child's own isolation are the practical bounds.
    """

    cpu_seconds: int = 300
    memory_mb: int = 512


class PluginChildError(Exception):
    """The child is unusable: crashed, hung, or answered unintelligibly."""


class PluginChild:
    """One collector plugin, isolated in its own process."""

    def __init__(
        self,
        collector_id: str,
        *,
        env: dict[str, str] | None = None,
        limits: SandboxLimits | None = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._collector_id = collector_id
        self._env = env
        self._limits = limits or SandboxLimits()
        self._call_timeout = call_timeout
        self._startup_timeout = startup_timeout
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 0

    # -- spawn / shutdown --------------------------------------------------

    def _spawn(self) -> subprocess.Popen[bytes]:
        limits = self._limits

        def apply_limits() -> None:  # runs inside the forked child
            import resource

            resource.setrlimit(
                resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds)
            )
            if sys.platform == "linux":
                # Linux honours RLIMIT_AS at fork; macOS raises (see
                # SandboxLimits) and the CPU cap stays the practical bound.
                mem_bytes = limits.memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

        return subprocess.Popen(  # noqa: S603 - argv is host-built, not input
            [sys.executable, "-m", "plugins.collectors", self._collector_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._env,
            preexec_fn=apply_limits if sys.platform != "win32" else None,
        )

    def start(self) -> None:
        """Spawn the child and verify it serves its collector."""
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = self._spawn()
        try:
            result = self._roundtrip("ping", {}, timeout=self._startup_timeout)
        except PluginChildError:
            self.stop()
            raise
        if not result.get("alive"):
            self.stop()
            raise PluginChildError(
                f"plugin child {self._collector_id!r} failed its startup ping"
            )
        LOGGER.info(
            "plugin child %s started (pid %s)", self._collector_id, self._proc.pid
        )

    def stop(self) -> None:
        """Shut the child down. Idempotent; safe on an already-dead child."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOGGER.error("plugin child %s would not die", self._collector_id)
        finally:
            # The child is gone; its pipe fds leak ResourceWarnings and, in a
            # long-lived host, file descriptors.
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError, ValueError):
                        pipe.close()
        LOGGER.info("plugin child %s stopped", self._collector_id)

    def restart(self) -> None:
        """Crash recovery and the future :reload path share this."""
        self.stop()
        self.start()

    # -- calls ---------------------------------------------------------------

    def _roundtrip(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise PluginChildError(
                f"plugin child {self._collector_id!r} is not running"
            )
        self._next_id += 1
        request = protocol.Request(id=self._next_id, method=method, params=params)
        stdin, stdout = proc.stdin, proc.stdout
        if stdin is None or stdout is None:
            raise PluginChildError(f"plugin child {self._collector_id!r} has no pipes")
        try:
            stdin.write(request.encode() + b"\n")
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise PluginChildError(f"child stdin closed: {exc}") from exc

        def read_line() -> bytes:
            import selectors

            selector = selectors.DefaultSelector()
            selector.register(stdout, selectors.EVENT_READ)
            ready = selector.select(timeout)
            if not ready:
                raise PluginChildError(
                    f"plugin child {self._collector_id!r} timed out after {timeout}s"
                )
            line: bytes = stdout.readline()
            return line

        line = read_line()
        if not line:
            raise PluginChildError(
                f"plugin child {self._collector_id!r} closed stdout (rc={proc.poll()})"
            )
        if len(line) > protocol.MAX_MESSAGE_BYTES:
            raise PluginChildError("child message exceeds the size cap")
        response = protocol.decode_response(line)
        if response.id != request.id:
            raise ProtocolError(
                f"response id {response.id} does not match request {request.id}"
            )
        if response.error is not None:
            raise PluginChildError(
                f"{response.error.get('code', 'error')}: "
                f"{response.error.get('message', '')}"
            )
        result: dict[str, Any] = response.result
        return result

    def call(self, method: str, params: dict[str, Any]) -> Any:
        """One serialized call; a crashed child is restarted once and retried.

        Serialization is deliberate: collectors own no locking of their own,
        and one request per plugin matches how the scheduler walks sources.
        """
        with self._lock:
            try:
                return self._roundtrip(method, params, timeout=self._call_timeout)
            except PluginChildError:
                LOGGER.warning(
                    "plugin child %s failed; restarting once", self._collector_id
                )
                self.restart()
                return self._roundtrip(method, params, timeout=self._call_timeout)

    # -- collector-shaped conveniences -------------------------------------

    def match_url(self, url: str) -> bool:
        return bool(self.call("match_url", {"url": url}))

    def source_config(self) -> dict[str, Any]:
        payload = self.call("source_config", {})
        return dict(payload)
