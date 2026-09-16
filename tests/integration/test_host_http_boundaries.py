"""User egress outcomes across real sockets and the child/host RPC boundary."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

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
