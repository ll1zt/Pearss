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
(CPU seconds, address space). Network egress is host-proxied: the child's
HTTP requests ride back over the RPC stream as reverse calls and run through
a transport the host injects (`set_http_transport` / the registry's
`for_each_sandboxed_collector`). The app's lifespan wires the shared client
there, so the SSRF private-address guard and the fetch bounds apply to
sandboxed egress exactly as they do to the host's own fetches. The child
sees only the environment the host injected at spawn.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any

from core.enums import ErrorClass
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


class ClassifiedChildError(Exception):
    """A classified collection failure reported by the child (a value).

    Carries an ErrorClass code and detail — the host maps it back onto
    CollectionResult without restarting the child, because the child is
    healthy: it was the *origin* that refused.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


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
        http_transport: Any = None,
    ) -> None:
        self._collector_id = collector_id
        self._env = env
        self._limits = limits or SandboxLimits()
        self._call_timeout = call_timeout
        self._startup_timeout = startup_timeout
        # httpx transport (e.g. a MockTransport) the child's egress is
        # proxied through; None = the child speaks HTTP directly.
        self._http_transport = http_transport
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 0

    # -- spawn / shutdown --------------------------------------------------

    def _spawn(self) -> subprocess.Popen[bytes]:
        limits = self._limits
        # None = inherit the host environment (which carries the injected
        # credentials the tests and the deployment set); an explicit dict
        # replaces it entirely — the credential-narrowing step composes with
        # `os.environ` explicitly.
        child_env = (
            {**os.environ, "PEARSS_SANDBOXED": "1"}
            if self._env is None
            else {**os.environ, **self._env}
        )

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
            env=child_env,
            preexec_fn=apply_limits if sys.platform != "win32" else None,
        )

    def set_http_transport(self, transport: Any) -> None:
        """Swap the egress transport; applies to the next spawned child."""
        self._http_transport = transport

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

        while True:
            line = read_line()
            if not line:
                raise PluginChildError(
                    f"plugin child {self._collector_id!r} closed stdout "
                    f"(rc={proc.poll()})"
                )
            if len(line) > protocol.MAX_MESSAGE_BYTES:
                raise PluginChildError("child message exceeds the size cap")
            message = protocol.decode_message(line)
            if isinstance(message, protocol.ReverseCall):
                # A reverse "http" call: the child proxies its egress through
                # us (network whitelist lives at the host, E4).
                self._service_reverse_call(message)
                continue
            if not isinstance(message, protocol.Response):
                raise PluginChildError("unexpected message type from child")
            if message.id != request.id:
                raise ProtocolError(
                    f"response id {message.id} does not match request {request.id}"
                )
            if message.error is not None:
                code = str(message.error.get("code", "error"))
                text = str(message.error.get("message", ""))
                if code in {
                    "auth",
                    "blocked",
                    "network",
                    "parse",
                    "structure_changed",
                }:
                    # A classified origin failure: the child is healthy (the
                    # origin refused, the payload moved, or the proxied
                    # egress could not connect), so a restart would only
                    # reproduce it. A value, not a child fault.
                    raise ClassifiedChildError(code, text)
                raise PluginChildError(f"{code}: {text}")
            result: dict[str, Any] = message.result
            return result

    def _service_reverse_call(self, call: protocol.ReverseCall) -> None:
        """Answer a child's reverse "http" call through the injected transport."""
        import base64

        params = call.params
        if self._http_transport is None:
            error = protocol.error_result(
                "egress_denied", "the child has no host-proxied network"
            )
        else:
            try:
                import httpx

                request = httpx.Request(
                    method=str(params.get("method", "GET")),
                    url=str(params.get("url", "")),
                    headers=[
                        (str(k).encode(), str(v).encode())
                        for k, v in params.get("headers") or []
                    ],
                    content=base64.b64decode(str(params.get("content_b64", ""))),
                )
                http_response = self._http_transport.handle_request(request)
                response_body = {
                    "status": http_response.status_code,
                    "headers": {
                        k.decode(): v.decode() for k, v in http_response.headers.raw
                    },
                    "body_b64": base64.b64encode(http_response.content).decode("ascii"),
                }
                error = None
            except Exception as exc:  # transport-level failure
                # A policy refusal (the egress transport's SSRF guard raises
                # FetchError) keeps its classification; anything else is a
                # plain transport failure. Both are answered as *values* —
                # the child stays healthy either way.
                from adapters.http import FetchError

                if isinstance(exc, FetchError):
                    error = protocol.error_result(exc.error_class.value, exc.detail)
                else:
                    error = protocol.error_result(
                        "network", f"{type(exc).__name__}: {exc}"
                    )
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 0}
        payload["result" if error is None else "error"] = (
            response_body if error is None else error
        )
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(json.dumps(payload).encode() + b"\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            LOGGER.warning("child stdin closed while servicing a reverse call")

    def call(self, method: str, params: dict[str, Any]) -> Any:
        """One serialized call; a crashed child is restarted once and retried.

        Serialization is deliberate: collectors own no locking of their own,
        and one request per plugin matches how the scheduler walks sources.
        """
        with self._lock:
            try:
                return self._roundtrip(method, params, timeout=self._call_timeout)
            except ClassifiedChildError:
                # A classified origin failure: the child is healthy, restart
                # would just reproduce it.
                raise
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

    def backfill(self) -> bool:
        """Whether the child declares the backward-walk capability."""
        return bool(self.call("backfill", {}))


class SandboxedCollector:
    """The collector-shaped proxy over a sandboxed child process.

    Satisfies the same surface as the in-process collector modules, so the
    registry can hand either to the pipeline and routes without branching:
    match_url/parse/fetch/collect_pages/source_config become RPC calls. The
    child owns the credentials (injected at spawn) and the origin-specific
    code; the host never imports either.
    """

    def __init__(self, child: PluginChild) -> None:
        self._child = child
        self._env_fingerprint: dict[str, str] | None = None

    def _ensure_running(self) -> PluginChild:
        """Spawn on first use, and respawn if the credential env changed.

        Late spawn is load-bearing: the child inherits the host environment
        at fork time, and tests (monkeypatch.setenv) and deployments set
        credentials *after* this module is imported. A child forked at
        import would freeze an empty environment and answer every request
        `auth`. Comparing a fingerprint of the PEARSS_* environment catches
        that change and rotates the child.
        """
        current = {
            key: value for key, value in os.environ.items() if key.startswith("PEARSS_")
        }
        if self._env_fingerprint is not None and current != self._env_fingerprint:
            LOGGER.info(
                "PEARSS_* environment changed; rotating plugin child %s",
                self._child._collector_id,
            )
            self._child.restart()
        self._env_fingerprint = current
        self._child.start()
        return self._child

    def start(self) -> None:
        self._ensure_running()

    def stop(self) -> None:
        self._child.stop()

    def restart(self) -> None:
        self._child.restart()

    def collector_id(self) -> str:
        result = self._ensure_running().call("ping", {})
        return str(result["collector_id"])

    def match_url(self, url: str) -> bool:
        return bool(self._ensure_running().call("match_url", {"url": url}))

    def source_config(self) -> Any:
        return self._ensure_running().call("source_config", {})

    def backfill(self) -> Any:
        return self._ensure_running().call("backfill", {})

    def fetch(self, url: str, **kwargs: Any) -> Any:
        from adapters.http import FetchError
        from adapters.raw_payload import RawPayload

        self._ensure_running()
        conditional = kwargs.get("conditional")
        params: dict[str, Any] = {
            "url": url,
            "source_id": str(kwargs.get("source_id", "")),
            "conditional": (
                {
                    "etag": conditional.etag,
                    "last_modified": conditional.last_modified,
                }
                if conditional is not None
                else None
            ),
        }
        try:
            result = self._child.call("fetch", params)
        except ClassifiedChildError as exc:
            # Same failure contract as the in-process path: a classified
            # origin failure surfaces as FetchError, and pipeline's existing
            # handling records it on the run.
            raise FetchError(ErrorClass(exc.code), exc.detail) from exc
        if result.get("error"):
            raise FetchError(
                ErrorClass(str(result["error"]["code"])),
                str(result["error"]["message"]),
            )
        return RawPayload.from_rpc_dict(result["raw"])

    def parse(self, raw: Any) -> Any:
        from core.models import ItemDraft

        self._ensure_running()
        result = self._child.call("parse", {"raw": raw.as_rpc_dict()})
        return [ItemDraft.from_rpc_dict(d) for d in result]

    def collect_pages(self, **kwargs: Any) -> Any:
        from core.enums import ErrorClass as _EC
        from plugins.collectors.twitter import walk  # re-exported result shape

        self._ensure_running()
        try:
            return self._collect_pages_inner(**kwargs)
        except ClassifiedChildError as exc:
            return walk.PagedWalkResult(
                drafts=[],
                newest_id=None,
                resume_cursor=None,
                error_class=_EC(exc.code),
                detail=exc.detail,
            )

    def _collect_pages_inner(self, **kwargs: Any) -> Any:
        from core.enums import ErrorClass
        from core.models import ItemDraft
        from plugins.collectors.twitter import walk

        result = self._child.call(
            "collect_pages",
            {
                "source_id": str(kwargs["source_id"]),
                "source_url": str(kwargs["source_url"]),
                "state": {
                    "seen_frontier": kwargs["state"].seen_frontier,
                    "resume_cursor": kwargs["state"].resume_cursor,
                    "resume_rounds": kwargs["state"].resume_rounds,
                },
                "max_pages": int(kwargs.get("max_pages", 20)),
            },
        )
        return walk.PagedWalkResult(
            drafts=[ItemDraft.from_rpc_dict(d) for d in result["drafts"]],
            newest_id=result["newest_id"],
            resume_cursor=result["resume_cursor"],
            error_class=(
                ErrorClass(result["error_class"]) if result.get("error_class") else None
            ),
            detail=result.get("detail"),
        )
