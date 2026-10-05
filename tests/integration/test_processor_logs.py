"""A processor's own logs reach the host's journal (issue #229).

The child's stderr went to /dev/null. The translate processor's fallback line --
"translate model m1 failed (network): ...; falling back to m2" -- was written on
every failure and read by nobody, which is why "which model is broken?" could
only be answered by guessing. The symptom was zero lines in the journal, not a
failing test, which is exactly the kind of gap a test has to hold.

What is asserted is the deliverable: the host's log carries the child's line
with the row's context, and a processor cannot flood the journal with it.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from app.processor_runtime import run_processor
from plugins.processors.translate import processor as translate_processor

pytestmark = pytest.mark.integration

PRIMARY, FALLBACK = "primary", "fallback"


class Chatty:
    """An importable processor that logs, the way the chain walk does."""

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        logging.getLogger("fixtures.chatty").warning(
            "translate model %s failed (network): boom; falling back to %s",
            PRIMARY,
            FALLBACK,
        )
        return str(store("<p>translated</p>"))


class Flood:
    """A processor that logs like something stuck in a loop."""

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        log = logging.getLogger("fixtures.flood")
        for index in range(1000):
            log.warning("line %d", index)
        return str(store("<p>translated</p>"))


@pytest.fixture
def _provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The usual two-model world: the first model refuses, the second answers."""

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = str(body["model"])
            if model == PRIMARY:
                raw = json.dumps({"error": {"message": "overloaded"}}).encode()
                self.send_response(503)
            else:
                raw = json.dumps(
                    {"choices": [{"message": {"content": f"[{model}] translated"}}]}
                ).encode()
                self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "PEARSS_TRANSLATE_BASE_URL", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "fixture-not-secret")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODELS", f"{PRIMARY},{FALLBACK}")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODEL", raising=False)
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


def _run(processor: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    import time

    return run_processor(
        processor,
        plugin_id="translate",
        item_ctx={
            "item_id": "i-229",
            "title": "T",
            "content": "<p>English prose that needs translating.</p>",
            "url": "https://logs.example/article",
        },
        target_lang="zh",
        deadline=time.monotonic() + 30.0,
        cancelled=threading.Event(),
    )


def test_the_processor_s_fallback_line_reaches_the_host_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _provider: None,
) -> None:
    """AC-231: which model failed, why, and where it fell back to is in the log.

    This is the real processor, not a fixture that logs: the line under test is
    the one the deployed journal never had.
    """
    with caplog.at_level(logging.WARNING):
        _run(translate_processor, monkeypatch)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "[translate i-229]" in message
        and f"translate model {PRIMARY} failed" in message
        and f"falling back to {FALLBACK}" in message
        for message in messages
    ), messages
    # The level survives the crossing: a warning forwarded as information would
    # be invisible to anything that alerts on warnings.
    assert {
        record.levelno
        for record in caplog.records
        if "falling back" in record.getMessage()
    } == {logging.WARNING}, [record.levelname for record in caplog.records]


def test_a_flooding_processor_cannot_fill_the_journal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A processor logging in a loop must not be able to outgrow the journal.

    The child's stderr is a pipe the host drains, so an unbounded forwarder
    would be a journal with a thousand lines per row -- and the row prefix is
    what would make them look like the host's own.
    """
    with caplog.at_level(logging.WARNING):
        _run(Flood, monkeypatch)

    forwarded = [r.getMessage() for r in caplog.records if "line " in r.getMessage()]
    assert forwarded, "nothing was forwarded at all"
    assert len(forwarded) <= 250, f"{len(forwarded)} lines reached the journal"
    assert any("truncated" in r.getMessage() for r in caplog.records), (
        "a dropped tail must say so, or the log silently lies"
    )


class TwoLines:
    """A processor that writes two lines, to pin one id across a whole run."""

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        log = logging.getLogger("fixtures.two_lines")
        log.warning("first line")
        log.warning("second line")
        return str(store("<p>translated</p>"))


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Undo configure_logging's process-global effects after the test."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    access = logging.getLogger("uvicorn.access")
    access_level = access.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    access.setLevel(access_level)


@pytest.mark.integration
def test_a_forwarded_processor_line_carries_the_request_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
    _restore_logging: None,
) -> None:
    """AC-293: the re-logged line names the request that started the child.

    Rendered, not only inspected: the correlation id reaches the line through
    the handler's filter, so the deliverable is the text in the journal.
    """
    from adapters import correlation
    from app import log_setup

    stream = io.StringIO()
    log_setup.configure_logging(level="DEBUG", stream=stream)
    token = correlation.set_correlation_id("req-7")
    try:
        _run(TwoLines, monkeypatch)
    finally:
        correlation.reset(token)

    lines = [line for line in stream.getvalue().splitlines() if "first line" in line]
    assert len(lines) == 1, stream.getvalue()
    assert "req-7" in lines[0]
    assert "[translate i-229]" in lines[0]


@pytest.mark.integration
def test_processor_lines_share_one_id_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _restore_logging: None,
) -> None:
    """AC-294: a background sweep still has one id for the whole child run.

    With no request there is no ambient id. Before the child's id was captured
    at spawn, the handler's filter generated a fresh one for every forwarded
    line, so a single processor's two lines looked like two different jobs'.
    """
    from app import log_setup

    stream = io.StringIO()
    log_setup.configure_logging(level="DEBUG", stream=stream)
    with caplog.at_level(logging.WARNING):
        _run(TwoLines, monkeypatch)

    forwarded = [
        record
        for record in caplog.records
        if "first line" in record.getMessage() or "second line" in record.getMessage()
    ]
    assert len(forwarded) == 2, [r.getMessage() for r in forwarded]
    ids = {getattr(record, "correlation_id", None) for record in forwarded}
    assert len(ids) == 1, ids
    assert ids != {None} and ids != {""}, ids
