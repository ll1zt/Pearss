"""Integration tests for authenticated paging: state, and failing closed.

The HTTP layer is replaced with a MockTransport serving recorded-shaped
payloads, so these exercise the real repository writes and the real walk
without a network or a credential.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from adapters.db_models import Source
from adapters.repository import paging_state, save_paging_state
from app import collector_registry
from app.main import create_engine_for
from app.pipeline import CollectionResult, collect_paged
from core.enums import ErrorClass

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ID = "src-x"
CANONICAL = "https://x.com/home"


def timeline(ids: list[str], next_cursor: str | None) -> bytes:
    """A minimal HomeLatestTimeline-shaped body."""
    entries = []
    for tweet_id in ids:
        entries.append(
            {
                "entryId": f"tweet-{tweet_id}",
                "sortIndex": tweet_id,
                "content": {
                    "itemContent": {
                        "tweet_results": {
                            "result": {
                                "__typename": "Tweet",
                                "rest_id": tweet_id,
                                "core": {
                                    "user_results": {
                                        "result": {
                                            "core": {
                                                "name": "Tester",
                                                "screen_name": "tester",
                                            }
                                        }
                                    }
                                },
                                "legacy": {
                                    "full_text": f"body of {tweet_id}",
                                    "created_at": "Sun Aug 09 16:00:00 +0000 2026",
                                    "entities": {"urls": [], "media": []},
                                },
                            }
                        }
                    }
                },
            }
        )
    if next_cursor:
        entries.append(
            {
                "entryId": f"cursor-bottom-{next_cursor}",
                "content": {"value": next_cursor},
            }
        )
    return json.dumps(
        {
            "data": {
                "home": {
                    "home_timeline_urt": {
                        "instructions": [
                            {"type": "TimelineAddEntries", "entries": entries}
                        ]
                    }
                }
            }
        }
    ).encode()


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "paging.db")))
    engine = create_engine_for(url)
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    with Session(engine) as made:
        made.add(
            Source(
                id=SOURCE_ID,
                type="web",
                url=CANONICAL,
                canonical_url=CANONICAL,
                schedule_policy='{"type":"fixed","interval":"60m"}',
                status="active",
                trust_level="standard",
                tenant_id="default",
                created_at="2026-01-01T00:00:00.000000Z",
                updated_at="2026-01-01T00:00:00.000000Z",
            )
        )
        made.commit()
        yield made
    engine.dispose()


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))


def source_row(session: Session) -> Source:
    found = session.get(Source, SOURCE_ID)
    assert found is not None
    return found


# --------------------------------------------------------------------------
# Failing closed on credentials
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_first_walk_spends_first_pages_and_records_the_floor(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interval model's entry point: a fresh source walks exactly
    `PEARSS_X_FIRST_PAGES` pages and its deepest cursor becomes the floor.

    Without this cap the first walk ran to the host's page budget and dragged
    in years of history -- 1,557 tweets spanning 2023-2026 on a source whose
    operator asked for a small sample.
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")
    monkeypatch.setenv(x_client.FIRST_PAGES_ENV, "3")

    requested: list[str] = []
    seen = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body["variables"].get("cursor")
        requested.append(cursor or "NEWEST")
        # Every page is entirely new (frontier is empty), and each carries a
        # *distinct* deeper cursor: an origin answering the same cursor it was
        # given is treated as the bottom, which is a different case.
        seen["n"] += 1
        return httpx.Response(200, content=timeline(["9000"], f"PAGE-{seen['n'] + 1}"))

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(route))
    result = collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    session.commit()

    # A fresh source has no frontier to stop at, so the walk runs to its
    # three-page budget and no further.
    assert requested == ["NEWEST", "PAGE-2", "PAGE-3"], requested
    # Three pages fetched; dedupe collapses the repeated tweet.
    assert result.drafts == 3
    assert result.inserted == 1
    # The walk ran out of budget without crossing a frontier there was none of,
    # so its bottom is the interval's floor.
    assert paging_state(session, source_id=SOURCE_ID).floor_cursor == "PAGE-4"
    # The deepest cursor the walk reached is the interval's floor... but note
    # the walk did not *end on* a floor (there is none yet) -- it ended on
    # budget. The floor is only ever set by a walk that reached one, so here
    # it stays unset; the interval's lower end is simply "whatever page 3
    # reached", and nothing below it will ever be requested because the
    # frontier now covers everything the first walk kept.
    state = paging_state(session, source_id=SOURCE_ID)
    assert state.seen_frontier == 9000


@pytest.mark.integration
def test_missing_credentials_fail_as_auth_without_requesting(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0012: no credential means `auth`, not an anonymous attempt.

    Falling back to an unauthenticated request would produce a 403 that reads
    as X refusing, sending the operator to the wrong remedy.
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.delenv(x_client.AUTH_TOKEN_ENV, raising=False)
    monkeypatch.delenv(x_client.CT0_ENV, raising=False)

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unauthenticated request must not be attempted")

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(never))
    result = collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    assert result.succeeded is False
    assert result.error_class == ErrorClass.AUTH.value
    assert "credentials" in (result.detail or "").lower()


@pytest.mark.integration
def test_a_rejected_request_keeps_its_classification(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport(
        "twitter", httpx.MockTransport(lambda request: httpx.Response(429))
    )
    result = collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    assert result.succeeded is False
    assert result.error_class == ErrorClass.BLOCKED.value


# --------------------------------------------------------------------------
# Cursor state
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_successful_walk_records_the_frontier(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=timeline(["900", "800"], None))

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(route))
    result = collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    session.commit()

    assert result.succeeded is True
    assert result.inserted == 2
    state = paging_state(session, source_id=SOURCE_ID)
    assert state.seen_frontier == 900
    assert state.floor_cursor is None


@pytest.mark.integration
def test_the_second_walk_stops_at_the_frontier(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing new means nothing inserted, and no deep crawl."""
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    # A finite origin: the newest page points deeper, the deeper page ends.
    # Returning the same page forever would model an origin with no bottom, in
    # which case burning the whole budget and leaving a resume behind is the
    # *correct* behaviour -- and made this test assert the wrong thing.
    calls: list[str | None] = []

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body["variables"].get("cursor")
        calls.append(cursor)
        if cursor is None:
            return httpx.Response(200, content=timeline(["900", "800"], "DEEPER"))
        return httpx.Response(200, content=timeline(["700", "600"], None))

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(route))
    client = httpx.Client()
    first = collect_paged(
        session, source=source_row(session), registration=registration, client=client
    )
    session.commit()
    assert first.inserted == 4
    assert calls == [None, "DEEPER"]

    calls.clear()
    second = collect_paged(
        session, source=source_row(session), registration=registration, client=client
    )
    session.commit()

    assert second.inserted == 0
    # The newest page is entirely at or below the frontier, so the walk stops
    # there rather than continuing to the deeper page.
    assert calls == [None]


