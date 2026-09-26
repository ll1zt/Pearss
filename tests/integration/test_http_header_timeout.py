"""The half-open request-header timeout (issue #9).

A client that sends a partial request header and then stops -- the slowloris
shape -- must not hold a connection open indefinitely. Measured on the deployed
host before this work: both the uvicorn socket and the tailscale serve hop kept
such a connection alive past **120 seconds** with no timeout in effect.

Scenario register (app-level; the deployment-layer attribution this issue asked
for first is written up in `docs/operations/tunnel-drill.md` §7):

  AC-25 半截请求头在 N 秒内被关闭
    Given 一个只发了部分请求头(未写结束空行)的 TCP 连接
    When 等待超过配置的 header 超时
    Then 服务端关闭该连接
    And 完全不发字节的空连接同样被关闭(那是最廉价的一种)

  AC-26 正常请求与 keep-alive 不受影响
    Given 同一服务
    When 客户端在超时内发完整请求, 或复用一条空闲的 keep-alive 连接
    Then 请求正常完成

  AC-27 超时可由环境变量配置, 且坏值不会把它关掉
    Given PEARSS_HTTP_HEADER_TIMEOUT_SECONDS 被设为某个值(或坏值)
    When 服务启动
    Then 该值生效; 坏值回落到默认而不是变成 0/inf

  AC-28 慢但持续发送的请求头不被误杀
    Given 一个分片缓慢发送, 但始终在推进的请求头
    When 总时长超过超时窗口, 每片间隔都在窗口内
    Then 请求完成 —— 超时针对「无进展」, 不是「总时长」

These drive a real uvicorn server on a real socket, in a thread, with the same
protocol class production installs via `--http`. The defect is a property of
the network layer -- a mocked transport could not reproduce it, and the
injection check depends on the real timer firing.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from app.http_timeout import HeaderTimeoutProtocol, header_timeout_seconds

pytestmark = pytest.mark.integration


_BODY = b'{"ok": true}'


async def _asgi_app(scope: Any, receive: Any, send: Any) -> None:
    """A trivial ASGI app: the timeout must be decided before any route runs.

    `content-length` is declared because the tests read raw sockets: without it
    uvicorn frames the response with chunked transfer-encoding, so a reader
    looking for the end of a body cannot tell a complete response from a partial
    one. Declaring the length keeps the framing out of the test's way.
    """
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_BODY)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _BODY})


@contextmanager
def _server() -> Iterator[tuple[str, int]]:
    """A real uvicorn server with the header timeout installed.

    Driven through `uvicorn.Server.run` in a thread over a real socket, so the
    class under test is the one production installs through uvicorn's `--http`
    seam rather than a stand-in.
    """
    import uvicorn

    config = uvicorn.Config(
        _asgi_app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    config.http = HeaderTimeoutProtocol
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            raise AssertionError("uvicorn failed to start")
        time.sleep(0.05)
    assert server.started, "uvicorn never started"
    listeners = list(getattr(server, "servers", []))
    assert listeners, "no listening socket"
    port = int(listeners[0].sockets[0].getsockname()[1])
    try:
        yield ("127.0.0.1", port)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _seconds_until_close(sock: socket.socket, budget: float) -> float | None:
    """Seconds until the peer closed, or None if it stayed open for `budget`."""
    sock.settimeout(budget)
    start = time.monotonic()
    try:
        while True:
            data = sock.recv(256)
            if not data:
                return time.monotonic() - start
            # Bytes mean the server answered rather than hung. That resolves the
            # wait too: what this test is about is that the connection is not
            # left open indefinitely.
            return time.monotonic() - start
    except TimeoutError:
        return None
    finally:
        sock.close()


# --------------------------------------------------------------------------
# AC-25: an incomplete header is closed
# --------------------------------------------------------------------------


def test_a_half_open_request_header_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-25: the defect itself, on a real socket.

    The budget is generous (a 1s deadline must fire inside 5s) so a scheduling
    hiccup on a shared runner cannot redden it, while a regression that removes
    the timer -- which the injection check verifies -- cannot pass it. The
    deadline is set explicitly because the *ordering* is what is asserted, and
    a test that waited on the production default would take as long as the
    default instead of proving the mechanism.
    """
    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "1")
    with _server() as (host, port):
        sock = socket.create_connection((host, port), timeout=10)
        sock.sendall(b"GET /p HTTP/1.1\r\nHost: x\r\nX-S: ")
        closed_after = _seconds_until_close(sock, budget=5.0)

    assert closed_after is not None, (
        "the server kept a half-open request header alive; it must close it "
        "within the configured header timeout"
    )


