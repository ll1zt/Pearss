"""What a failed attempt leaves behind for the next one (issue #232).

A row that ran out of budget used to start from its first block on the next
attempt: the same outage, paid for once per block. The processor now keeps each
finished block in a scratch directory the runtime hands it, and the runtime
removes that directory once the row succeeds.

Two halves, because each is useless without the other: a cache that did not
survive a failure would never be read, and one that survived success would be
read by nobody while quietly accumulating.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.processor_runtime import run_processor
from core.plugins.hooks import ProcessorError
from plugins.processors.translate import processor as translate_processor

pytestmark = pytest.mark.integration

BODY = "<p>prose one.</p><p>prose two.</p><p>prose three.</p>"


@contextlib.contextmanager
def _provider(
    monkeypatch: pytest.MonkeyPatch, *, fail_on: str = ""
) -> Iterator[list[str]]:
    """A provider that answers every block but the one matching `fail_on`."""
    asked: list[str] = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = str(body["messages"][1]["content"])
            asked.append(text)
            if fail_on and fail_on in text:
                raw = json.dumps({"error": {"message": "overloaded"}}).encode()
                self.send_response(503)
            else:
                raw = json.dumps(
                    {"choices": [{"message": {"content": f"[{text}]"}}]}
                ).encode()
                self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setenv(
        "PEARSS_TRANSLATE_BASE_URL", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "fixture-not-secret")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "fixture")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODELS", raising=False)
    try:
        yield asked
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _run(item_id: str) -> str:
    return run_processor(
        translate_processor,
        plugin_id="translate",
        item_ctx={
            "item_id": item_id,
            "title": "T",
            "content": BODY,
            "url": "https://cache.example/article",
            "lang": "zh",
        },
        target_lang="zh",
        deadline=time.monotonic() + 30.0,
        cancelled=threading.Event(),
    )


def _cache_dir(tmp_path: Path, item_id: str) -> Path:
    return tmp_path / "storage" / "processor-cache" / "translate" / "zh" / item_id


def test_a_failed_row_keeps_its_finished_blocks_for_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-234: the cache has to outlive the attempt that failed, or it is useless.

    Also asserts the write is atomic: a `.part` file the next attempt could read
    as a finished translation must never be left behind.
    """
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))

    with (
        _provider(monkeypatch, fail_on="prose two") as asked,
        pytest.raises(ProcessorError),
    ):
        _run("i-232-a")

    assert asked, "sanity: the provider was reached"
    kept = sorted(path.name for path in _cache_dir(tmp_path, "i-232-a").iterdir())
    assert kept, "the block that did succeed was kept for the next attempt"
    assert not any(name.endswith(".part") for name in kept), kept


def test_a_successful_row_takes_its_cache_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-235: the cache is not durable state, and it does not survive success.

    Asserted on the filesystem *and* on the schema: no file left behind, no table
    named for it, no column added to the row it would have had to live in.
    """
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    with _provider(monkeypatch) as asked:
        _run("i-232-b")

    assert asked, "sanity: the provider was reached"
    assert not _cache_dir(tmp_path, "i-232-b").exists(), "the cache outlived the row"

    engine = sa.create_engine(migrated_database(tmp_path, "cache-schema.db"))
    try:
        inspector = sa.inspect(engine)
        assert not [name for name in inspector.get_table_names() if "cache" in name]
        assert not [
            column["name"]
            for column in inspector.get_columns("item_enrichment")
            if "cache" in column["name"] or "partial" in column["name"]
        ]
    finally:
        engine.dispose()