@pytest.mark.integration
def test_the_frontier_only_moves_forward(session: Session) -> None:
    """A re-walk of old history must not drag the anchor backwards.

    Letting it would make every later cycle re-read content it already has.
    """
    save_paging_state(
        session, source_id=SOURCE_ID, newest_id=1000, floor_candidate=None
    )
    save_paging_state(session, source_id=SOURCE_ID, newest_id=500, floor_candidate=None)
    session.commit()
    assert paging_state(session, source_id=SOURCE_ID).seen_frontier == 1000


@pytest.mark.integration
def test_the_floor_persists_when_a_walk_stops_at_the_frontier(
    session: Session,
) -> None:
    """A routine cycle reports no cursor; the stored floor must survive it.

    Erasing the floor there would uncap the next cycle's history: with no
    stop sign, a budget-exhausted walk crawls below the oldest tweet the
    source ever reached.
    """
    save_paging_state(
        session, source_id=SOURCE_ID, newest_id=1000, floor_candidate="FLOOR-1"
    )
    # A frontier stop hands back no cursor at all.
    save_paging_state(
        session, source_id=SOURCE_ID, newest_id=2000, floor_candidate=None
    )
    session.commit()
    state = paging_state(session, source_id=SOURCE_ID)
    assert state.floor_cursor == "FLOOR-1"
    assert state.seen_frontier == 2000


