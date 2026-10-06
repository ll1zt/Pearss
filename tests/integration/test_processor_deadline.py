"""Real provider cancellation through the production processor and SQLite queue."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from adapters import filestore
from adapters.db_models import Item, ItemEnrichment
from adapters.repository import create_source, parse_chain
from app import enrichment_worker
from app.pipeline import normalise_and_store
from core.models import ItemDraft

pytestmark = pytest.mark.integration


def test_user_later_processing_progresses_after_stalled_provider_is_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a never-replying provider, when its budget expires, then its socket
    closes, failure persists, and the next item publishes without a hung sweep.

    The cooldown is switched off here on purpose: this test is about the row
    budget, and with one model a failed row legitimately parks the chain for
    the cooldown (AC-229), which would make the later row fail for a reason
    that is not this test's subject. Model health has its own tests.
    """
    from tests.migrated_schema import migrated_database

    entered = threading.Event()
    disconnected = threading.Event()
    done = threading.Event()
    connections: list[socket.socket] = []
    errors: list[Exception] = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if b"Stalled article" in body:
                connections.append(self.connection)
                entered.set()
                if self.connection.recv(1) == b"":
                    disconnected.set()
                return
            response = json.dumps(
                {"choices": [{"message": {"content": "Translated article"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv(
        "PEARSS_TRANSLATE_BASE_URL", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "fixture-not-secret")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "fixture")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "0")
    monkeypatch.setattr(enrichment_worker, "DEFAULT_CALL_TIMEOUT", 3.0)
    engine = sa.create_engine(migrated_database(tmp_path, "deadline.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    chain = (
        '[{"plugin_id":"translate","kind":"translation","lang":"zh","required":true}]'
    )
    with factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://deadline.example/feed",
            canonical_url="https://deadline.example/feed",
            tenant_id="default",
            schedule_policy='{"type":"fixed","interval":"30m"}',
            pipeline_chain=chain,
            at="2026-09-17T00:00:00.000000Z",
        )
        for name in ("Stalled article", "Later article"):
            normalise_and_store(
                session,
                [
                    ItemDraft(
                        origin_id=source.id,
                        external_id=name,
                        title=name,
                        canonical_url=f"https://deadline.example/{name.split()[0]}",
                        published_at=None,
                        content=f"<p>{name}</p>",
                    )
                ],
                chain=parse_chain(chain, target_lang=None),
            )
        session.commit()

    def run() -> None:
        try:
            for _ in range(2):
                with factory() as session:
                    enrichment_worker.sweep_queue(
                        session,
                        processors=enrichment_worker.default_processors(),
                        concurrency=1,
                        max_attempts=1,
                        row_session_factory=factory,
                    )
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5), "provider was not reached"
        assert done.wait(5), "stalled processor prevented later work and sweep shutdown"
        assert errors == []
        assert disconnected.wait(1), "timed-out processor still owns its HTTP socket"
        with factory() as session:
            rows = session.execute(
                sa.select(
                    Item.title,
                    Item.publish_status,
                    ItemEnrichment.status,
                    ItemEnrichment.error_class,
                    ItemEnrichment.payload_ref,
                )
                .join(ItemEnrichment, Item.id == ItemEnrichment.item_id)
                .order_by(Item.title)
            ).all()
            assert [tuple(row[:4]) for row in rows] == [
                ("Later article", "ready", "succeeded", None),
                ("Stalled article", "pending", "failed", "network"),
            ]
            assert (
                filestore.read_body(rows[0].payload_ref) == "<p>Translated article</p>"
            )
            assert rows[1].payload_ref is None
    finally:
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        thread.join(5)
        server.shutdown()
        server.server_close()
        server_thread.join(5)
        engine.dispose()


class SocketStall:
    """An importable processor that keeps a real connection open indefinitely."""

    @staticmethod
    def process(item_ctx: dict[str, str], *, target_lang: str, store: object) -> str:
        host, port = item_ctx["title"].split(":")
        with socket.create_connection((host, int(port))) as connection:
            connection.sendall(b"started")
            connection.recv(1)
        raise RuntimeError("provider closed")


@pytest.mark.asyncio
async def test_user_shutdown_terminates_active_processor_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given active processing, when stopped, then the socket closes and DB settles."""
    # Keep migration/app initialization in the parent: the child imports this
    # module only to resolve SocketStall, not to construct an unrelated API app.
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    engine = sa.create_engine(migrated_database(tmp_path, "shutdown.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    chain = '[{"plugin_id":"stall","kind":"summary","required":true}]'
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    with factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://shutdown.example/feed",
            canonical_url="https://shutdown.example/feed",
            tenant_id="default",
            schedule_policy='{"type":"fixed","interval":"30m"}',
            pipeline_chain=chain,
            at="2026-09-17T00:00:00.000000Z",
        )
        normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source.id,
                    external_id="shutdown",
                    title=f"127.0.0.1:{listener.getsockname()[1]}",
                    canonical_url="https://shutdown.example/article",
                    published_at=None,
                    content="<p>Stalled work</p>",
                )
            ],
            chain=parse_chain(chain, target_lang=None),
        )
        session.commit()
    worker = enrichment_worker.EnrichmentWorker(
        factory,
        processors={"stall": SocketStall},
        sweep_seconds=0.01,
    )
    try:
        await worker.start()
        connection, _ = await asyncio.to_thread(listener.accept)
        with connection:
            connection.settimeout(3)
            assert await asyncio.to_thread(connection.recv, 7) == b"started"
            async with asyncio.timeout(3):
                await worker.stop()
            assert await asyncio.to_thread(connection.recv, 1) == b""
        with factory() as session:
            assert session.scalar(sa.select(ItemEnrichment.status)) == "queued"
            assert session.scalar(sa.select(ItemEnrichment.error_class)) == "network"
            assert session.scalar(sa.select(ItemEnrichment.attempt_count)) == 1
            assert session.scalar(sa.select(ItemEnrichment.payload_ref)) is None
            assert session.scalar(sa.select(Item.publish_status)) == "pending"
    finally:
        await worker.stop()
        listener.close()
        engine.dispose()


# --------------------------------------------------------------------------
# The row's remaining budget reaches the processor (AC-221, AC-222)
#
# Before this, the processor had no idea how long it was allowed to take: one
# provider attempt was bounded at 300s regardless of the row's budget, so a
# stalled model spent the whole budget, the parent killed the child, and the
# kill discarded the per-model reasons the chain had collected.
#
# The factor itself (`min(60s, remaining/2)`) is pinned where it is
# deterministic, in the processor's own unit test. What is asserted here is the
# consequence: a stalled model cannot own the row, because an attempt that spent
# the whole budget would end in the parent's kill and the row would read
# "processor deadline expired". Measured while writing this on a box running
# another agent's builds: an httpx timeout is precise to 0.02s even under load,
# but the *child's* wall clock slips 1-2s under scheduling pressure -- so an
# attempt's duration asserted from out here would measure the scheduler, not the
# budget (issue #213). The budget below therefore leaves that jitter room.
# --------------------------------------------------------------------------


def test_user_sees_which_model_failed_when_the_budget_runs_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-221: the row's failure names the model and its reason, not the kill.

    Measured live on the deployed code: the row read
    `network / processor deadline expired`, which names neither the model that
    stalled nor what it did, and reads the same whether one model or five were
    asked.
    """
    from tests.migrated_schema import migrated_database

    reached = threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            reached.set()
            self.connection.recv(1)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv(
        "PEARSS_TRANSLATE_BASE_URL", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "fixture-not-secret")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "fixture")
    monkeypatch.setattr(enrichment_worker, "DEFAULT_CALL_TIMEOUT", 10.0)
    engine = sa.create_engine(migrated_database(tmp_path, "reason.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    chain = (
        '[{"plugin_id":"translate","kind":"translation","lang":"zh","required":true}]'
    )
    with factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://reason.example/feed",
            canonical_url="https://reason.example/feed",
            tenant_id="default",
            schedule_policy='{"type":"fixed","interval":"30m"}',
            pipeline_chain=chain,
            at="2026-09-17T00:00:00.000000Z",
        )
        normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source.id,
                    external_id="stalled",
                    title="Stalled article",
                    canonical_url="https://reason.example/stalled",
                    published_at=None,
                    content="<p>An English article that needs translating.</p>",
                )
            ],
            chain=parse_chain(chain, target_lang=None),
        )
        session.commit()
    try:
        with factory() as session:
            enrichment_worker.sweep_queue(
                session,
                processors=enrichment_worker.default_processors(),
                concurrency=1,
                max_attempts=1,
                row_session_factory=factory,
            )
        assert reached.is_set(), "the provider was not reached"
        with factory() as session:
            status, error_class, detail, payload_ref, publish_status = session.execute(
                sa.select(
                    ItemEnrichment.status,
                    ItemEnrichment.error_class,
                    ItemEnrichment.detail,
                    ItemEnrichment.payload_ref,
                    Item.publish_status,
                ).join(Item, Item.id == ItemEnrichment.item_id)
            ).one()
        assert (status, error_class) == ("failed", "network")
        assert payload_ref is None, "a failed row stores no artifact"
        assert publish_status == "pending", "the gate stays shut"
        assert "fixture:" in detail, detail
        assert "deadline expired" not in detail, detail
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(5)
        engine.dispose()