def test_a_connection_that_sends_nothing_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-25 (the cheapest attack): connect and never send anything.

    This is the shape that costs an attacker nothing at all -- no bytes, so a
    design that arms its deadline on the first byte would miss it entirely.
    Arming at `connection_made` is what covers it.
    """
    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "1")
    with _server() as (host, port):
        sock = socket.create_connection((host, port), timeout=10)
        closed_after = _seconds_until_close(sock, budget=5.0)

    assert closed_after is not None, (
        "a connection that never sends a byte must also be closed"
    )


def test_the_configured_timeout_is_what_bounds_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-25/AC-27: the value in the environment is the deadline that applies.

    Asserted as an ordering rather than an exact duration, because the server's
    timer and the test's clock are different clocks on a shared machine. With
    the deadline set to 1s the close must arrive well inside a 4s wait; with it
    set to 30s the same wait must expire first. A hardcoded deadline fails one
    of those two.
    """
    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "1")
    with _server() as (host, port):
        quick = socket.create_connection((host, port), timeout=10)
        quick.sendall(b"GET /p HTTP/1.1\r\nHost: x\r\n")
        closed_quickly = _seconds_until_close(quick, budget=4.0)

    assert closed_quickly is not None, "a 1s deadline must fire inside 4s"

    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "30")
    with _server() as (host, port):
        patient = socket.create_connection((host, port), timeout=10)
        patient.sendall(b"GET /p HTTP/1.1\r\nHost: x\r\n")
        closed_late = _seconds_until_close(patient, budget=3.0)

    assert closed_late is None, (
        "a 30s deadline must not have fired inside 3s; the timeout is not "
        "being read from the environment"
    )


# --------------------------------------------------------------------------
# AC-26: normal traffic is untouched
# --------------------------------------------------------------------------


def test_a_complete_request_is_served() -> None:
    """AC-26: the timeout must not cut connections that do send a request."""
    with _server() as (host, port):
        response = httpx.get(f"http://{host}:{port}/p", timeout=10)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}


def test_an_idle_keep_alive_connection_survives_and_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-26 (keep-alive): the deadline belongs to the request, not the socket.

    Asserted on the *socket*, not on a second response. An HTTP client that
    finds its pooled connection closed transparently opens a new one and
    returns 200, so "the second request succeeded" passes even when the timer
    was wrongly left armed -- measured, and it is exactly how the first version
    of this test failed to catch its own injection. Reuse is only proven by
    watching the same file descriptor carry both requests.

    The idle period is longer than the deadline, so a timer that was not
    disarmed must fire during it.
    """
    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "1")
    with _server() as (host, port):
        sock = socket.create_connection((host, port), timeout=15)
        try:
            request = f"GET /p HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
            sock.sendall(request)
            first = _read_one_response(sock)
            assert b"200" in first, first[:120]

            # Idle past the deadline, keeping the socket open the whole time.
            idle_until = time.monotonic() + 3.0
            while time.monotonic() < idle_until:
                time.sleep(min(0.25, max(0.0, idle_until - time.monotonic())))

            # If the timer was still armed it has closed this socket by now: the
            # write fails outright, or the read returns EOF.
            sock.sendall(request)
            second = _read_one_response(sock)
        finally:
            sock.close()

    assert b"200" in second, (
        "an idle keep-alive connection must survive and stay usable; a "
        "leftover header deadline closed it, which would present as flaky "
        f"client failures rather than as security. Got {second[:120]!r}"
    )


def _read_one_response(sock: socket.socket) -> bytes:
    """Read exactly one framed response (or as far as the peer allows).

    Bounded by the `content-length` the test app declares, so the caller can
    send a second request on the same socket and start at a clean boundary. A
    timeout returns whatever arrived, which is what the callers assert on.
    """
    received = b""
    sock.settimeout(10)
    try:
        while b"\r\n\r\n" not in received:
            chunk = sock.recv(4096)
            if not chunk:
                return received
            received += chunk
        head, _, body = received.partition(b"\r\n\r\n")
        expected = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                expected = int(value.strip())
        while len(body) < expected:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
        return head + b"\r\n\r\n" + body
    except TimeoutError:
        return received


def test_a_pipelined_pair_of_requests_is_served() -> None:
    """AC-26 (pipelining): two complete requests in one write are both served.

    h11 handles them from one `data_received`; a deadline armed per byte rather
    than per progress could cancel between them and drop the second response.
    """
    with _server() as (host, port):
        sock = socket.create_connection((host, port), timeout=10)
        sock.sendall(
            b"GET /p HTTP/1.1\r\nHost: x\r\n\r\nGET /p HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        sock.settimeout(15)
        received = b""
        try:
            while received.count(b"HTTP/1.1 200") < 2:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        except TimeoutError:
            pass
        finally:
            sock.close()

    assert received.count(b"HTTP/1.1 200") == 2, received[:200]


# --------------------------------------------------------------------------
# AC-27: configured, and safe when misconfigured
# --------------------------------------------------------------------------


def test_the_timeout_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-27: an operator can tune it without a code change."""
    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "37")
    assert header_timeout_seconds() == 37.0

    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "2.5")
    assert header_timeout_seconds() == 2.5


