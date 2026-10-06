"""Alerting end to end: the system says something when a person is needed.

Issue #4 was a 15-hour silent outage -- 56 of 56 sources failing while
`recent_run_success_rate` sat at 0.0 on an endpoint nobody was polling. The
scenario register is `tests/unit/test_alerting.py` (AC-29..AC-35); this file
drives the same promises through the real path: the real `system_snapshot`
query, the real sender, and a real HTTP endpoint on the loopback standing in for
the deployment's notify gateway.

Why a local endpoint rather than a mocked `httpx.post`: the thing that broke was
"nobody was told", and the evidence that someone *is* told is a request arriving
somewhere. A mock would assert that we called a function; a socket proves the
body that would have been sent.

Nothing here talks to the real notify gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Source as _SourceRow
from app.main import create_engine_for
from core.enums import ErrorClass
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration


class _AlertSink:
    """A real HTTP endpoint that records every alert it is sent."""

    def __init__(self) -> None:
        self._bodies: list[dict[str, Any]] = []
        self._guard = threading.Lock()
        self.status = 202
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {"_unparsed": raw.decode("utf-8", "replace")}
                with outer._guard:
                    outer._bodies.append(payload)
                self.send_response(outer.status)
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/notify"

    def bodies(self) -> list[dict[str, Any]]:
        with self._guard:
            return list(self._bodies)

    def close(self) -> None:
        # shutdown() stops serve_forever(); server_close() alone leaves the loop
        # spinning on a closed socket (measured at ~89% of a core per leak --
        # tests/integration/test_server_lifecycle.py pins this).
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def sink() -> Iterator[_AlertSink]:
    one = _AlertSink()
    yield one
    one.close()


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine_for(migrated_database(tmp_path, "alerting.db"))
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _finish_runs(
    factory: sessionmaker[Session], *, succeeded: int, failed: int
) -> None:
    """Record finished runs so the snapshot has a rate to read.

    Seeded through the repository rather than by raw INSERT, as the testing
    guide requires: a hand-written row breaks the moment the schema gains a
    column and does not exercise the real write path. Timestamps are the
    repository's own fixed-width strings, because every `at` parameter on this
    API is a `str` -- passing a `datetime` here made SQLite fall back to its
    deprecated adapter and the resulting DeprecationWarning failed the test.

    The source row exists first: a run hangs off a job, and a job off a source.
    """
    from adapters.repository import (
        claim_job_run,
        create_job_run,
        create_source,
        ensure_collect_job,
        finish_job_run,
    )
    from adapters.timestamps import fixed_width_utc
    from core.job_state import JobRunEvent

    moment = datetime.now(UTC)
    with factory() as session:
        if session.get(_SourceRow, "src-alert") is None:
            create_source(
                session,
                source_type="rss",
                url="https://alert.example/feed",
                canonical_url="https://alert.example/feed",
                schedule_policy='{"type": "fixed", "interval": "1h"}',
                tenant_id="default",
                at=fixed_width_utc(moment),
            )
            session.commit()
        job_id = ensure_collect_job(
            session,
            source_id="src-alert",
            tenant_id="default",
            at=fixed_width_utc(moment),
        )
        session.commit()
        planned = [True] * succeeded + [False] * failed
        # Ids must be unique across calls: the recovery scenario seeds a second
        # batch on the same database to flip the rate, and a fixed `run-N`
        # collides with the first batch's primary keys.
        batch = f"{int(moment.timestamp() * 1_000_000)}"
        for index, ok in enumerate(planned):
            # Oldest first, so `finished_at.desc()` puts the newest state first
            # and the snapshot's last-50 window is the tail of this sequence.
            started = moment - timedelta(seconds=max(0, len(planned) - index))
            run_id = f"run-{batch}-{index}"
            at_open = fixed_width_utc(started)
            create_job_run(
                session,
                run_id=run_id,
                job_id=job_id,
                correlation_id=f"corr-{index}",
                at=at_open,
            )
            session.commit()
            assert claim_job_run(session, run_id=run_id, at=at_open) is True
            finish_job_run(
                session,
                run_id=run_id,
                event=JobRunEvent.SUCCEED if ok else JobRunEvent.FAIL,
                correlation_id=f"corr-{index}",
                error_class=None if ok else ErrorClass.NETWORK,
                at=fixed_width_utc(started + timedelta(seconds=1)),
            )
            session.commit()


def _scheduler(factory: sessionmaker[Session]) -> Any:
    """A Scheduler whose health check is the unit under test."""
    from app.scheduler import Scheduler

    return Scheduler(factory, client=None)  # type: ignore[arg-type]


def _alerts(factory: sessionmaker[Session], *, tick: int) -> int:
    """Run the health check `tick` times on one instance; return alerts sent."""
    scheduler = _scheduler(factory)
    return sum(1 for _ in range(tick) if scheduler._alert_if_needed())


# --------------------------------------------------------------------------
# AC-29: collapse alerts, and only once
# --------------------------------------------------------------------------


def test_a_collapsed_success_rate_sends_exactly_one_alert(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-29: the incident that went unnoticed for 15 hours now speaks up.

    And speaks up **once**: the tick loop runs every 60s, so a level-triggered
    alert would send 900 notifications across that outage and get the channel
    muted -- the original silence, by another route.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    _finish_runs(session_factory, succeeded=0, failed=40)

    sent = _alerts(session_factory, tick=5)

    assert sent == 1, f"expected one alert on the edge, got {sent}"
    assert len(sink.bodies()) == 1


def test_the_alert_names_the_direction(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-33: the body carries what makes the cause obvious at a glance.

    In the motivating incident every failure was `network` and they landed in
    the same minute, which points straight at a shared dependency. "Something is
    wrong" would have sent the operator through 56 sources instead.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    _finish_runs(session_factory, succeeded=1, failed=39)

    _alerts(session_factory, tick=1)

    (body,) = sink.bodies()
    assert str(body.get("source", "")).startswith("pearss@")
    text = json.dumps(body).lower()
    assert "network" in text, body
    assert "success rate" in text, body


def test_a_healthy_system_sends_nothing(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction, so an "always alert" regression cannot pass."""
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    _finish_runs(session_factory, succeeded=40, failed=0)

    sent = _alerts(session_factory, tick=5)

    assert sent == 0
    assert sink.bodies() == []


