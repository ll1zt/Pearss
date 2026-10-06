"""Fetch politeness: a blog is not hammered when a chain drains a backlog.

Issue #35. The processor sandbox spawns a fresh child per row, so a counter
inside the processor cannot pace anything — the pace has to live in the host
worker, which is what this suite drives end to end: a real migrated SQLite
queue, the real fulltext processor through its real child boundary, and a
real HTTP origin that records the arrival time of every request.

Nothing here talks to a real blog.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Long enough that a sibling fetch could reach the origin while the refusal is
# still in flight, which is exactly the window issue #57 is about. Deliberately
# explicit rather than "however long the runner happens to take".
SLOW_REFUSAL_DELAY = 2.0

# Long enough to clear MIN_ARTICLE_CHARS after extraction, so a fetch that
# happens is a fetch that succeeds (a failed fetch would muddy the counts).
ARTICLE_HTML = (
    "<html><body><article><h1>Post</h1><p>"
    + ("Full article prose for the feed reader. " * 20)
    + "</p></article></body></html>"
)


class _RecordingOrigin:
    """A real origin that timestamps every arrival.

    `delay` exists so a test can control how long the refusal takes to come
    back, which is the variable the per-host cooldown's guarantee depends on:
    a *slow* 429 is what let a sibling fetch slip through (issue #57), and a
    test that only ever answers instantly was passing on a timing coincidence
    rather than on the guarantee.
    """

    def __init__(self, *, delay: float = 0.0) -> None:
        self._arrivals: list[float] = []
        self._guard = threading.Lock()
        self.delay = delay
        # When set, every path answers 429. Lets a test state "this host is
        # refusing us" without the result depending on which of its items the
        # worker happens to fetch first (see the cooldown test).
        self.refuse_everything = False
        self.responses: dict[str, tuple[int, bytes]] = {}
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with outer._guard:
                    outer._arrivals.append(time.monotonic())
                if outer.delay:
                    # The delay is the *payload* this handler serves, not the
                    # suite waiting for something to become ready: the request
                    # under test is "a refusal that takes a while to arrive".
                    # Held as a bounded loop so the wait is interruptible and
                    # cannot silently drift longer than the configured value.
                    until = time.monotonic() + outer.delay
                    while time.monotonic() < until:
                        time.sleep(min(0.05, max(0.0, until - time.monotonic())))
                default = (
                    (429, b"slow down")
                    if outer.refuse_everything
                    else (200, ARTICLE_HTML.encode())
                )
                status, body = outer.responses.get(self.path, default)
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def arrivals(self) -> list[float]:
        with self._guard:
            return list(self._arrivals)

    def close(self) -> None:
        # shutdown() asks serve_forever() to stop; server_close() only closes the
        # listening socket. Without shutdown the loop keeps running on a closed
        # socket and spins at ~89% of a core forever -- measured, see
        # tests/integration/test_server_lifecycle.py.
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def origin() -> Iterator[_RecordingOrigin]:
    one = _RecordingOrigin()
    yield one
    one.close()


@pytest.fixture
def slow_origin() -> Iterator[_RecordingOrigin]:
    """An origin that takes a while to answer -- the shape that exposed #57."""
    one = _RecordingOrigin(delay=SLOW_REFUSAL_DELAY)
    yield one
    one.close()


def _seed_backlog(
    factory: sessionmaker[Session], origin: _RecordingOrigin, *, count: int
) -> None:
    """A source with the fulltext chain and `count` summary-only items."""
    from adapters.repository import create_source, parse_chain, timestamp
    from app.pipeline import normalise_and_store
    from core.models import ItemDraft

    chain = '[{"plugin_id":"fulltext","kind":"fulltext"}]'
    with factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://politeness.example/feed",
            canonical_url="https://politeness.example/feed",
            schedule_policy="{}",
            tenant_id="default",
            pipeline_chain=chain,
            at=timestamp(datetime.now(UTC)),
        )
        normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source.id,
                    external_id=f"post-{index}",
                    title=f"Post {index}",
                    canonical_url=origin.url(f"/post/{index}"),
                    published_at=None,
                    content="<p>Short summary.</p>",
                )
                for index in range(count)
            ],
            chain=parse_chain(chain, target_lang=None),
        )
        session.commit()


