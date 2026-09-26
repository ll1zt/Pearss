"""A test server must actually stop when its owner is torn down.

`server_close()` closes the listening socket. It does **not** stop
`serve_forever()`, which only returns once `shutdown()` has set its flag. So a
server torn down with `server_close()` alone leaves a thread looping on a closed
socket -- and it does not block there, it spins: measured at **89% of a core**,
per leaked server, for the rest of the process (issue #53).

That is not untidiness. Coverage traces every thread, so the spinning thread's
frames are attributed to whichever test is running, and every later test pays:

    test_items_api setup, median     before 1120 ms   after 63 ms
    the two files together           before 161 s     after 12 s

Two real origin classes had the bug (`_RecordingOrigin.close` in
test_fetch_politeness, `_Origin.stop` in the fulltext processor tests); the other
eight `serve_forever()` sites already called `shutdown()`. These tests exercise
the real classes rather than a toy server, because a hand-written stand-in would
have been written with the same misunderstanding and would prove nothing.

The assertion is `join()` + `is_alive()` rather than a CPU measurement:
`serve_forever` polls its flag every 0.5s, so a one-second join distinguishes
"stopped" from "still spinning" deterministically. Sampling CPU would make this
flaky on a loaded shared runner.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# serve_forever's poll_interval defaults to 0.5s, so 1s is two cycles: enough
# for a stopped loop to be observably gone, and too short for a spinning one to
# ever look stopped.
_JOIN_SECONDS = 1.0


def test_the_politeness_origin_stops_its_server_thread() -> None:
    """Given a real `_RecordingOrigin`, When it is closed,
    Then its serve_forever thread has exited within the poll interval.

    Fails on the pre-fix code, where `close()` called only `server_close()` and
    the thread stayed alive (and spinning) for the rest of the session.
    """
    from tests.integration.test_fetch_politeness import _RecordingOrigin

    origin = _RecordingOrigin()
    try:
        # The server is genuinely up before the teardown is judged.
        assert origin._thread.is_alive(), "the origin never started serving"
    finally:
        origin.close()

    origin._thread.join(timeout=_JOIN_SECONDS)
    assert not origin._thread.is_alive(), (
        "the origin's serve_forever thread outlived close(); it spins on the "
        "closed socket and every later test in this process pays for it "
        "(issue #53)"
    )


def test_the_fulltext_processor_origin_stops_its_server_thread() -> None:
    """Given a real fulltext `_Origin`, When it is stopped,
    Then its serve_forever thread has exited within the poll interval.

    The second of the two sites that had the bug. Kept separate rather than
    parametrized because the two classes are unrelated -- they live in different
    test trees and share only the shape of the mistake.
    """
    from plugins.processors.fulltext.test_processor import _Origin

    origin = _Origin()
    try:
        assert origin._thread.is_alive(), "the origin never started serving"
    finally:
        origin.stop()

    origin._thread.join(timeout=_JOIN_SECONDS)
    assert not origin._thread.is_alive(), (
        "the fulltext origin's serve_forever thread outlived stop(); it spins "
        "on the closed socket and every later test in this process pays for it "
        "(issue #53)"
    )


def test_shutdown_is_what_makes_the_thread_stop() -> None:
    """Given the two teardowns, When compared, Then the difference is `shutdown()`.

    This pins the mechanism rather than the symptom, using the standard library
    directly: `server_close()` alone leaves the loop running, and adding
    `shutdown()` is what ends it. Without this, a future edit could restore the
    bug in a *new* server helper and the two tests above would not necessarily
    catch it -- this states what the fix actually is.
    """
    import socket
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # pragma: no cover - never reached
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    def start() -> tuple[ThreadingHTTPServer, threading.Thread]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Wait until the loop is actually serving before judging a teardown --
        # bounded polling that fails loudly, not a fixed sleep that would both
        # waste time and hide a server that never came up.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                with socket.create_connection(
                    ("127.0.0.1", server.server_port), timeout=0.2
                ):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("the test server never started accepting connections")
                time.sleep(0.01)
        return server, thread

    # close() alone: the thread keeps looping.
    leaked, leaked_thread = start()
    leaked.server_close()
    leaked_thread.join(timeout=_JOIN_SECONDS)
    still_running = leaked_thread.is_alive()

    # shutdown() first: it stops.
    stopped, stopped_thread = start()
    stopped.shutdown()
    stopped.server_close()
    stopped_thread.join(timeout=_JOIN_SECONDS)
    exited = not stopped_thread.is_alive()

    # Both settled before asserting, so the leak is not left behind by the
    # test that documents it.
    if still_running:
        leaked.shutdown()
        leaked_thread.join(timeout=_JOIN_SECONDS)

    assert still_running, (
        "server_close() alone now stops the loop, so this test no longer "
        "describes the library's behaviour -- re-check what the fix relies on"
    )
    assert exited, (
        "shutdown() + server_close() did not stop the loop; the fix in the two "
        "origins would not work either"
    )
