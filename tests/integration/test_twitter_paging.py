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
from app.pipeline import CollectionResult, collect_authenticated_pages
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
    engine = sa.create_engine(url)
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
                resume_rounds=0,
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

    result = collect_authenticated_pages(
        session,
        source=source_row(session),
        client=httpx.Client(transport=httpx.MockTransport(never)),
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

    result = collect_authenticated_pages(
        session,
        source=source_row(session),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(429))
        ),
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

    result = collect_authenticated_pages(
        session,
        source=source_row(session),
        client=httpx.Client(transport=httpx.MockTransport(route)),
    )
    session.commit()

    assert result.succeeded is True
    assert result.inserted == 2
    state = paging_state(session, source_id=SOURCE_ID)
    assert state.seen_frontier == 900
    assert state.resume_cursor is None


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

    client = httpx.Client(transport=httpx.MockTransport(route))
    first = collect_authenticated_pages(
        session, source=source_row(session), client=client
    )
    session.commit()
    assert first.inserted == 4
    assert calls == [None, "DEEPER"]

    calls.clear()
    second = collect_authenticated_pages(
        session, source=source_row(session), client=client
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
    save_paging_state(session, source_id=SOURCE_ID, newest_id=1000, resume_cursor=None)
    save_paging_state(session, source_id=SOURCE_ID, newest_id=500, resume_cursor=None)
    session.commit()
    assert paging_state(session, source_id=SOURCE_ID).seen_frontier == 1000


@pytest.mark.integration
def test_a_stuck_resume_ages_and_a_moving_one_does_not(session: Session) -> None:
    """The bound exists so a backlog cannot slide into the past forever."""
    save_paging_state(session, source_id=SOURCE_ID, newest_id=10, resume_cursor="GAP-1")
    session.commit()
    assert paging_state(session, source_id=SOURCE_ID).resume_rounds == 1

    # Same cursor again: unresolved, so it ages.
    save_paging_state(session, source_id=SOURCE_ID, newest_id=10, resume_cursor="GAP-1")
    session.commit()
    assert paging_state(session, source_id=SOURCE_ID).resume_rounds == 2

    # A different cursor is newly discovered work, not another round of old.
    save_paging_state(session, source_id=SOURCE_ID, newest_id=10, resume_cursor="GAP-2")
    session.commit()
    assert paging_state(session, source_id=SOURCE_ID).resume_rounds == 1

    # Clearing it resets the count.
    save_paging_state(session, source_id=SOURCE_ID, newest_id=10, resume_cursor=None)
    session.commit()
    state = paging_state(session, source_id=SOURCE_ID)
    assert state.resume_rounds == 0 and state.resume_cursor is None


@pytest.mark.integration
def test_a_dropped_resume_lets_the_walk_restart_from_the_newest(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the bound, the cycle stops chasing deep history."""
    from plugins.collectors.twitter import client as x_client
    from plugins.collectors.twitter.paging import MAX_RESUME_ROUNDS

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    save_paging_state(
        session,
        source_id=SOURCE_ID,
        newest_id=1000,
        resume_cursor="ANCIENT",
    )
    row = source_row(session)
    row.resume_rounds = MAX_RESUME_ROUNDS
    session.commit()

    requested: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cursor = body["variables"].get("cursor")
        requested.append(cursor or "NEWEST")
        return httpx.Response(200, content=timeline(["2000"], None))

    collect_authenticated_pages(
        session,
        source=source_row(session),
        client=httpx.Client(transport=httpx.MockTransport(route)),
    )
    session.commit()

    assert requested == ["NEWEST"], f"stale resume was still used: {requested}"


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
    monkeypatch.delenv(cc.DOMAIN_ENV, raising=False)
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

    result = collect_authenticated_pages(
        session,
        source=source_row(session),
        client=httpx.Client(transport=httpx.MockTransport(route)),
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

    result = collect_authenticated_pages(
        session,
        source=source_row(session),
        client=httpx.Client(transport=httpx.MockTransport(route)),
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