# --------------------------------------------------------------------------
# AC-30: recovery
# --------------------------------------------------------------------------


def test_recovery_sends_one_notification(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-30: the operator learns the outage is over without watching for it.

    Recovery is the one message that must not wait: a state that flipped back is
    news, and an interval meant to suppress *repeats* has nothing to suppress
    here.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    _finish_runs(session_factory, succeeded=0, failed=40)

    # One instance throughout, because edge detection is per-process state.
    scheduler = _scheduler(session_factory)
    assert scheduler._alert_if_needed() is True

    _finish_runs(session_factory, succeeded=40, failed=0)
    assert scheduler._alert_if_needed() is True, (
        "recovery must send immediately, not after the interval"
    )

    bodies = sink.bodies()
    assert len(bodies) == 2, bodies
    assert "recovered" in json.dumps(bodies[-1]).lower()


def test_recovery_is_not_delayed_by_the_repeat_interval(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-30 (the boundary): the interval suppresses repeats, not recovery.

    A long interval is the realistic setting for an outage notification, so if
    recovery honoured it the operator would learn that collection came back up
    only after the next hour elapsed.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    monkeypatch.setenv("PEARSS_ALERT_MIN_INTERVAL_SECONDS", "99999")
    _finish_runs(session_factory, succeeded=0, failed=40)

    scheduler = _scheduler(session_factory)
    assert scheduler._alert_if_needed() is True

    _finish_runs(session_factory, succeeded=40, failed=0)
    assert scheduler._alert_if_needed() is True, (
        "recovery must not be held back by the repeat interval"
    )


# --------------------------------------------------------------------------
# AC-32: no bombardment
# --------------------------------------------------------------------------


def test_a_persistent_outage_does_not_repeat_within_the_interval(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-32: an hour-long outage is one message, not sixty.

    Every repeat is a notification someone has to dismiss, and dismissing is the
    first step towards ignoring.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    monkeypatch.setenv("PEARSS_ALERT_MIN_INTERVAL_SECONDS", "3600")
    _finish_runs(session_factory, succeeded=0, failed=40)

    sent = _alerts(session_factory, tick=10)

    assert sent == 1, f"the interval must suppress repeats, got {sent}"


# --------------------------------------------------------------------------
# AC-34: alerting never breaks collection
# --------------------------------------------------------------------------


def test_an_unreachable_gateway_does_not_break_the_tick(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-34: a dead alert endpoint is not a collection outage.

    Pointed at a port nothing listens on, which is the realistic failure: the
    gateway host is down, or the URL is wrong.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", "http://127.0.0.1:9/notify")
    monkeypatch.setenv("PEARSS_ALERT_TIMEOUT_SECONDS", "1")
    _finish_runs(session_factory, succeeded=0, failed=40)

    # Must not raise: the caller is a scheduler tick.
    assert _alerts(session_factory, tick=1) == 0


def test_an_unconfigured_gateway_is_not_an_error(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No endpoint configured means alerts are off, which is a valid choice.

    It must not raise, and must not be mistaken for a delivery: a deployment
    that never configured notifications has not failed at anything.
    """
    monkeypatch.delenv("PEARSS_ALERT_URL", raising=False)
    monkeypatch.delenv("NOTIFY_URL", raising=False)
    _finish_runs(session_factory, succeeded=0, failed=40)

    assert _alerts(session_factory, tick=1) == 0


def test_a_rejected_alert_is_reported_as_not_sent(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx from the gateway must not read as a delivered alert.

    The gateway answers non-2xx for a body too long, an unknown chat, or a full
    queue. Treating that as success would leave the operator believing they had
    been told.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    sink.status = 400
    _finish_runs(session_factory, succeeded=0, failed=40)

    assert _alerts(session_factory, tick=1) == 0
    assert len(sink.bodies()) == 1, "it was attempted, and refused"


def test_an_oversized_body_is_truncated_to_the_gateway_limit(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway caps a body at 4093 characters and answers 400 past it.

    Our bodies are a few lines, so this guards a future one growing silently
    past the limit and being refused in the one place we cannot see.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)

    assert _send("x" * 5000) is True

    (body,) = sink.bodies()
    assert len(body["text"]) == 4093


def _send(text: str) -> bool:
    from adapters.alerting import send_alert

    return send_alert(text, title="t", source="s")


def test_the_alert_does_not_wait_on_a_silent_gateway(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout bounds our wait, so a wedged gateway cannot stall a tick.

    A listening socket that never answers is the shape a hung gateway takes, and
    the bound is what keeps an alerting problem from becoming a scheduler one.
    """
    import socket as _socket

    server = _socket.socket()
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    monkeypatch.setenv("PEARSS_ALERT_URL", f"http://127.0.0.1:{port}/notify")
    monkeypatch.setenv("PEARSS_ALERT_TIMEOUT_SECONDS", "1")
    _finish_runs(session_factory, succeeded=0, failed=40)

    started = time.monotonic()
    try:
        sent = _alerts(session_factory, tick=1)
    finally:
        # `accept()` is never called, so the queued connection goes when this
        # socket does; closing it here keeps the warning stream clean, since an
        # unclosed socket is a failure under this suite's warning policy.
        server.close()
    elapsed = time.monotonic() - started

    assert sent == 0, "nothing was delivered"
    assert elapsed < 20, f"the tick waited {elapsed:.1f}s on a silent gateway"


# --------------------------------------------------------------------------
# AC-35: the journal can tell "failing" from "fine"
# --------------------------------------------------------------------------


def test_a_backing_off_source_is_visible_in_the_journal(
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-35: the second half of the issue -- a failing source must be legible.

    A source in backoff used to be indistinguishable from a healthy one in the
    journal, because the due-check's decision was discarded on the "not due"
    path. With one source failing for days, `journalctl` looked exactly like a
    quiet, working system (issue #4, mode 2).
    """
    from adapters.repository import create_source, timestamp
    from app.scheduler import run_tick

    monkeypatch.setenv("PEARSS_ALERT_URL", "")
    with session_factory() as session:
        create_source(
            session,
            source_type="rss",
            url="https://backoff.example/feed",
            canonical_url="https://backoff.example/feed",
            schedule_policy='{"type": "fixed", "interval": "1h"}',
            tenant_id="default",
            at=timestamp(datetime.now(UTC)),
        )
        session.commit()
    _finish_runs(session_factory, succeeded=0, failed=3)

    with caplog.at_level("INFO", logger="app.scheduler"):
        run_tick(session_factory, client=None)  # type: ignore[arg-type]

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "source" in logged, f"no per-source line at all:\n{logged}"
    if "failing" in logged:
        assert "consecutive failure" in logged, logged


# --------------------------------------------------------------------------
# The async wrapper: the loop's own failure handling
#
# `_check_health_and_alert` is what the scheduler's tick loop actually calls, and
# its two branches (a raised failure, and a successful send being announced) are
# the difference between "an alerting problem stays an alerting problem" and
# "the tick loop dies". Neither is reachable through `_alert_if_needed` alone.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_wrapper_swallows_a_failing_health_check(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-34: a broken health check must not take the tick loop down.

    The snapshot query can fail for its own reasons (a locked database, a schema
    mismatch after a bad migration). The tick loop calls this after every
    collection pass, so an escaping exception here would stop collection
    entirely -- the outage being alerted about, caused by the alerting.
    """
    scheduler = _scheduler(session_factory)

    def explode() -> bool:
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(scheduler, "_alert_if_needed", explode, raising=False)

    with caplog.at_level("WARNING", logger="app.scheduler"):
        # Must not raise.
        await scheduler._check_health_and_alert()

    assert any("health check failed" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


@pytest.mark.asyncio
async def test_the_wrapper_announces_a_sent_alert(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delivered alert is logged, because the alternative is guessing.

    The operator reading the journal cannot see the notification that arrived on
    their phone, so the journal has to say one was sent -- otherwise "did it
    fire?" is unanswerable from the host.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    _finish_runs(session_factory, succeeded=0, failed=40)
    scheduler = _scheduler(session_factory)

    with caplog.at_level("INFO", logger="app.scheduler"):
        await scheduler._check_health_and_alert()

    assert len(sink.bodies()) == 1
    assert any("health alert sent" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_a_supplied_client_is_used_for_the_post(
    sink: _AlertSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sender accepts a client so a caller can reuse one.

    Driven directly because going through the scheduler constructs its own
    client; this is the injection point a future caller (or a test) would use to
    share a connection pool.
    """
    import httpx

    from adapters.alerting import send_alert

    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    with httpx.Client() as client:
        assert send_alert("body", title="t", source="s", client=client) is True

    (payload,) = sink.bodies()
    assert payload["text"] == "body"


def _source_with_chain(
    session: Session,
    *,
    source_id: str,
    statuses: list[str],
    content: str = "<p>body</p>",
) -> str:
    """A source with one item and one chain row per status given.

    Seeded through the repository, then the rows are moved to their statuses
    directly: the worker owns those transitions, and this test is about what the
    inspection query makes of the result rather than about how they got there.
    """
    from adapters.repository import create_source, enqueue_chain, timestamp
    from core.enrichment import EnrichmentKind, EnrichmentSpec

    source = create_source(
        session,
        source_type="rss",
        url=f"https://{source_id}.example/feed",
        canonical_url=f"https://{source_id}.example/feed",
        schedule_policy="{}",
        tenant_id="default",
        at=timestamp(datetime.now(UTC)),
    )
    item = _store_one_item(session, source.id, content=content)
    enqueue_chain(
        session,
        item_id=item,
        content_hash="hash",
        chain=tuple(
            EnrichmentSpec(plugin_id=f"p{i}", kind=EnrichmentKind.SUMMARY)
            for i in range(len(statuses))
        ),
        tenant_id="default",
        now=None,
    )
    session.flush()
    rows = (
        session.execute(
            sa.text("select id from item_enrichment where item_id = :item order by id"),
            {"item": item},
        )
        .scalars()
        .all()
    )
    assert len(rows) == len(statuses), (rows, statuses)
    for row_id, status in zip(rows, statuses, strict=True):
        session.execute(
            sa.text("update item_enrichment set status = :s where id = :i"),
            {"s": status, "i": row_id},
        )
    return source.id


def _store_one_item(
    session: Session, source_id: str, *, content: str = "<p>body</p>"
) -> str:
    """One item through the production write path."""
    from app.pipeline import normalise_and_store
    from core.models import ItemDraft

    ingested = normalise_and_store(
        session,
        [
            ItemDraft(
                origin_id=source_id,
                external_id="one",
                title="An item",
                canonical_url="https://x.example/one",
                published_at=datetime.now(UTC),
                content=content,
            )
        ],
        tenant_id="default",
    )
    return ingested[0].result.item.id


@pytest.mark.integration
def test_a_source_whose_chain_all_failed_is_reported(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-150: the stuck-chain condition, and its attribution, from the rows.

    Three boundaries, because each is a way to make the channel noise: two rows
    is below ADR 0034's sample floor, one queued row means the chain can still
    finish, and a succeeded row is not pending at all. The attribution (title,
    class, count, excerpt) rides along because issue #152 made the alert name
    the source and the reason rather than only its id.
    """
    from adapters.repository.inspection import exhausted_enrichment_sources

    with session_factory() as session:
        stuck = _source_with_chain(session, source_id="stuck", statuses=["failed"] * 3)
        too_few = _source_with_chain(session, source_id="few", statuses=["failed"] * 2)
        still_working = _source_with_chain(
            session, source_id="working", statuses=["failed", "failed", "queued"]
        )
        done = _source_with_chain(
            session, source_id="done", statuses=["failed", "failed", "succeeded"]
        )
        session.execute(
            sa.text("update source set title = :t where id = :i"),
            {"t": "X Following", "i": stuck},
        )
        session.execute(
            sa.text(
                "update item_enrichment set error_class = :c, detail = :d "
                "where item_id in (select id from item where source_id = :s)"
            ),
            {
                "c": "parse",
                "d": "provider rejected the request: HTTP 400 (region)",
                "s": stuck,
            },
        )
        session.commit()

        reported = exhausted_enrichment_sources(session, tenant_id="default")

    assert len(reported) == 1, (
        f"reported {[source.origin_id for source in reported]}; expected only "
        f"the source whose every pending row failed ({stuck}). Below the sample "
        f"floor: {too_few}. Still able to finish: {still_working}. Nothing "
        f"pending: {done}."
    )
    (source,) = reported
    assert source.origin_id == stuck
    assert source.title == "X Following"
    assert source.failed_count == 3
    assert source.error_class == "parse"
    assert source.detail_excerpt == "provider rejected the request: HTTP 400 (region)"


@pytest.mark.integration
def test_the_representative_failure_is_the_newest(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-150: the class and message are the *newest* failure.

    "What is it failing on now" is the actionable reading; an older class would
    send the operator at a failure the source has already moved past.
    """
    from adapters.repository.inspection import exhausted_enrichment_sources

    with session_factory() as session:
        stuck = _source_with_chain(session, source_id="stuck", statuses=["failed"] * 3)
        session.execute(
            sa.text("update source set title = 'T' where id = :i"), {"i": stuck}
        )
        row_ids = (
            session.execute(
                sa.text(
                    "select id from item_enrichment where item_id in "
                    "(select id from item where source_id = :s) order by id"
                ),
                {"s": stuck},
            )
            .scalars()
            .all()
        )
        for row_id, (cls, detail, at) in zip(
            row_ids,
            [
                ("network", "oldest", "2026-01-01T00:00:00.000000Z"),
                ("parse", "middle", "2026-01-02T00:00:00.000000Z"),
                ("auth", "newest", "2026-01-03T00:00:00.000000Z"),
            ],
            strict=True,
        ):
            session.execute(
                sa.text(
                    "update item_enrichment set error_class = :c, detail = :d, "
                    "updated_at = :u where id = :i"
                ),
                {"c": cls, "d": detail, "u": at, "i": row_id},
            )
        session.commit()

        (source,) = exhausted_enrichment_sources(session, tenant_id="default")

    assert source.error_class == "auth", source
    assert source.detail_excerpt == "newest", source
    assert source.failed_count == 3


@pytest.mark.integration
def test_the_alert_attributes_a_stuck_chain(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-151: the notification names the source, the class and the reason.

    The live complaint behind issue #152: the alert carried only a source id,
    and every follow-up question (which source? what error?) was a file read
    away from a message that already had the answer.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    with session_factory() as session:
        stuck = _source_with_chain(session, source_id="stuck", statuses=["failed"] * 3)
        session.execute(
            sa.text("update source set title = 'X Following' where id = :i"),
            {"i": stuck},
        )
        session.execute(
            sa.text(
                "update item_enrichment set error_class = 'parse', detail = :d "
                "where item_id in (select id from item where source_id = :s)"
            ),
            {
                "d": (
                    "provider rejected the request: HTTP 400 "
                    "(User location is not supported for the API use.)"
                ),
                "s": stuck,
            },
        )
        session.commit()
    # A healthy rate: only the stuck-chain condition can degrade the verdict.
    _finish_runs(session_factory, succeeded=40, failed=0)

    assert _alerts(session_factory, tick=1) == 1
    (body,) = sink.bodies()
    text = body["text"]
    assert "X Following" in text
    assert stuck[:8] in text
    assert "parse" in text
    assert "User location is not supported" in text
    assert len(text) <= 4093


@pytest.mark.integration
def test_three_stuck_sources_are_each_named(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-156: one line per stuck source, and the body stays within the limit."""
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    with session_factory() as session:
        for index in range(3):
            stuck = _source_with_chain(
                session, source_id=f"stuck{index}", statuses=["failed"] * 3
            )
            session.execute(
                sa.text("update source set title = :t where id = :i"),
                {"t": f"Feed {index}", "i": stuck},
            )
            session.execute(
                sa.text(
                    "update item_enrichment set error_class = 'parse', "
                    "detail = 'provider said no' where item_id in "
                    "(select id from item where source_id = :s)"
                ),
                {"s": stuck},
            )
        session.commit()
    _finish_runs(session_factory, succeeded=40, failed=0)

    assert _alerts(session_factory, tick=1) == 1
    (body,) = sink.bodies()
    for index in range(3):
        assert f"Feed {index}" in body["text"], body["text"]
    assert len(body["text"]) <= 4093


@pytest.mark.integration
def test_the_alert_never_carries_item_content(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-155: the alert carries the provider's message, never the item body.

    ADR 0042 draws the line: attribution fields (source id/title, class, count,
    provider error text) may leave the host; item content may not. The provider
    message is the reason a person needs; the body is the thing they are not
    supposed to be reading in a notification.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    with session_factory() as session:
        stuck = _source_with_chain(
            session,
            source_id="stuck",
            statuses=["failed"] * 3,
            content="<p>ITEM-SECRET-MARKER</p>",
        )
        session.execute(
            sa.text("update source set title = 'T' where id = :i"), {"i": stuck}
        )
        session.execute(
            sa.text(
                "update item_enrichment set error_class = 'parse', "
                "detail = 'provider said no' where item_id in "
                "(select id from item where source_id = :s)"
            ),
            {"s": stuck},
        )
        session.commit()
    _finish_runs(session_factory, succeeded=40, failed=0)

    assert _alerts(session_factory, tick=1) == 1
    (body,) = sink.bodies()
    assert "provider said no" in body["text"]
    assert "ITEM-SECRET-MARKER" not in body["text"], body["text"]


# --------------------------------------------------------------------------
# Issue #61 B: a loop that stops, reported by the loop that is still running
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_worker_reports_a_scheduler_that_stopped(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direction that matters most, because this is the one that goes silent.

    A dead scheduler stops collection entirely -- the shape of issue #4 -- and it
    cannot report itself: the code that would notice is the code that stopped. So
    the enrichment worker, which is still ticking, has to be the one that says
    so. One alert on the edge, and a recovery when the loop comes back, both
    naming the loop rather than the watcher.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    # Long, not zero: the interval is what suppresses a repeat of a *continuing*
    # condition, so zeroing it here would assert away the thing being asserted.
    # An edge and a recovery both ignore the interval, which is the other half.
    monkeypatch.setenv("PEARSS_ALERT_MIN_INTERVAL_SECONDS", "3600")

    from app.enrichment_worker import EnrichmentWorker
    from app.scheduler import Scheduler

    scheduler = Scheduler(session_factory, client=None, tick_seconds=3600)  # type: ignore[arg-type]
    worker = EnrichmentWorker(session_factory, processors={}, watch=scheduler.liveness)

    async def scenario() -> list[bool]:
        await scheduler.start()
        assert scheduler.liveness()[0] is True
        quiet = worker.check_loops()

        # Kill it the way an escaped error would: the task ends, the object
        # stays -- exactly the state the health endpoint reads as not alive.
        assert scheduler._task is not None
        scheduler._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler._task

        stopped = worker.check_loops()
        repeated = worker.check_loops()

        await scheduler.start()
        recovered = worker.check_loops()
        return [quiet, stopped, repeated, recovered]

    quiet, stopped, repeated, recovered = asyncio.run(scenario())
    assert quiet is False, "a live scheduler is not news"
    assert stopped is True, "the worker must not let a dead scheduler go unsaid"
    assert repeated is False, "one edge, not one per sweep"
    assert recovered is True, "recovery is reported immediately"

    stopped_body, recovered_body = sink.bodies()
    assert stopped_body["title"] == "Pearss loop stopped"
    assert stopped_body["text"] == (
        "background loop(s) not running: scheduler\n"
        "action: check the process logs; the loop restarts with the service"
    ), stopped_body
    assert recovered_body["title"] == "Pearss loop recovered"
    assert recovered_body["text"] == (
        "background processing recovered: it is running again"
    ), recovered_body


@pytest.mark.integration
def test_the_scheduler_reports_a_worker_that_stopped(
    session_factory: sessionmaker[Session],
    sink: _AlertSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-113 (the other direction): the two loops watch *each other*.

    The worker->scheduler direction is pinned above. This is the reverse, wired
    in `main.py` as `scheduler.watch(worker.liveness)`: a dead enrichment worker
    must be named, once, and its recovery reported. Without this half, "the
    loops watch each other" rests on one tested direction and one that only its
    wiring claims -- which is exactly the shape #61's audit found (#125).
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)
    monkeypatch.setenv("PEARSS_ALERT_MIN_INTERVAL_SECONDS", "3600")
    # A healthy rate, so only the loop watch has anything to say.
    _finish_runs(session_factory, succeeded=40, failed=0)

    from app.enrichment_worker import EnrichmentWorker
    from app.scheduler import Scheduler

    scheduler = Scheduler(session_factory, client=None, tick_seconds=3600)  # type: ignore[arg-type]
    worker = EnrichmentWorker(session_factory, processors={}, watch=lambda: (True, ""))
    scheduler.watch(worker.liveness)

    async def scenario() -> None:
        await worker.start()
        scheduler._alert_if_needed()  # the worker is alive: no loop alert
        assert worker._task is not None
        worker._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker._task
        scheduler._alert_if_needed()  # the worker stopped: one alert
        scheduler._alert_if_needed()  # a repeat within the interval: suppressed

    asyncio.run(scenario())

    (body,) = sink.bodies()
    assert body["title"] == "Pearss loop stopped"
    assert body["text"] == (
        "background loop(s) not running: enrichment\n"
        "action: check the process logs; the loop restarts with the service"
    ), body


@pytest.mark.integration
def test_a_watch_that_was_never_handed_a_loop_reports_nothing(
    sink: _AlertSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop with nothing to watch must not name one.

    Reporting a guessed name would be worse than silence: it would send an
    operator to look at a component nobody checked.
    """
    monkeypatch.setenv("PEARSS_ALERT_URL", sink.url)

    from app.liveness_watch import LoopWatch

    assert LoopWatch(name="scheduler", watch=lambda: (True, "")).check() is False
    assert sink.bodies() == []
