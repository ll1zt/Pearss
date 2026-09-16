"""User egress outcomes across real sockets and the child/host RPC boundary."""

from __future__ import annotations

import contextlib
import gzip
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from adapters.deadline_transport import DeadlineTransport
from adapters.http import FetchError
from app.plugin_runtime import HostClientTransport
from core.enums import ErrorClass

pytestmark = pytest.mark.integration


@pytest.fixture
def origin() -> Iterator[tuple[str, list[str]]]:
    visited: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            visited.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://localhost:{server.server_port}/forbidden"
                )
                body = b""
            else:
                self.send_response(200)
                body = b"safe content"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", visited
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_user_origin_cannot_redirect_host_into_forbidden_destination(
    origin: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given an allowed origin, when it redirects privately, then no target request."""
    from adapters import http

    # Explicit destination policy only; the HTTP exchange is real loopback TCP.
    monkeypatch.delenv("PEARSS_ALLOW_PRIVATE_FETCH", raising=False)
    monkeypatch.setattr(
        http,
        "resolve_host",
        lambda host: {"127.0.0.1" if host == "localhost" else "93.184.216.34"},
    )
    url, visited = origin
    with httpx.Client(follow_redirects=True, trust_env=False) as shared:
        transport = HostClientTransport(shared)
        with pytest.raises(FetchError) as refused:
            transport.handle_request(httpx.Request("GET", url + "/redirect"))
    assert refused.value.error_class == ErrorClass.BLOCKED
    assert visited == ["/redirect"]


@pytest.fixture
def body_origin() -> Iterator[tuple[str, threading.Event]]:
    release = threading.Event()
    limit = 5 * 1024 * 1024

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except ConnectionResetError:
                self.close_connection = True

        def do_GET(self) -> None:
            self.send_response(200)
            if self.path == "/unfinished":
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                chunk = b"x" * (64 * 1024)
                try:
                    for _ in range(81):
                        self.wfile.write(b"10000\r\n" + chunk + b"\r\n")
                    self.wfile.flush()
                    release.wait(10)
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/small":
                body = b"safe content"
            elif self.path == "/exact":
                body = b"x" * limit
            else:
                body = b"x" * (limit + 1)
            if self.path == "/compressed":
                body = gzip.compress(body)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("deadline_transport", [False, True], ids=["httpx", "lifespan"])
@pytest.mark.parametrize("path", ["/oversize", "/compressed", "/unfinished"])
def test_user_oversized_origin_is_rejected_before_completion_and_pool_recovers(
    body_origin: tuple[str, threading.Event],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    deadline_transport: bool,
) -> None:
    """Given an oversized body, when fetched, then refuse and free the connection.

    The unfinished origin withholds EOF until after the rejection assertion.
    Real sockets exercise streaming and pool release below the RPC boundary.
    """
    monkeypatch.setenv("PEARSS_ALLOW_PRIVATE_FETCH", "1")
    url, release = body_origin
    with (
        httpx.Client(
            trust_env=False,
            timeout=3,
            limits=httpx.Limits(max_connections=1),
            transport=DeadlineTransport(trust_env=False)
            if deadline_transport
            else None,
        ) as shared,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        transport = HostClientTransport(shared)
        future = executor.submit(
            transport.handle_request, httpx.Request("GET", url + path)
        )
        try:
            with pytest.raises(FetchError) as refused:
                future.result(timeout=2)
            assert refused.value.error_class == ErrorClass.NETWORK
            assert refused.value.detail.startswith("response exceeded 5242880 bytes")
            response = transport.handle_request(httpx.Request("GET", url + "/small"))
            assert response.status_code == 200
            assert response.content == b"safe content"
        finally:
            release.set()


def test_user_body_exactly_at_host_limit_is_preserved(
    body_origin: tuple[str, threading.Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given exactly 5 MiB, when fetched through the host, then retain every byte."""
    monkeypatch.setenv("PEARSS_ALLOW_PRIVATE_FETCH", "1")
    url, _ = body_origin
    with httpx.Client(trust_env=False) as shared:
        response = HostClientTransport(shared).handle_request(
            httpx.Request("GET", url + "/exact")
        )
    assert response.status_code == 200
    assert response.content == b"x" * (5 * 1024 * 1024)
    assert response.headers["content-length"] == "5242880"