def test_a_killed_row_keeps_what_the_chain_had_learned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-232: a killed row says where it died and what the models had said.

    Before this, a killed row read exactly `processor deadline expired`: the
    child's stderr -- where the block counter and the fallback line both live --
    went to /dev/null, and the kill discarded the aggregate the chain had built.

    The fallback model drips bytes instead of answering, so no read timeout ever
    fires and the child really does run to the deadline: the kill is the path
    under test rather than a timing accident.
    """
    from tests.migrated_schema import migrated_database

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body["model"] != "primary":
                # One byte at a time: a read timeout measures the gap *between*
                # bytes, so this neither trips it nor ever completes.
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                for _ in range(1000):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
                return
            raw = json.dumps({"error": {"message": "overloaded"}}).encode()
            self.send_response(503)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv(
        "PEARSS_TRANSLATE_BASE_URL", f"http://127.0.0.1:{server.server_port}"
    )
    monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "fixture-not-secret")
    monkeypatch.setenv("PEARSS_TRANSLATE_MODELS", "primary,fallback")
    monkeypatch.delenv("PEARSS_TRANSLATE_MODEL", raising=False)
    monkeypatch.setattr(enrichment_worker, "DEFAULT_CALL_TIMEOUT", 4.0)
    engine = sa.create_engine(migrated_database(tmp_path, "killed.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    chain = (
        '[{"plugin_id":"translate","kind":"translation","lang":"zh","required":true}]'
    )
    with factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://killed.example/feed",
            canonical_url="https://killed.example/feed",
            tenant_id="default",
            schedule_policy='{"type":"fixed","interval":"30m"}',
            pipeline_chain=chain,
            at="2026-09-17T00:00:00.000000Z",
        )
        normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source.id,
                    external_id="killed",
                    title="Killed article",
                    canonical_url="https://killed.example/article",
                    published_at=None,
                    content=(
                        "<p>English prose one.</p>"
                        "<p>English prose two.</p>"
                        "<p>English prose three.</p>"
                    ),
                )
            ],
            chain=parse_chain(chain, target_lang=None),
        )
        session.commit()
    try:
        with factory() as session:
            enrichment_worker.sweep_queue(
                session,
                processors=enrichment_worker.default_processors(),
                concurrency=1,
                max_attempts=1,
                row_session_factory=factory,
            )
        with factory() as session:
            detail = session.scalar(sa.select(ItemEnrichment.detail))
        assert detail is not None
        assert "deadline expired" in detail, detail
        assert "chunk " in detail, detail
        assert "primary" in detail and "falling back" in detail, detail
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(5)
        engine.dispose()
