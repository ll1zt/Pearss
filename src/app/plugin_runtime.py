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
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from adapters.raw_payload import ConditionalState, RawPayload
from adapters.repository import PagingState
from core.enums import ErrorClass
from core.plugins import protocol
from core.plugins.hooks import BackfillHooks
from core.plugins.protocol import ProtocolError

if TYPE_CHECKING:
    from core.models import ItemDraft
    from plugins.collectors.twitter.walk import PagedWalkResult

LOGGER = logging.getLogger(__name__)

# Defaults, overridable per deployment via env (read by the factory).
DEFAULT_CALL_TIMEOUT_SECONDS = 120.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 15.0

# Variables every child needs to run Python and talk to its origin at all.
# Everything else in the host environment is withheld: a child must not be
# able to read another collector's credentials or the host's own secrets
# (database URLs, API tokens) by executing `os.environ`, however unlikely
# that misuse is.
_BASE_ENV_VARS: Final[frozenset[str]] = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "PYTHONHASHSEED"}
)
_SSL_ENV_PREFIXES: Final[tuple[str, ...]] = (
    "SSL_",
    "REQUESTS_CA_BUNDLE",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
# Credential/config prefixes each collector may see. CookieCloud is shared:
# zhihu and zsxq both resolve sessions through it.
_COLLECTOR_ENV_PREFIXES: Final[dict[str, tuple[str, ...]]] = {
    "telegram": (),
    "twitter": ("PEARSS_X_", "PEARSS_COOKIECLOUD_"),
    "zhihu": ("PEARSS_ZHIHU_", "PEARSS_COOKIECLOUD_"),
    "zsxq": ("PEARSS_ZSXQ_", "PEARSS_COOKIECLOUD_"),
    # The translate processor (P3) may run in a child; its provider config
    # arrives the same way collectors' credentials do.
    "translate": ("PEARSS_TRANSLATE_",),
}


def child_environment(collector_id: str) -> dict[str, str]:
    """The narrowed environment a sandboxed child may see.

    An allowlist, not a deny-list: base runtime variables, the TLS/proxy
    passthroughs, the collector's own credential prefixes, and the sandbox
    marker. The parent's PEARSS_* variables that name *other* collectors'
    credentials are exactly what this exists to withhold.
    """
    own_prefixes = _COLLECTOR_ENV_PREFIXES.get(collector_id, ())
    narrowed: dict[str, str] = {}
    for key, value in os.environ.items():
        if (
            key in _BASE_ENV_VARS
            or key.startswith(_SSL_ENV_PREFIXES)
            or key.startswith(own_prefixes)
        ):
            narrowed[key] = value
    narrowed["PEARSS_SANDBOXED"] = "1"
    return narrowed


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
        http_transport: httpx.BaseTransport | None = None,
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
        # The child never inherits the host environment wholesale: the env is
        # an allowlist (base runtime + this collector's credential prefixes),
        # so one compromised collector cannot read another's session or the
        # host's own secrets. An explicit dict overrides entries on top of
        # the allowlist; None is the plain allowlist.
        child_env = child_environment(self._collector_id)
        if self._env is not None:
            child_env = {**child_env, **self._env}

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
            # Inherit the host's stderr: the pipes are the protocol, but a
            # crashing child's traceback is diagnostics, and DEVNULL would
            # lose it while a host-read PIPE could deadlock on a full one.
            # The deployment's service manager (journald) collects it.
            stderr=None,
            env=child_env,
            preexec_fn=apply_limits if sys.platform != "win32" else None,
        )

    def set_http_transport(self, transport: httpx.BaseTransport | None) -> None:
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
            # The child is gone; its stdout pipe would leak a ResourceWarning
            # and, in a long-lived host, a file descriptor. (stderr is the
            # host's own stream, inherited at spawn.)
            if proc.stdout is not None:
                with contextlib.suppress(OSError, ValueError):
                    proc.stdout.close()
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

        deadline = time.monotonic() + timeout
        # Raw fd reads only: mixing them with the BufferedReader's methods
        # would let the same bytes land in whichever buffer is checked last
        # and tear the framing. Everything below reads `fd` exclusively.
        fd = stdout.fileno()

        def read_line() -> bytes:
            """One line, bounded by the deadline and the message-size cap.

            `select` alone only guards the first byte: a child that wrote
            half a line and hung would hold the host's lock forever with an
            unbounded `readline`, and a child flooding bytes without a
            newline would exhaust memory before any length check. Raw reads
            against the deadline bound both, and draining whatever the pipe
            already holds per call keeps the fast path a handful of syscalls
            rather than one per byte.
            """
            import selectors

            pending = b""
            total = 0
            while True:
                newline = pending.find(b"\n")
                if newline != -1:
                    return pending[:newline]
                total += len(pending)
                if total > protocol.MAX_MESSAGE_BYTES:
                    raise PluginChildError(
                        "child message exceeds the size cap (no newline)"
                    )
                chunks = [pending]
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PluginChildError(
                            f"plugin child {self._collector_id!r} timed out "
                            f"after {timeout}s"
                        )
                    selector = selectors.DefaultSelector()
                    try:
                        selector.register(fd, selectors.EVENT_READ)
                        if not selector.select(remaining):
                            raise PluginChildError(
                                f"plugin child {self._collector_id!r} timed "
                                f"out after {timeout}s"
                            )
                    finally:
                        selector.close()
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        raise PluginChildError(
                            f"plugin child {self._collector_id!r} closed "
                            f"stdout (rc={proc.poll()})"
                        )
                    chunks.append(chunk)
                    total += len(chunk)
                    newline = chunk.find(b"\n")
                    if newline != -1:
                        break
                joined = b"".join(chunks)
                newline = joined.find(b"\n")
                if newline != -1:
                    return joined[:newline]
                pending = joined

        while True:
            line = read_line()
            try:
                message = protocol.decode_message(line)
            except ProtocolError as exc:
                # Everything the child writes on stdout is protocol; a line
                # we cannot decode means the stream has lost framing (a
                # foreign print, a torn write). The buffer is desynchronised
                # past repair, so this ends the child like any other crash.
                raise PluginChildError(f"undecodable child message: {exc}") from exc
            if isinstance(message, protocol.ReverseCall):
                # A reverse "http" call: the child proxies its egress through
                # us (network whitelist lives at the host, E4).
                self._service_reverse_call(message)
                continue
            if not isinstance(message, protocol.Response):
                raise PluginChildError("unexpected message type from child")
            if message.id != request.id:
                # Same framing loss: an id that is not ours means the stream
                # is carrying someone else's answer, so nothing after this
                # line can be trusted either.
                raise PluginChildError(
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
                    # Pair lists, not dicts: Set-Cookie repeats, and folding
                    # it would silently drop every cookie after the first.
                    "headers": [
                        [k.decode(), v.decode()] for k, v in http_response.headers.raw
                    ],
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


class HostClientTransport(httpx.BaseTransport):
    """Present the host's `httpx.Client` as a transport for reverse calls.

    The lifespan owns one shared `httpx.Client` (connection pool, timeouts);
    the sandbox expects `httpx.BaseTransport.handle_request`. This adapter
    forwards into the client's send path. Egress policy note: the SSRF
    private-address guard lives in `adapters.http._request`, the path
    *host-initiated* fetches take; reverse-proxied child egress through this
    adapter is gated by whatever fronts the client (a proxy, a firewall), or
    by wrapping it in a policy transport at wiring time.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._client.send(request)
        response.read()
        # `send` returns content already decompressed, but the original
        # content-encoding header travels with it; a child-side httpx would
        # then try to gunzip plain bytes and die with zlib -3. Drop the
        # transfer-state headers and restate the true length.
        headers = [
            *(
                (name, value)
                for name, value in response.headers.raw
                if name.lower() not in (b"content-encoding", b"content-length")
            ),
            (b"content-length", str(len(response.content)).encode()),
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=response.content,
        )


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

        Late spawn is load-bearing: the child inherits the (narrowed)
        environment at fork time, and tests (monkeypatch.setenv) and
        deployments set credentials *after* this module is imported. A child
        forked at import would freeze an empty environment and answer every
        request `auth`. The fingerprint is of the environment the child
        would actually receive (`child_environment`), so a change to another
        collector's credentials correctly does not rotate this one.
        """
        current = child_environment(self._child._collector_id)
        if self._env_fingerprint is not None and current != self._env_fingerprint:
            LOGGER.info(
                "plugin child %s environment changed; rotating it",
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

    def source_config(self) -> dict[str, Any]:
        """The manifest as its surface dict (the child JSON-encodes it)."""
        result = self._ensure_running().call("source_config", {})
        return dict(result)

    def backfill(self) -> BackfillHooks | None:
        """The declared backward-walk capability, as an RPC-backed object.

        Returns a hooks object whose every method is an RPC call — the same
        shape the in-process collectors return — so the registry's
        `backfiller()` narrowing works unchanged over the sandbox. None
        means the origin has no backward walk.
        """
        self._ensure_running()
        if not bool(self._child.call("backfill", {})):
            return None
        return _SandboxedBackfillHooks(self._child)

    def fetch(
        self,
        url: str,
        *,
        client: httpx.Client,
        source_id: str,
        conditional: ConditionalState | None = None,
    ) -> RawPayload:
        """The FetchingCollector shape: the hookspec's fetch, over RPC.

        `client` is accepted for hook-signature uniformity and unused - the
        child's egress is host-proxied, so the caller's client never leaves
        this process.
        """
        del client
        from adapters.http import FetchError

        self._ensure_running()
        params: dict[str, Any] = {
            "url": url,
            "source_id": source_id,
            "conditional": (
                {"etag": conditional.etag, "last_modified": conditional.last_modified}
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
        return RawPayload.from_rpc_dict(result["raw"])

    def parse(self, raw: RawPayload) -> list[ItemDraft]:
        from core.models import ItemDraft

        self._ensure_running()
        result = self._child.call("parse", {"raw": raw.as_rpc_dict()})
        return [ItemDraft.from_rpc_dict(d) for d in result]

    def collect_pages(
        self,
        *,
        source_id: str,
        source_url: str,
        client: httpx.Client,
        state: PagingState,
        max_pages: int = 20,
    ) -> PagedWalkResult:
        """The paged-walk hook over RPC; failures come back as values.

        `client` is accepted for hook-signature uniformity and unused (the
        child's egress is host-proxied).
        """
        del client
        from core.enums import ErrorClass as _EC
        from plugins.collectors.twitter import walk

        self._ensure_running()
        try:
            return self._collect_pages_inner(
                source_id=source_id,
                source_url=source_url,
                state=state,
                max_pages=max_pages,
            )
        except ClassifiedChildError as exc:
            return walk.PagedWalkResult(
                drafts=[],
                newest_id=None,
                resume_cursor=None,
                error_class=_EC(exc.code),
                detail=exc.detail,
            )

    def _collect_pages_inner(
        self,
        *,
        source_id: str,
        source_url: str,
        state: PagingState,
        max_pages: int,
    ) -> PagedWalkResult:
        from core.models import ItemDraft
        from plugins.collectors.twitter import walk

        result = self._child.call(
            "collect_pages",
            {
                "source_id": source_id,
                "source_url": source_url,
                "state": {
                    "seen_frontier": state.seen_frontier,
                    "resume_cursor": state.resume_cursor,
                    "resume_rounds": state.resume_rounds,
                },
                "max_pages": max_pages,
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


class _SandboxedBackfillHooks(BackfillHooks):
    """BackfillHooks whose every method is one RPC call.

    Lives on the host, talks to the child's declared hooks object. It
    satisfies the registry's `backfiller()` narrowing (before_url +
    oldest_cursor) without the host importing the collector.
    """

    def __init__(self, child: PluginChild) -> None:
        self._child = child

    def before_url(self, url: str, cursor: str) -> str | None:
        result = self._child.call("before_url", {"url": url, "cursor": cursor})
        page_url = result.get("page_url")
        return str(page_url) if page_url is not None else None

    def oldest_cursor(self, drafts: list[ItemDraft]) -> str | None:

        result = self._child.call(
            "oldest_cursor",
            {"drafts": [draft.as_rpc_dict() for draft in drafts]},
        )
        cursor = result.get("cursor")
        return str(cursor) if cursor is not None else None