def test_the_default_is_a_real_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-27 (default): the protection does not require configuration.

    A default that must be set is a default that is off on every deployment
    whose operator did not read the release note.
    """
    monkeypatch.delenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", raising=False)
    value = header_timeout_seconds()

    assert value > 0
    assert value <= 60, "the default must actually bound a half-open header"


def test_an_unusable_setting_falls_back_rather_than_disabling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-27 (failure mode): a bad value must not switch the protection off.

    Every unusable spelling is checked, including the two that would disable a
    bound if passed through: `0` (fire immediately) and `inf`/`nan` (never
    fire). A typo in an environment file must not be a security regression.
    """
    for bad in ("", "abc", "0", "-5", "nan", "inf", "-inf", "1e400"):
        monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", bad)
        value = header_timeout_seconds()
        assert value > 0, f"{bad!r} must not disable the timeout"
        assert value <= 300, f"{bad!r} must not become effectively unbounded"


# --------------------------------------------------------------------------
# AC-28: progress, not total duration
# --------------------------------------------------------------------------


def test_a_slow_but_progressing_request_is_not_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-28: the deadline targets *no progress*, not total duration.

    A client on a slow link dribbling its header bytes is legitimate traffic,
    and a total-duration limit would sever it. Here the whole header takes well
    longer than the deadline while every gap stays inside it, so the request
    must be served. A deadline that is never reset fails this.
    """
    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "1")
    with _server() as (host, port):
        sock = socket.create_connection((host, port), timeout=10)
        try:
            header = b"GET /p HTTP/1.1\r\nHost: x\r\nX-Dribble: value\r\n\r\n"
            # 4 slices, 0.4s apart: total 1.2s > the 1s deadline, but each
            # gap is 0.4s < it.
            slices = [header[i : i + 4] for i in range(0, len(header), 4)]
            for piece in slices:
                sock.sendall(piece)
                # The gap *is* the experiment: short enough to stay inside the
                # deadline, long enough that the whole header exceeds it. Wait
                # by polling so the interval does not depend on one sleep
                # returning on time.
                gap_until = time.monotonic() + 0.4
                while time.monotonic() < gap_until:
                    time.sleep(min(0.05, max(0.0, gap_until - time.monotonic())))
            sock.settimeout(15)
            received = b""
            while b"200" not in received:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        finally:
            sock.close()

    assert b"200" in received, (
        f"a slow but progressing request must be served, got {received[:120]!r}"
    )


# --------------------------------------------------------------------------
# AC-27 (clamping) and the protocol's internal guards
#
# The clamping branch and the two early returns in `_on_header_deadline` are
# cheap to reach and are exactly where a "just in case" line would rot
# unnoticed, so they are driven directly rather than through a socket.
# --------------------------------------------------------------------------


def test_an_oversized_setting_is_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-27: an absurd value is bounded, not honoured.

    `99999` seconds is not a timeout in any useful sense; it is the protection
    switched off by another spelling. The clamp keeps the variable's promise
    ("bounded") true for every value an operator can write.
    """
    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "99999")
    value = header_timeout_seconds()

    assert value == 300.0, f"an oversized value must clamp, got {value}"