def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The documented test escape hatches for a loopback origin + child."""
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_ALLOW_PRIVATE_FETCH", "1")
    monkeypatch.setenv("PEARSS_TEST_DNS", json.dumps({"*": ["127.0.0.1"]}))
    # The child resolves the plugin from THIS checkout; the shared venv's .pth
    # points at the parent checkout, which has no fulltext processor.
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "src")])
    )


def _sweep(session: Session, factory: sessionmaker[Session]) -> dict[str, int]:
    from app.enrichment_worker import sweep_queue
    from plugins.processors.fulltext import processor as fulltext

    return sweep_queue(
        session,
        processors={"fulltext": fulltext},
        row_session_factory=factory,
    )


def _fresh_pacer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebuild the process-wide pacer so this test's env is the one it uses.

    Production reads the environment once for the process's lifetime; a test
    that changes it must say so explicitly rather than inherit a sibling's
    configuration.
    """
    from app.fetch_pacing import reset_pacer

    reset_pacer()


def test_user_backlog_is_fetched_spaced_and_capped_per_sweep(
    origin: _RecordingOrigin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a blog with a 5-post backlog and a fulltext chain,
    When the worker drains it,
    Then no sweep fetches that host more than the cap,
    And consecutive fetches to it are spaced by at least the interval.

    The complaint this pins: subscribing a blog queued 122 fetches and the
    worker fired them as fast as its slots allowed — a burst at a third
    party's server, with three of them in flight at once.
    """
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_FETCH_MIN_INTERVAL", "1")
    monkeypatch.setenv("PEARSS_FETCH_MAX_PER_HOST_PER_SWEEP", "2")
    _hermetic(monkeypatch, tmp_path)
    _fresh_pacer(monkeypatch)

    engine = sa.create_engine(migrated_database(tmp_path, "politeness.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        _seed_backlog(factory, origin, count=5)

        per_sweep: list[int] = []
        for _ in range(3):
            with factory() as session:
                report = _sweep(session, factory)
            per_sweep.append(report["succeeded"])
            assert origin.arrivals(), "each sweep must make progress"

        assert per_sweep == [2, 2, 1], (
            f"per-sweep per-host cap must hold; got {per_sweep}"
        )
        arrivals = origin.arrivals()
        assert len(arrivals) == 5, "exactly one fetch per item"
        gaps = [later - earlier for earlier, later in pairwise(arrivals)]
        # The pacer bounds *dispatch* times; the origin timestamps *arrivals*,
        # and between the two sit a child-process spawn and a file read whose
        # duration varies per row and with machine load. So the honest bounds
        # are (1) the whole span, which load can only lengthen, and (2) a
        # coarse per-pair floor. Both still separate this from the bug
        # decisively: un-paced, the same five fetches arrived 0.02s apart
        # (measured) -- an order of magnitude below either bound. A per-pair
        # bound alone was the flake: a single slow row shifted the *next*
        # arrival earlier relative to its predecessor and dipped under it
        # (issue #213).
        interval = 1.0
        span = arrivals[-1] - arrivals[0]
        assert span >= (len(arrivals) - 1) * interval * 0.9, (
            f"five fetches must span four intervals; arrivals={arrivals}"
        )
        assert all(gap >= interval * 0.5 for gap in gaps), (
            f"fetches to one host must be spaced, not fired together; gaps={gaps}"
        )

        with factory() as session:
            rows = session.execute(
                sa.text("select publish_status, count(*) from item group by 1")
            ).all()
        assert {row[0]: row[1] for row in rows} == {"ready": 5}, (
            "every item completed its chain"
        )
    finally:
        engine.dispose()


def test_user_rate_limited_host_is_left_alone(
    origin: _RecordingOrigin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a host that answers 429,
    When the worker continues,
    Then it stops fetching that host for a cooldown — a rate-limited origin
    is a request to slow down, not an invitation to try the next item on it.

    Every path refuses, on purpose. Refusing only the first item made this
    assertion depend on which row the pool ran first: with the per-sweep cap
    admitting both, a healthy row that went first *should* have been served and
    the refusal should have arrived second — two arrivals, correct behaviour,
    red test (it failed roughly one run in six under load, and passed alone).
    With every path refusing, whichever row goes first is refused and the other
    must be held back, so "exactly one" is the contract whatever the order.
    """
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_FETCH_MIN_INTERVAL", "1")
    monkeypatch.setenv("PEARSS_FETCH_MAX_PER_HOST_PER_SWEEP", "2")
    monkeypatch.setenv("PEARSS_FETCH_COOLDOWN_SECONDS", "300")
    _hermetic(monkeypatch, tmp_path)
    _fresh_pacer(monkeypatch)

    engine = sa.create_engine(migrated_database(tmp_path, "cooldown.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        origin.refuse_everything = True
        _seed_backlog(factory, origin, count=2)

        with factory() as session:
            _sweep(session, factory)
        after_first = len(origin.arrivals())
        assert after_first == 1, (
            "the host refused the first fetch it received; the sibling that was "
            f"already admitted must not be sent after that. Arrivals: {after_first}"
        )

        # The second item is still queued and the host is cooling: the next
        # sweep must not touch it.
        with factory() as session:
            _sweep(session, factory)
        assert len(origin.arrivals()) == after_first, (
            "a 429 puts the host on cooldown; no further fetches to it"
        )
    finally:
        engine.dispose()


def test_a_slow_refusal_still_puts_the_host_on_cooldown(
    slow_origin: _RecordingOrigin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 must stop the host even when the refusal is slow to arrive.

    The contract in the issue's words: a rate limit is a request to slow down,
    not an invitation to try the next item on that host. That held only when
    the origin answered instantly -- a slow refusal left a window in which the
    sibling fetch, already admitted by the per-sweep cap, dispatched before the
    cooldown was recorded.

    Measured before the fix, with only the origin's latency varying:
      delay 0s -> 1 request reaches the refusing host
      delay 3s -> 2 requests reach it

    The delay is a fixture value rather than whatever the runner does, so this
    does not depend on machine load: it is the same deterministic window every
    time, and it is *open* for 2 seconds -- far longer than any scheduling
    jitter can account for.

    **Every** path refuses, not just the first item's. Refusing one path made
    the assertion depend on which row the pool happened to run first: if the
    healthy row fetched second, two arrivals were correct behaviour and the
    test was measuring scheduling order rather than the cooldown (it passed
    alone and failed under load -- that is the signature). With every path
    refusing, whichever row goes first is refused and the other must be held
    back, so "exactly one arrival" is the contract regardless of order.
    """
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_FETCH_MIN_INTERVAL", "1")
    monkeypatch.setenv("PEARSS_FETCH_MAX_PER_HOST_PER_SWEEP", "2")
    monkeypatch.setenv("PEARSS_FETCH_COOLDOWN_SECONDS", "300")
    _hermetic(monkeypatch, tmp_path)
    _fresh_pacer(monkeypatch)

    engine = sa.create_engine(migrated_database(tmp_path, "slow-cooldown.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        slow_origin.refuse_everything = True
        _seed_backlog(factory, slow_origin, count=2)

        with factory() as session:
            _sweep(session, factory)

        arrivals = slow_origin.arrivals()
        assert len(arrivals) == 1, (
            "a host that answered 429 has asked us to slow down; a sibling "
            "fetch reaching it in the same sweep is exactly what the cooldown "
            f"is for. Arrivals: {len(arrivals)}"
        )

        # And the host stays alone on the next sweep.
        with factory() as session:
            _sweep(session, factory)
        assert len(slow_origin.arrivals()) == 1, "the cooldown must hold across sweeps"
    finally:
        engine.dispose()
