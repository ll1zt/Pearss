"""Elapsed HTTP budgets require real sockets, not a simulated clock."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic

import httpx
import pytest

from adapters.deadline_transport import DeadlineTransport
from adapters.http import FetchError, FetchPolicy, fetch
from core.enums import ErrorClass

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("phase", ["body", "headers", "redirects"])
def test_user_slow_stream_expires_and_releases_connection(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Given continuous chunks, when the budget expires, then fetch disconnects."""
    monkeypatch.setenv("PEARSS_ALLOW_PRIVATE_FETCH", "1")
    started = Event()
    stop = Event()
    disconnected = Event()

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/ok":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            started.set()
            try:
                if phase == "redirects":
                    hop = (
                        int(self.path.removeprefix("/hop/"))
                        if self.path.startswith("/hop/")
                        else 0
                    )
                    stop.wait(0.08)
                    self.send_response(302 if hop < 8 else 200)
                    if hop < 8:
                        self.send_header("Location", f"/hop/{hop + 1}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if phase == "headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    while not stop.is_set():
                        self.wfile.write(b"X-Progress: yes\r\n")
                        self.wfile.flush()
                        stop.wait(0.02)
                    self.wfile.write(b"Content-Length: 0\r\n\r\n")
                    self.wfile.flush()
                    return
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while not stop.is_set():
                    self.wfile.write(b"1\r\nx\r\n")
                    self.wfile.flush()
                    stop.wait(0.02)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        with (
            httpx.Client(
                transport=DeadlineTransport(trust_env=False), trust_env=False
            ) as client,
            ThreadPoolExecutor(1) as pool,
        ):
            before = monotonic()
            result = pool.submit(
                fetch,
                f"http://127.0.0.1:{server.server_port}/stream",
                client=client,
                policy=FetchPolicy(total_timeout=0.2, read_timeout=2.0),
            )
            try:
                assert started.wait(1), "origin never received the request"
                with pytest.raises(FetchError) as caught:
                    result.result(timeout=1.0)
                assert caught.value.error_class is ErrorClass.NETWORK
                assert monotonic() - before < 1.0
                if phase != "redirects":
                    assert disconnected.wait(1), "expired fetch retained its socket"
                assert (
                    fetch(
                        f"http://127.0.0.1:{server.server_port}/ok", client=client
                    ).body
                    == b"ok"
                )
            finally:
                stop.set()
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
