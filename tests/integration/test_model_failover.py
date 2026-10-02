"""Model health between rows: prefer the first, demote what fails, recover.

The processor runs in a child spawned per row (ADR 0047), so a counter inside
one cannot outlive it -- the same reason fetch pacing lives in the host
(`app/fetch_pacing`). What that means for the contract: the row's policy
travels *into* the child as data (`item_ctx["model_policy"]`), and the child
reports per model what happened (`model_events`), because the worker is the
only process that can remember a provider going bad.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item, ItemEnrichment
from adapters.repository import create_source, parse_chain
from app import enrichment_worker
from app.pipeline import normalise_and_store
from core.models import ItemDraft

pytestmark = pytest.mark.integration

CHAIN = '[{"plugin_id":"translate","kind":"translation","lang":"zh","required":true}]'
PRIMARY, FALLBACK = "primary", "fallback"
THREE_BLOCKS = (
    "<p>English prose one.</p><p>English prose two.</p><p>English prose three.</p>"
)


def _answer(model: str) -> tuple[int, dict[str, Any]]:
    return 200, {"choices": [{"message": {"content": f"[{model}] translated"}}]}


@contextlib.contextmanager
def _provider(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, tuple[int, dict[str, Any]]]
) -> Iterator[list[str]]:
    """A chat-completions endpoint that records the model each call names."""
    asked: list[str] = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = str(body["model"])
            asked.append(model)
            status, payload = answers.get(model, _answer(model))
            raw = json.dumps(payload).encode()
            self.send_response(status)
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
        yield asked
    finally:
        server.shutdown()
        server.server_close()


def _world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bodies: dict[str, str]
) -> sessionmaker[Session]:
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    engine = sa.create_engine(migrated_database(tmp_path, "failover.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://models.example/feed",
            canonical_url="https://models.example/feed",
            tenant_id="default",
            schedule_policy='{"type":"fixed","interval":"30m"}',
            pipeline_chain=CHAIN,
            at="2026-09-17T00:00:00.000000Z",
        )
        normalise_and_store(
            session,
            [
                ItemDraft(
                    source_id=source.id,
                    external_id=title,
                    title=title,
                    canonical_url=f"https://models.example/{title}",
                    published_at=None,
                    content=body,
                )
                for title, body in bodies.items()
            ],
            chain=parse_chain(CHAIN, target_lang=None),
        )
        session.commit()
    return factory


def _sweep(factory: sessionmaker[Session]) -> None:
    """One sweep: `concurrency=1` means one row, so rows are told apart."""
    with factory() as session:
        enrichment_worker.sweep_queue(
            session,
            processors=enrichment_worker.default_processors(),
            concurrency=1,
            max_attempts=1,
            row_session_factory=factory,
        )


def _rows(factory: sessionmaker[Session]) -> list[tuple[str, str, str | None]]:
    with factory() as session:
        return [
            (row[0], row[1], row[2])
            for row in session.execute(
                sa.select(
                    Item.title,
                    Item.publish_status,
                    ItemEnrichment.detail,
                )
                .join(ItemEnrichment, Item.id == ItemEnrichment.item_id)
                .order_by(ItemEnrichment.created_at)
            ).all()
        ]


def test_a_failed_model_is_skipped_on_the_next_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-224, AC-226: the first model is preferred; a failed one waits it out.

    Live shape of the defect (issue #227's journal): the broken model was
    asked first on every row, every sweep, forever -- the healthy fallback only
    ever answered after the broken one had been paid for again.
    """
    overloaded = {PRIMARY: (503, {"error": {"message": "overloaded"}})}
    with _provider(monkeypatch, overloaded) as asked:
        factory = _world(
            tmp_path,
            monkeypatch,
            {"one": "<p>English one.</p>", "two": "<p>English two.</p>"},
        )
        _sweep(factory)
        first_row = list(asked)
        _sweep(factory)

    assert first_row == [PRIMARY, FALLBACK], "the fallback serves the first row"
    assert asked[len(first_row) :] == [FALLBACK], "the cooling model is not asked"
    assert [title for title, status, _ in _rows(factory) if status != "ready"] == []


def test_a_model_is_retried_at_most_twice_per_row_not_per_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-225: the retry budget counts rows, not the blocks inside one.

    A document whose every block retried the broken model would pay for the
    same outage once per paragraph.
    """
    overloaded = {PRIMARY: (503, {"error": {"message": "overloaded"}})}
    with _provider(monkeypatch, overloaded) as asked:
        factory = _world(tmp_path, monkeypatch, {"doc": THREE_BLOCKS})
        _sweep(factory)

    assert asked.count(PRIMARY) == 2, asked
    assert asked.count(FALLBACK) == 3, asked
    assert [(title, status) for title, status, _ in _rows(factory)] == [
        ("doc", "ready")
    ], "the fallback still produced the whole document"


def test_a_rejected_key_is_not_retried_within_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-228: a permanent class demotes immediately instead of spending retries."""
    rejected = {PRIMARY: (401, {"error": {"message": "invalid api key"}})}
    with _provider(monkeypatch, rejected) as asked:
        factory = _world(tmp_path, monkeypatch, {"doc": THREE_BLOCKS})
        _sweep(factory)

    assert asked.count(PRIMARY) == 1, asked
    assert asked.count(FALLBACK) == 3, asked


def test_a_wholly_cooling_chain_fails_transiently_without_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-229: nothing to ask is local backpressure, not a provider verdict.

    The row must stay retryable -- `network`, never `parse` -- because the
    models are merely cooling down, and no request may go out meanwhile.
    """
    both_down = {
        PRIMARY: (503, {"error": {"message": "overloaded"}}),
        FALLBACK: (503, {"error": {"message": "overloaded"}}),
    }
    with _provider(monkeypatch, both_down) as asked:
        factory = _world(
            tmp_path,
            monkeypatch,
            {"one": "<p>English one.</p>", "two": "<p>English two.</p>"},
        )
        _sweep(factory)
        after_first = len(asked)
        assert after_first, "sanity: the first row did reach the provider"
        _sweep(factory)

    assert len(asked) == after_first, "a cooling chain must not be asked again"
    rows = _rows(factory)
    assert [(title, status) for title, status, _ in rows] == [
        ("one", "pending"),
        ("two", "pending"),
    ]
    with factory() as session:
        classes = session.scalars(
            sa.select(ItemEnrichment.error_class).order_by(ItemEnrichment.created_at)
        ).all()
    assert list(classes) == ["network", "network"], "retryable, not pinned permanent"
    assert "cooling" in (rows[1][2] or ""), rows[1][2]