def _bare_protocol() -> HeaderTimeoutProtocol:
    """A protocol instance with no event loop and no real transport.

    Constructed through `__new__` so the deadline helpers can be driven
    directly: these are the branches a socket cannot arrange deterministically
    (a peer that vanishes between two reads, a timer firing just as a request
    lands), and each must be a no-op rather than an error inside a timer
    callback -- an exception there surfaces as an asyncio "never retrieved"
    noise line and can leave the connection unclosed in the case that matters.
    """
    return HeaderTimeoutProtocol.__new__(HeaderTimeoutProtocol)


class _ClosingTransport:
    """A transport that reports itself closing, recording any close() call."""

    def __init__(self) -> None:
        self.closes = 0

    def is_closing(self) -> bool:
        return True

    def close(self) -> None:
        self.closes += 1

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default


def test_the_deadline_handler_is_harmless_when_it_has_nothing_to_do() -> None:
    """The handler's early returns: a served request, or a transport already gone.

    Both are reachable in production -- a request that arrives as the deadline
    fires, and a peer that disconnected first -- and both must be no-ops.
    """
    # (a) A connection whose transport is already closing: nothing to close.
    closing = _ClosingTransport()
    protocol = _bare_protocol()
    protocol.transport = closing  # type: ignore[assignment]
    protocol._on_header_timeout()

    assert closing.closes == 0, (
        "a transport that is already closing must not be touched again"
    )

    # (b) No transport at all (the connection was lost before the timer ran).
    protocol = _bare_protocol()
    protocol.transport = None  # type: ignore[assignment]
    protocol._on_header_timeout()  # must not raise

    # (c) The handler leaves its own handle cleared, so a later arm starts from
    #     a known state rather than cancelling a stale one.
    assert protocol._header_deadline is None


def test_no_deadline_is_armed_once_the_transport_is_closing() -> None:
    """A closing transport must not get a timer.

    Reachable when a peer disconnects between two reads: `_update_deadline`
    then finds a transport that is already closing and must cancel rather than
    arm. Asserting the handle stays unset also pins that the cancel happened,
    which a leaked timer would hide.
    """
    protocol = _bare_protocol()
    protocol.transport = _ClosingTransport()  # type: ignore[assignment]
    protocol._header_deadline = None

    protocol._update_deadline()

    assert protocol._header_deadline is None, (
        "a closing transport must not have a header deadline armed"
    )


def test_a_partial_request_on_a_reused_connection_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-25 (keep-alive): a partial *second* request must be closed too.

    The hole this pins, found by an injection check that survived its first
    test: after an idle keep-alive socket was exempted, a client could send a
    complete request and then a partial second one and hold the connection open
    forever (measured, >4s beyond a 1s deadline). h11 reports IDLE in that state
    exactly as it does for a fresh connection, so a design that *latches* "this
    connection has served a request" stops protecting the connection it just
    decided to trust. Slowloris over a reused connection is the same attack with
    one complete request in front of it.

    The payload is load-bearing. `X-S: ` (a field with no value) is a header h11
    *rejects*, so uvicorn closes the connection for its own reason and this test
    passed without the deadline doing anything -- which is exactly how the first
    version failed to catch its injection. A start line with no terminating CRLF
    leaves h11 genuinely waiting, which is the state under test.
    """
    monkeypatch.setenv("PEARSS_HTTP_HEADER_TIMEOUT_SECONDS", "1")
    with _server() as (host, port):
        sock = socket.create_connection((host, port), timeout=15)
        try:
            request = f"GET /p HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
            sock.sendall(request)
            first = _read_one_response(sock)
            assert b"200" in first, first[:120]

            sock.sendall(b"GET /p HTTP/1.1\r\n")
            closed_after = _seconds_until_close(sock, budget=5.0)
        finally:
            sock.close()

    assert closed_after is not None, (
        "a partial request on a reused connection must be closed like any other "
        "incomplete header; leaving it open is slowloris with one complete "
        "request in front of it"
    )
