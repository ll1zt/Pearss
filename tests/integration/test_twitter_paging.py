"""Integration tests for authenticated paging: state, and failing closed.

Scenario register (normative source: SCENARIOS.md; issues #130, #131; AC-117..121).

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
from tests.migrated_schema import migrated_database

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
    url = migrated_database(tmp_path, "paging.db")
    engine = create_engine_for(url)

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


@pytest.mark.integration
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


@pytest.mark.integration
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
            # This page carries the frontier id, but it still contributes 500,
            # so the walk needs one more page to prove it has passed 100: a
            # seen id alone is not an ending (a module can carry a seen parent
            # above newer replies, issue #130).
            return httpx.Response(200, content=timeline(["500", "100"], "N3"))
        if cursor == "N3":
            # Nothing above the frontier: the catch-up ends here.
            return httpx.Response(200, content=timeline(["50", "40"], None))
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

    assert requested == ["NEWEST", "N1", "N2", "N3"], requested
    # 900/800/700/600/500 are new; the page carrying the frontier id still
    # contributes 500, so the walk confirms with one more page that nothing is
    # left above it.
    assert paging_state(session, source_id=SOURCE_ID).seen_frontier == 900


# --------------------------------------------------------------------------
# A module's tweets reach the corpus, not just the parser
# --------------------------------------------------------------------------
#
# The parser tests prove `_tweets` returns module items. That is one hop short
# of the question a user asks ("why is this missing from my reader?"): does the
# tweet survive the walk, the filter, and the store? These drive the whole
# pipeline with a module-shaped page and read the result back from the
# database, which is the only place that can answer it.


def module_timeline(next_cursor: str | None) -> bytes:
    """A page whose only content is a module carrying two replies.

    Shaped like the real `home-conversation` entry: a `TimelineTimelineModule`
    whose `items[].item.itemContent` each hold a complete tweet.
    """

    def item(rid: str, text: str) -> dict[str, object]:
        return {
            "entryId": f"tweet-{rid}",
            "item": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "__typename": "Tweet",
                            "rest_id": rid,
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
                                "full_text": text,
                                "created_at": "Sun Aug 09 16:00:00 +0000 2026",
                                "entities": {"urls": [], "media": []},
                            },
                        }
                    }
                }
            },
        }

    entries: list[dict[str, object]] = [
        {
            "entryId": "home-conversation-500",
            "sortIndex": "500",
            "content": {
                "__typename": "TimelineTimelineModule",
                "items": [item("500", "parent post"), item("499", "a reply")],
            },
        }
    ]
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


def _stored_titles(session: Session) -> list[str]:
    """The stored items' titles, which carry the tweet text.

    `guid` holds a derived identity (a content hash), not the collector's
    external id, so the tweet's own text is the field that says which tweet a
    row came from. Asserting on it is also closer to the user's question --
    "is the reply I expected in my reader?" -- than an id would be.
    """
    return [
        str(row[0])
        for row in session.execute(sa.text("select title from item")).fetchall()
    ]


@pytest.mark.integration
def test_tweets_inside_a_module_are_stored_by_the_full_walk(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both tweets from a module land in the database.

    The end-to-end half of the conversation-module fix: a page whose entire
    content is a module must produce stored items. A regression here is
    invisible to the parser tests, which stop one layer short of the store.
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["variables"].get("cursor") is None:
            return httpx.Response(200, content=module_timeline(None))
        raise AssertionError("the walk should stop at the origin's bottom")

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

    stored = _stored_titles(session)
    assert any("parent post" in t for t in stored), (
        f"the module's parent was lost: {stored}"
    )
    assert any("a reply" in t for t in stored), f"the module's reply was lost: {stored}"


def _tweet_result(rid: str, text: str) -> dict[str, object]:
    return {
        "__typename": "Tweet",
        "rest_id": rid,
        "core": {
            "user_results": {
                "result": {"core": {"name": "Tester", "screen_name": "tester"}}
            }
        },
        "legacy": {
            "full_text": text,
            "created_at": "Sun Aug 09 16:00:00 +0000 2026",
            "entities": {"urls": [], "media": []},
        },
    }


def _top_level_entry(rid: str, text: str) -> dict[str, object]:
    return {
        "entryId": f"tweet-{rid}",
        "sortIndex": rid,
        "content": {
            "itemContent": {"tweet_results": {"result": _tweet_result(rid, text)}}
        },
    }


def _module_entry(module_id: str, items: list[tuple[str, str]]) -> dict[str, object]:
    """A conversation module whose items are laid out parent-then-replies."""
    return {
        "entryId": f"home-conversation-{module_id}",
        "sortIndex": module_id,
        "content": {
            "__typename": "TimelineTimelineModule",
            "items": [
                {
                    "entryId": f"tweet-{rid}",
                    "item": {
                        "itemContent": {
                            "tweet_results": {"result": _tweet_result(rid, text)}
                        }
                    },
                }
                for rid, text in items
            ],
        },
    }


def _page(entries: list[dict[str, object]], next_cursor: str | None) -> bytes:
    if next_cursor:
        entries = [
            *entries,
            {
                "entryId": f"cursor-bottom-{next_cursor}",
                "content": {"value": next_cursor},
            },
        ]
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


@pytest.mark.integration
def test_a_new_reply_is_not_dropped_when_its_parent_is_already_seen(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frontier truncation must not drop a reply that follows its parent.

    A conversation module lists the parent (older, already ingested) before the
    reply (newer). A prefix scan stops at the parent, drops the reply -- and
    every newer tweet after it -- then ends the round, so they are lost for
    good: the frontier is then advanced past them. This drives the whole
    pipeline and reads the corpus back, which is the only place that answers
    "is the reply in my reader?" (AC-117).
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    # 510 is the newest id already ingested; the module's parent (500) is below
    # it, its reply (520) is above it -- newer content, placed after a seen id.
    row = session.get(Source, SOURCE_ID)
    assert row is not None
    row.seen_frontier = 510
    session.commit()

    page1 = _page(
        [
            _top_level_entry("530", "a new top-level post"),
            _module_entry("510", [("500", "the old parent"), ("520", "a new reply")]),
        ],
        "C1",
    )
    page2 = _page(
        [_top_level_entry("400", "old"), _top_level_entry("300", "older")], None
    )

    def route(request: httpx.Request) -> httpx.Response:
        cursor = json.loads(request.content)["variables"].get("cursor")
        if cursor is None:
            return httpx.Response(200, content=page1)
        if cursor == "C1":
            return httpx.Response(200, content=page2)
        raise AssertionError(f"walk went past the frontier: {cursor}")

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

    stored = _stored_titles(session)
    assert any("a new reply" in t for t in stored), (
        f"the newer reply was dropped by the frontier scan: {stored}"
    )
    assert any("a new top-level post" in t for t in stored), (
        f"the newer top-level post was dropped: {stored}"
    )


@pytest.mark.integration
def test_a_repeat_catch_up_neither_duplicates_nor_moves_the_frontier_back(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second catch-up over the same page inserts nothing and keeps the
    frontier (AC-119): the fix must not introduce duplicates or drag the anchor
    backwards."""
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    page = _page(
        [_top_level_entry("530", "first"), _top_level_entry("520", "second")], None
    )

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=page)

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(route))

    first = collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    session.commit()
    assert first.inserted == 2
    assert paging_state(session, source_id=SOURCE_ID).seen_frontier == 530

    second = collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    session.commit()
    assert second.drafts == 0
    assert second.inserted == 0
    assert len(_stored_titles(session)) == 2
    assert paging_state(session, source_id=SOURCE_ID).seen_frontier == 530