def test_a_budget_stop_never_moves_the_floor(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A walk that stopped on budget reports a cursor -- but not the floor.

    Cursors are opaque strings, so the host cannot compare positions; the
    walk's reached_floor flag is what says whether the interval's lower end
    was touched. Without the flag the host must write nothing, or a shallow
    budget-stop cursor would overwrite the floor and content between the two
    positions would fall outside the interval the model promises to cover.
    """
    save_paging_state(
        session, source_id=SOURCE_ID, newest_id=1000, floor_candidate="FLOOR-DEEP"
    )
    session.commit()

    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    # Two pages, both above the floor, neither crossing the frontier: the
    # walk ends on budget with its own bottom cursor.
    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body["variables"].get("cursor")
        if cursor is None:
            return httpx.Response(200, content=timeline(["2000"], "C1"))
        return httpx.Response(200, content=timeline(["1500"], "C2"))

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(route))
    collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    session.commit()

    state = paging_state(session, source_id=SOURCE_ID)
    assert state.floor_cursor == "FLOOR-DEEP", (
        "a budget-stop cursor overwrote the floor"
    )
    assert state.seen_frontier == 2000


def test_a_first_walk_establishes_the_floor(session: Session) -> None:
    """Before any walk there is no floor; the first one that leaves a cursor
    sets it."""
    assert paging_state(session, source_id=SOURCE_ID).floor_cursor is None
    save_paging_state(
        session, source_id=SOURCE_ID, newest_id=1000, floor_candidate="FIRST-BOTTOM"
    )
    session.commit()
    assert paging_state(session, source_id=SOURCE_ID).floor_cursor == "FIRST-BOTTOM"


@pytest.mark.integration
def test_a_rejected_session_is_refreshed_and_the_page_retried(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X rotates ct0 on login, so a cached session is exactly what gets 401'd.

    Without the refresh the source stays broken until the process restarts even
    though the browser already synced a working pair. With an unbounded retry it
    would instead hammer the origin while nothing can authorise us, so this
    asserts both halves: it recovers, and it recovers exactly once.
    """
    from adapters import cookiecloud as cc
    from plugins.collectors.twitter import client as x_client

    for key in (x_client.AUTH_TOKEN_ENV, x_client.CT0_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(cc.BASE_URL_ENV, "http://127.0.0.1:4000")
    monkeypatch.setenv(cc.UUID_ENV, "synthetic-uuid")
    monkeypatch.setenv(cc.TOKEN_ENV, "synthetic-master-password")
    cc.invalidate()

    state = {"x_calls": 0, "cc_calls": 0}

    def route(request: httpx.Request) -> httpx.Response:
        from adapters.cookiecloud import parse_cookie_header

        if request.url.path.startswith("/get/"):
            # The browser re-synced between the two lookups: the first hands
            # out the session X is about to reject, the second the fresh one.
            state["cc_calls"] += 1
            issued = "stale" if state["cc_calls"] == 1 else "fresh"
            return httpx.Response(200, text=f"auth_token=a; ct0={issued}")

        state["x_calls"] += 1
        sent = parse_cookie_header(request.headers.get("cookie", ""))
        if sent.get("ct0") != "fresh":
            return httpx.Response(401)
        return httpx.Response(200, content=timeline(["900"], None))

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(route))
    result = collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    session.commit()

    assert result.succeeded is True
    assert result.inserted == 1
    # Two CookieCloud lookups: the cached one that got rejected, then a forced
    # refresh that bypassed the cache.
    assert state["cc_calls"] == 2, state
    assert state["x_calls"] == 2, "one rejection, one retry"


@pytest.mark.integration
def test_a_persistent_rejection_does_not_retry_forever(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One refresh attempt, then the failure stands.

    Looping on a rejection would hammer the origin while the real problem is
    that nobody can authorise us.
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401)

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(route))
    result = collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    assert result.succeeded is False
    assert result.error_class == ErrorClass.AUTH.value
    assert calls["n"] == 1, f"expected a single attempt, got {calls['n']}"


@pytest.mark.integration
def test_collection_result_defaults_cover_paging_fields() -> None:
    """The dataclass is shared with the single-page path; defaults must hold."""
    result = CollectionResult(succeeded=True)
    assert result.last_cursor is None
    assert result.inserted == 0


@pytest.mark.integration
def test_a_busy_catch_up_walks_past_the_first_walk_page_count(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catching up is not capped by the first walk's page policy.

    A cycle whose new tweets span more pages than `PEARSS_X_FIRST_PAGES` must
    keep walking until it reaches the frontier. Stopping at that number
    instead is exactly how content goes missing between cycles: the overflow
    sinks below the top pages before the next tick looks again.
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")
    # A deliberately small first-walk policy: the catch-up must ignore it.
    monkeypatch.setenv(x_client.FIRST_PAGES_ENV, "1")

    # A frontier exists, so this is a catch-up walk.
    save_paging_state(
        session, source_id=SOURCE_ID, newest_id=100, floor_candidate="OLDEST"
    )
    session.commit()

    requested: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body["variables"].get("cursor")
        requested.append(cursor or "NEWEST")
        if cursor is None:
            # Entirely new: nothing at or below the frontier of 100 yet.
            return httpx.Response(200, content=timeline(["900", "800"], "N1"))
        if cursor == "N1":
            return httpx.Response(200, content=timeline(["700", "600"], "N2"))
        if cursor == "N2":
            # This page carries the frontier id, so the walk stops here.
            return httpx.Response(200, content=timeline(["500", "100"], "N3"))
        raise AssertionError(f"walk went past the frontier: {cursor}")

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(route))
    # max_pages=1 is what the production path passes (collect_source's
    # default). The point here is that a catch-up walk ignores it and keeps
    # going until the frontier, rather than stopping after that one page.
    collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
        max_pages=1,
    )
    session.commit()

    assert requested == ["NEWEST", "N1", "N2"], requested
    # 900/800/700/600/500 are new; the page holding the frontier contributes
    # nothing from 100 onwards.
    assert paging_state(session, source_id=SOURCE_ID).seen_frontier == 900