@pytest.mark.integration
def test_a_mid_walk_failure_keeps_the_pages_it_already_fetched(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page that fails after earlier ones arrived must not discard them.

    The walk keeps its partial pages by contract; the host was dropping them
    *and* moving the frontier past them, so the fetched tweets were lost for
    good (AC-120).
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    save_paging_state(
        session, source_id=SOURCE_ID, newest_id=100, floor_candidate="OLDEST"
    )
    session.commit()

    def route(request: httpx.Request) -> httpx.Response:
        cursor = json.loads(request.content)["variables"].get("cursor")
        if cursor is None:
            return httpx.Response(200, content=timeline(["300", "200"], "C1"))
        # The second page fails after the first one already arrived.
        return httpx.Response(500)

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

    assert result.succeeded is False
    stored = _stored_titles(session)
    assert any("body of 300" in t for t in stored), (
        f"the fetched first page was discarded after a later failure: {stored}"
    )
    assert any("body of 200" in t for t in stored), (
        f"the fetched first page was discarded after a later failure: {stored}"
    )


@pytest.mark.integration
def test_a_partial_failure_does_not_skip_the_unfetched_page(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frontier must not move past content that was never fetched.

    If it does, the next round starts at the top, meets the moved frontier and
    stops -- the tweet on the page that failed is skipped for good (AC-121).
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")

    save_paging_state(
        session, source_id=SOURCE_ID, newest_id=100, floor_candidate="OLDEST"
    )
    session.commit()

    def failing_route(request: httpx.Request) -> httpx.Response:
        cursor = json.loads(request.content)["variables"].get("cursor")
        if cursor is None:
            return httpx.Response(200, content=timeline(["300", "200"], "C1"))
        return httpx.Response(500)

    registration = collector_registry.by_name("twitter")
    assert registration is not None
    collector_registry.set_http_transport("twitter", httpx.MockTransport(failing_route))
    collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    session.commit()

    # The frontier still names the last id the store actually knows about.
    assert paging_state(session, source_id=SOURCE_ID).seen_frontier == 100

    def recovered_route(request: httpx.Request) -> httpx.Response:
        cursor = json.loads(request.content)["variables"].get("cursor")
        if cursor is None:
            return httpx.Response(200, content=timeline(["300", "200"], "C1"))
        if cursor == "C1":
            return httpx.Response(200, content=timeline(["150", "50"], None))
        raise AssertionError(f"walk went past the origin bottom: {cursor}")

    collector_registry.set_http_transport(
        "twitter", httpx.MockTransport(recovered_route)
    )
    collect_paged(
        session,
        source=source_row(session),
        registration=registration,
        client=httpx.Client(),
    )
    session.commit()

    stored = _stored_titles(session)
    assert any("body of 150" in t for t in stored), (
        f"a newer tweet below a failed page was skipped for good: {stored}"
    )
    assert paging_state(session, source_id=SOURCE_ID).seen_frontier == 300


@pytest.mark.integration
def test_a_module_under_an_unknown_prefix_is_stored_too(
    session: Session, storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same walk with a prefix the parser has never been taught.

    This is the shape the allowlist dropped silently. Pinning it at the store
    level is the behavioural statement that matters: the tweet is not merely
    parsed, it is in the corpus and therefore visible to readers.
    """
    from plugins.collectors.twitter import client as x_client

    monkeypatch.setenv(x_client.AUTH_TOKEN_ENV, "synthetic-auth")
    monkeypatch.setenv(x_client.CT0_ENV, "synthetic-ct0")
    page = module_timeline(None).replace(
        b"home-conversation-500", b"profile-conversation-500"
    )

    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["variables"].get("cursor") is None:
            return httpx.Response(200, content=page)
        raise AssertionError("the walk should stop at the origin's bottom")

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

    stored = _stored_titles(session)
    assert any("parent post" in t for t in stored), (
        f"unknown prefix lost content: {stored}"
    )
    assert any("a reply" in t for t in stored), f"unknown prefix lost content: {stored}"
