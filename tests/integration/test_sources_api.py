"""Integration tests for the /v1/sources Core API.

These pin the contract the style guide publishes: cursor pagination, a
mandatory Idempotency-Key on writes, one error envelope for every failure, and
scope separation between the read and write tokens.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app, create_engine_for
from app.middleware import CorrelationAndAuditMiddleware
from tests.integration.conftest import sweep_until_terminal
from tests.migrated_schema import migrated_database

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE_TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"
FEED_URL = "https://planetpython.org/rss20.xml"
NOW = "2026-01-01T00:00:00.000000Z"


def policy(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"type": "fixed", "interval": "30m"}
    base.update(overrides)
    return base


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "v1.db")

    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    return create_app(session_factory)


@pytest_asyncio.fixture
async def writer(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


@pytest_asyncio.fixture
async def reader(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {READ_TOKEN}"},
    ) as opened:
        yield opened


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_legacy_response_survives_migration_and_remains_replayable(
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A pre-fingerprint response survives upgrade with its replay contract."""
    from adapters.db_models import IdempotencyRecord
    from adapters.repository import store_idempotent

    with session_factory() as session:
        store_idempotent(
            session,
            actor="api:full",
            endpoint="POST /v1/sources",
            key="legacy",
            status_code=201,
            response_body='{"id":"legacy-source"}',
            created_at=NOW,
            expires_at="2099-01-01T00:00:00.000000Z",
        )
        session.commit()
        url = session.connection().engine.url
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", str(url))
    command.downgrade(config, "0019")
    command.upgrade(config, "0020")
    with session_factory() as session:
        row = session.get(IdempotencyRecord, ("api:full", "POST /v1/sources", "legacy"))
        assert row is not None
        assert (
            row.fingerprint,
            row.owner_token,
            row.response_body,
            row.created_at,
        ) == (
            None,
            None,
            '{"id":"legacy-source"}',
            NOW,
        )
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "legacy"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    assert response.status_code == 201
    assert response.json() == {"id": "legacy-source"}
    assert response.headers["Idempotent-Replay"] == "true"
    assert (await writer.get("/v1/sources")).json()["data"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_identical_retry_normalizes_json_and_replays_without_mutation(
    writer: httpx.AsyncClient,
) -> None:
    """Given a completed PATCH, reordered JSON replays its exact durable result."""
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "seed"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    path = f"/v1/sources/{created.json()['id']}"
    headers = {"Idempotency-Key": "normalized", "Content-Type": "application/json"}
    first = await writer.patch(
        path, headers=headers, json={"folder": "Pinned", "status": "paused"}
    )
    retry = await writer.patch(
        path, headers=headers, content=' { "status":"paused", "folder" : "Pinned" } '
    )
    assert first.status_code == retry.status_code == 200
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replay"] == "true"
    assert (await writer.get(path)).json()["updated_at"] == first.json()["updated_at"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_overlapping_retry_does_not_enter_the_mutation_window(
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """With a mutation committed and held, a retry gets 409, not another write."""
    from adapters.db_models import Source

    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "seed"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    path = f"/v1/sources/{created.json()['id']}"
    committed, release = threading.Event(), threading.Event()

    def mark(session: Session, *_: object) -> None:
        if any(
            isinstance(row, Source) and row.folder == "Held" for row in session.dirty
        ):
            session.info["hold_response"] = True

    def hold(session: Session) -> None:
        if session.info.pop("hold_response", False) and not committed.is_set():
            committed.set()
            if not release.wait(10):
                raise TimeoutError("test did not release mutation")

    sa.event.listen(Session, "before_flush", mark)
    sa.event.listen(Session, "after_commit", hold)
    first = asyncio.create_task(
        writer.patch(
            path, headers={"Idempotency-Key": "overlap"}, json={"folder": "Held"}
        )
    )
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        second = await asyncio.wait_for(
            writer.patch(
                path, headers={"Idempotency-Key": "overlap"}, json={"folder": "Held"}
            ),
            3,
        )
        assert second.status_code == 409
        assert second.json()["detail"]["error"]["code"] == "idempotency_in_progress"
        assert "Idempotent-Replay" not in second.headers
    finally:
        release.set()
        result = await first
        sa.event.remove(Session, "before_flush", mark)
        sa.event.remove(Session, "after_commit", hold)
    assert result.status_code == 200
    assert (await writer.get(path)).json()["updated_at"] == result.json()["updated_at"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_crash_after_mutation_never_reexecutes_or_replays_success(
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """After committed state with no response, retries fail closed after expiry."""
    from adapters.db_models import Source

    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "seed"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    path = f"/v1/sources/{created.json()['id']}"
    headers = {"Idempotency-Key": "crashed"}

    def mark(session: Session, *_: object) -> None:
        if any(
            isinstance(row, Source) and row.folder == "Committed"
            for row in session.dirty
        ):
            session.info["crash_response"] = True

    def crash(session: Session) -> None:
        if session.info.pop("crash_response", False):
            raise RuntimeError("injected crash after business commit")

    sa.event.listen(Session, "before_flush", mark)
    sa.event.listen(Session, "after_commit", crash)
    try:
        with pytest.raises(RuntimeError, match="injected crash"):
            await writer.patch(path, headers=headers, json={"folder": "Committed"})
    finally:
        sa.event.remove(Session, "before_flush", mark)
        sa.event.remove(Session, "after_commit", crash)
    before = (await writer.get(path)).json()
    assert before["folder"] == "Committed"
    retry = await writer.patch(path, headers=headers, json={"folder": "Committed"})
    assert retry.status_code == 409
    assert retry.json()["detail"]["error"]["code"] == "idempotency_in_progress"
    assert "Idempotent-Replay" not in retry.headers
    # Advance persisted deadline, not wall-clock sleeps; never re-admit unknown work.
    from adapters.db_models import IdempotencyRecord

    with session_factory() as session:
        row = session.get(IdempotencyRecord, ("api:full", f"PATCH {path}", "crashed"))
        assert row is not None
        row.expires_at = "2000-01-01T00:00:00.000000Z"
        session.commit()
    recovered = await writer.patch(path, headers=headers, json={"folder": "Committed"})
    assert recovered.status_code == 409
    assert recovered.json()["detail"]["error"]["code"] == "idempotency_outcome_unknown"
    assert "Idempotent-Replay" not in recovered.headers
    assert (await writer.get(path)).json()["updated_at"] == before["updated_at"]


# --------------------------------------------------------------------------
# Authentication and scopes
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_requires_a_token(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        assert (await anonymous.get("/v1/sources")).status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_token_cannot_write(reader: httpx.AsyncClient) -> None:
    response = await reader.post(
        "/v1/sources",
        headers={"Idempotency-Key": "k"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/sources"),
        ("PATCH", "/v1/sources/src-1"),
        ("DELETE", "/v1/sources/src-1"),
        ("POST", "/v1/sources/src-1:trigger"),
        ("POST", "/v1/sources/src-1:backfill-chain"),
        ("POST", "/v1/sources/src-1:retry-enrichments"),
    ],
)
async def test_read_token_is_refused_on_every_write_route(
    reader: httpx.AsyncClient, method: str, path: str
) -> None:
    """Scope is enforced per route, not just on POST /v1/sources.

    A single-route check leaves every sibling write one refactor away from
    losing its scope guard. The parametrisation is the point: each verb the
    sources surface exposes is refused for the read token.
    """
    response = await reader.request(
        method,
        path,
        headers={"Idempotency-Key": f"scope-{method}-{path}"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    assert response.status_code == 403, (method, path, response.status_code)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/sources",
        "/v1/sources/src-1",
        "/v1/sources/src-1/enrichments",
        "/v1/items",
        "/v1/items/item-1",
        "/v1/jobs",
        "/v1/system/snapshot",
        "/v1/plugins",
        "/v1/plugins/rss",
    ],
)
async def test_read_token_can_read_every_read_route(
    reader: httpx.AsyncClient, path: str
) -> None:
    """The read token's whole purpose: every read route accepts it.

    Asserted as "not 401/403" rather than 200, because an empty collection or
    a missing row is a legitimate answer -- what must never happen is a scope
    rejection on a read.
    """
    response = await reader.get(path)
    assert response.status_code not in (401, 403), (path, response.status_code)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_lists_every_status_including_zero_counts(
    writer: httpx.AsyncClient,
) -> None:
    """A missing key cannot be told apart from a status that is empty.

    Omitting zeros cost a real misreading: `pending` was absent while thousands
    of `ready` items were present, and a client labelled the whole set "pending"
    because the one status that contradicted that was not in the payload.
    """
    from core.enums import ErrorClass, JobRunStatus, PublishStatus, SourceStatus

    response = await writer.get("/v1/system/snapshot")
    assert response.status_code == 200
    body = response.json()

    expected = {
        "sources_by_status": SourceStatus,
        "items_by_publish_status": PublishStatus,
        "runs_by_status": JobRunStatus,
        "runs_by_error_class": ErrorClass,
    }
    for key, enum in expected.items():
        present = set(body[key])
        missing = {member.value for member in enum} - present
        assert not missing, f"{key} omits {sorted(missing)}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_reports_pending_separately_from_ready(
    writer: httpx.AsyncClient,
) -> None:
    """The two states mean opposite things and must not merge into one number."""
    body = (await writer.get("/v1/system/snapshot")).json()
    statuses = body["items_by_publish_status"]
    assert "pending" in statuses and "ready" in statuses
    assert statuses["pending"] == 0


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_without_idempotency_key_is_refused(
    writer: httpx.AsyncClient,
) -> None:
    response = await writer.post(
        "/v1/sources", json={"url": FEED_URL, "schedule_policy": policy()}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "idempotency_key_required"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_with_same_key_creates_one_source(
    writer: httpx.AsyncClient,
) -> None:
    body = {"url": FEED_URL, "schedule_policy": policy()}
    first = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "same"}, json=body
    )
    second = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "same"}, json=body
    )
    assert first.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    # Marked so a caller can tell a replay from a fresh execution.
    assert second.headers.get("idempotent-replay") == "true"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_url_under_a_new_key_does_not_duplicate(
    writer: httpx.AsyncClient,
) -> None:
    body = {"url": FEED_URL, "schedule_policy": policy()}
    first = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "a"}, json=body
    )
    second = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "b"}, json=body
    )
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_read_token_cannot_write_even_with_a_known_key(
    writer: httpx.AsyncClient, reader: httpx.AsyncClient
) -> None:
    """The read token is refused before idempotency is consulted.

    Named for what it actually proves. An earlier version of this test was
    called `test_keys_are_scoped_per_actor` and claimed to show that a key is
    not shared between principals -- but the 403 comes from EarlyAuth, before
    the route body or the idempotency lookup runs, so it never exercised key
    scoping at all. That property is verified at its own layer in
    tests/integration/test_idempotency.py, where two actors can be constructed.
    """
    body = {"url": FEED_URL, "schedule_policy": policy()}
    created = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "shared"}, json=body
    )
    assert created.status_code == 201

    refused = await reader.post(
        "/v1/sources", headers={"Idempotency-Key": "shared"}, json=body
    )
    assert refused.status_code == 403
    assert refused.headers.get("idempotent-replay") is None


# --------------------------------------------------------------------------
# Canonicalisation and validation
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tracking_parameters_are_stripped_from_the_dedupe_key(
    writer: httpx.AsyncClient,
) -> None:
    with_tracking = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "with"},
        json={
            "url": f"{FEED_URL}?utm_source=feed",
            "schedule_policy": policy(),
        },
    )
    assert with_tracking.json()["canonical_url"] == FEED_URL
    # The original is echoed so an operator sees what they asked for.
    assert "utm_source" in with_tracking.json()["url"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_policy",
    [
        {},
        {"type": "fixed"},
        {"type": "adaptive", "min": "15m"},
        {"type": "nonsense", "interval": "30m"},
        # A typo'd key must be refused, not silently ignored: accepting it
        # would store a policy with no interval.
        {"type": "fixed", "interwal": "30m"},
        {"type": "fixed", "interval": "soon"},
    ],
)
async def test_invalid_schedule_policies_are_refused(
    writer: httpx.AsyncClient, bad_policy: dict[str, object]
) -> None:
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": f"bad-{abs(hash(str(bad_policy)))}"},
        json={"url": "https://x.example/feed.xml", "schedule_policy": bad_policy},
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unusable_url_is_refused(writer: httpx.AsyncClient) -> None:
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "u"},
        json={"url": "not a url", "schedule_policy": policy()},
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_agent_source_can_be_created(writer: httpx.AsyncClient) -> None:
    """An `agent://` URL is valid for an agent source (ADR 0022).

    Regression: the URL check was a field validator that read `type` from
    `info.data`, but `url` is declared first, so `type` was absent and the
    check ran against its RSS default. Every agent source was refused as
    "unsupported URL scheme: 'agent'" -- A3 could not be configured through
    the API at all, which went unnoticed because the only write path that
    existed was the scaffold binding ItemDraft directly.
    """
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "agent-create"},
        json={"url": "agent://intake", "type": "agent", "schedule_policy": policy()},
    )
    assert response.status_code == 201, response.text
    assert response.json()["canonical_url"] == "agent://intake"
    assert response.json()["type"] == "agent"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_agent_url_is_still_refused_for_a_non_agent_source(
    writer: httpx.AsyncClient,
) -> None:
    """The type still decides; the check only stopped depending on field order.

    An `agent://` URL on an rss source is a mistake, and accepting it would
    create a source no collector can serve.
    """
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "agent-on-rss"},
        json={"url": "agent://intake", "type": "rss", "schedule_policy": policy()},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Reads, updates, deletes
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_created_source_can_be_read_back(writer: httpx.AsyncClient) -> None:
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "read-back"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]
    fetched = await writer.get(f"/v1/sources/{source_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == source_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_source_is_404_with_the_envelope(
    writer: httpx.AsyncClient,
) -> None:
    response = await writer.get("/v1/sources/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "source_not_found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_patch_is_refused(writer: httpx.AsyncClient) -> None:
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "patch"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    response = await writer.patch(
        f"/v1/sources/{created.json()['id']}",
        headers={"Idempotency-Key": "patch-2"},
        json={},
    )
    assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_transitions(writer: httpx.AsyncClient) -> None:
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "status"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]

    paused = await writer.patch(
        f"/v1/sources/{source_id}",
        headers={"Idempotency-Key": "pause"},
        json={"status": "paused"},
    )
    assert paused.json()["status"] == "paused"

    # A paused source must not be collected: the state machine forbids it, and
    # silently fetching would ignore an operator's explicit pause.
    refused = await writer.post(
        f"/v1/sources/{source_id}:trigger",
        headers={"Idempotency-Key": "trig"},
    )
    assert refused.status_code == 409

    deleted = await writer.delete(
        f"/v1/sources/{source_id}", headers={"Idempotency-Key": "del"}
    )
    assert deleted.status_code == 200
    assert deleted.json()["sources"] == 1, "removal deletes the row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_removes_the_source_and_its_items(
    app: FastAPI, writer: httpx.AsyncClient
) -> None:
    """Deletion is physical: the source, its items and their read state go.

    This replaces a test that asserted the opposite -- that items survive
    their source's removal. Deletion now removes them (ADR 0021), and the
    items are seeded here for the same reason as before: a cascade with
    nothing to cascade to would pass on an empty set.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "hard"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]

    with app.state.session_factory() as session:
        for index in range(2):
            session.execute(
                sa.text(
                    "insert into item (id, source_id, guid, content_hash, title,"
                    " url, publish_status, tenant_id, created_at, updated_at)"
                    " values (:id, :s, :g, :h, :t, :u, 'published', 'default',"
                    " '2026-01-01T00:00:00.000000Z',"
                    " '2026-01-01T00:00:00.000000Z')"
                ),
                {
                    "id": f"hard-item-{index}",
                    "s": source_id,
                    "g": f"hard-guid-{index}",
                    "h": f"hard-hash-{index}",
                    "t": f"Gone {index}",
                    "u": f"https://example.com/gone/{index}",
                },
            )
        # Read state is the row a bare DELETE would strand if the database is
        # not enforcing the schema's cascade.
        session.execute(
            sa.text(
                "insert into read_state (item_id, is_read, is_starred, tenant_id,"
                " updated_at) values ('hard-item-0', 1, 0, 'default',"
                " '2026-01-01T00:00:00.000000Z')"
            )
        )
        session.commit()

    response = await writer.delete(
        f"/v1/sources/{source_id}", headers={"Idempotency-Key": "d"}
    )
    assert response.status_code == 200
    counts = response.json()
    assert counts["sources"] == 1
    assert counts["items"] == 2

    with app.state.session_factory() as session:
        sources = session.execute(
            sa.text("select count(*) from source where id = :i"), {"i": source_id}
        ).scalar()
        items = session.execute(
            sa.text("select count(*) from item where source_id = :i"), {"i": source_id}
        ).scalar()
        states = session.execute(
            sa.text(
                "select count(*) from read_state where item_id in"
                " ('hard-item-0', 'hard-item-1')"
            )
        ).scalar()
    assert sources == 0, "the source row is gone, not merely marked"
    assert items == 0, "its items go with it"
    assert states == 0, "and so does their read state"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deleting_a_source_twice_is_not_an_error(
    writer: httpx.AsyncClient,
) -> None:
    """Idempotence: a retry must not turn a success into a 404.

    A client whose response was lost cannot tell "I already deleted it" from
    "it was never there", and after the first call those are the same fact.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "twice-1"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]

    first = await writer.delete(
        f"/v1/sources/{source_id}", headers={"Idempotency-Key": "twice-del-a"}
    )
    second = await writer.delete(
        f"/v1/sources/{source_id}", headers={"Idempotency-Key": "twice-del-b"}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["sources"] == 1
    assert second.json()["sources"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readding_a_deleted_source_creates_a_fresh_one(
    writer: httpx.AsyncClient,
) -> None:
    """Re-subscribing after deletion starts over, cursor included.

    Replaces a test that pinned reactivation of a tombstone. Deletion removes
    the row, so there is nothing to revive: the new source has the same
    identity (it is derived from the canonical URL) but no history depth, and
    its next collection re-samples from the first page. That is the cost of
    deleting rather than the cost of a bug -- a caller who only wants the
    items gone should clear them and keep the source.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "readd-1"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]
    deleted = await writer.delete(
        f"/v1/sources/{source_id}", headers={"Idempotency-Key": "readd-del"}
    )
    assert deleted.json()["sources"] == 1

    readded = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "readd-2"},
        json={
            "url": FEED_URL,
            "schedule_policy": {"type": "fixed", "interval": "2h"},
        },
    )
    assert readded.status_code == 201
    body = readded.json()
    assert body["id"] == source_id, "identity is derived, so it is the same id"
    assert body["status"] == "active"
    assert body["schedule_policy"] == {"type": "fixed", "interval": "2h"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clearing_a_sources_items_keeps_the_subscription(
    app: FastAPI,
    writer: httpx.AsyncClient,
) -> None:
    """The "bad crawl, keep collecting" case (ADR 0021).

    The source survives with its cursor intact, so the next walk resumes
    instead of re-fetching history -- which is what distinguishes this from
    delete-and-resubscribe.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "clear-1"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]
    with app.state.session_factory() as session:
        session.execute(
            sa.text("update source set seen_frontier = 999 where id = :i"),
            {"i": source_id},
        )
        session.execute(
            sa.text(
                "insert into item (id, source_id, guid, content_hash, title, url,"
                " publish_status, tenant_id, created_at, updated_at)"
                " values ('clear-item', :s, 'clear-guid', 'clear-hash', 'Bad',"
                " 'https://example.com/bad', 'published', 'default',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
            ),
            {"s": source_id},
        )
        session.commit()

    cleared = await writer.post(
        f"/v1/sources/{source_id}/items:delete",
        headers={"Idempotency-Key": "clear-2"},
        json={},
    )
    assert cleared.status_code == 200
    assert cleared.json()["items"] == 1

    still = await writer.get(f"/v1/sources/{source_id}")
    assert still.status_code == 200, "the subscription remains"
    with app.state.session_factory() as session:
        frontier = session.execute(
            sa.text("select seen_frontier from source where id = :i"),
            {"i": source_id},
        ).scalar()
        items = session.execute(
            sa.text("select count(*) from item where source_id = :i"),
            {"i": source_id},
        ).scalar()
    assert items == 0
    assert frontier == 999, "the cursor survives, so collection resumes"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pagination_walks_every_source_exactly_once(
    writer: httpx.AsyncClient,
) -> None:
    """A full walk yields each source once, in creation order, and terminates.

    The sources endpoint pages by offset (bounded, append-only collection),
    so this pins the contract that actually holds: stable ordering, no
    repeats, no skips, and a cursor that reaches None. The property offset
    paging *cannot* give -- surviving an insert ahead of the cursor -- is
    tested for the items endpoint, where insertion ahead is the norm; here a
    source is appended at the end of the ordering, which is why the same test
    would not discriminate between the two schemes.
    """
    created: list[str] = []
    for index in range(3):
        response = await writer.post(
            "/v1/sources",
            headers={"Idempotency-Key": f"page-{index}"},
            json={
                "url": f"https://example.com/feed{index}.xml",
                "schedule_policy": policy(),
            },
        )
        assert response.status_code == 201
        created.append(response.json()["id"])

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = (await writer.get("/v1/sources", params=params)).json()
        seen.extend(item["id"] for item in page["data"])
        cursor = page["next_cursor"]
        pages += 1
        if cursor is None:
            break

    assert cursor is None, "the walk must terminate, not spin forever"
    assert len(seen) == len(set(seen)), "paging must not repeat a row"
    assert set(seen) == set(created), "paging must not skip a row"
    assert seen == created, "creation order is the documented page order"
    assert pages == 3, "limit=1 over 3 rows is 3 pages"

    # A source appended after the walk is not lost: it simply sorts last and
    # shows up on a fresh walk. This is the append-only assumption the offset
    # cursor relies on.
    late = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "page-late"},
        json={"url": "https://example.com/late.xml", "schedule_policy": policy()},
    )
    assert late.status_code == 201
    fresh = (await writer.get("/v1/sources", params={"limit": 50})).json()
    assert [row["id"] for row in fresh["data"]] == [*created, late.json()["id"]]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_cursor_is_refused(writer: httpx.AsyncClient) -> None:
    response = await writer.get("/v1/sources", params={"cursor": "not-a-cursor!!"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "invalid_cursor"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 101, 500])
async def test_limit_above_or_below_the_contract_is_refused(
    writer: httpx.AsyncClient, limit: int
) -> None:
    """The page size is bounded on both sides.

    Only testing 500 leaves the lower bound and the exact upper boundary
    unverified: a regression from `le=100` to `le=500` would still refuse 500
    only by accident, and `0` or negative would slip through to the query.
    """
    response = await writer.get("/v1/sources", params={"limit": limit})
    assert response.status_code == 422, (limit, response.status_code)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_at_the_boundary_is_accepted(writer: httpx.AsyncClient) -> None:
    """The exact bounds (1 and 100) are inside the contract, not outside it."""
    for limit in (1, 100):
        response = await writer.get("/v1/sources", params={"limit": limit})
        assert response.status_code == 200, (limit, response.status_code)


# --------------------------------------------------------------------------
# Envelope and correlation
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_error_uses_one_envelope(writer: httpx.AsyncClient) -> None:
    failures = [
        await writer.get("/v1/sources/nope"),
        await writer.post(
            "/v1/sources", json={"url": FEED_URL, "schedule_policy": policy()}
        ),
        await writer.get("/v1/sources", params={"cursor": "!!"}),
    ]
    for response in failures:
        error = response.json()["detail"]["error"]
        assert set(error) == {
            "code",
            "message",
            "error_class",
            "suggestion",
            "details",
            "correlation_id",
        }
        assert error["correlation_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validation_failure_uses_the_same_envelope(
    writer: httpx.AsyncClient,
) -> None:
    # Without a handler for RequestValidationError this returns Starlette's
    # default shape, giving one API two error formats.
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "v"},
        json={"url": FEED_URL},
    )
    assert response.status_code == 422
    assert "error" in response.json()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openapi_documents_the_core_api(writer: httpx.AsyncClient) -> None:
    spec = (await writer.get("/openapi.json")).json()
    paths = spec["paths"]
    assert "/v1/sources" in paths
    assert "get" in paths["/v1/sources"] and "post" in paths["/v1/sources"]
    assert any(":trigger" in path for path in paths)


# --------------------------------------------------------------------------
# Triggering records a run
# --------------------------------------------------------------------------


@pytest.fixture
def with_mock_client(app: FastAPI) -> Iterator[FastAPI]:
    """Install a shared HTTP client, as the lifespan would.

    The trigger path needs one, and an ASGITransport does not run the
    lifespan that creates it. A fixture supplies it explicitly rather than
    letting the endpoint fall back to something, so the production path stays
    the only way to get a client.
    """
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        b"<item><title>Triggered post</title>"
        b"<link>https://trig.example.com/1</link><guid>trig-1</guid>"
        b"<pubDate>Mon, 01 Jun 2026 10:00:00 +0000</pubDate>"
        b"<description>Body.</description></item></channel></rss>"
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    app.state.http_client = client
    try:
        yield app
    finally:
        del app.state.http_client


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trigger_records_a_job_run(
    with_mock_client: FastAPI,
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A manual trigger must be as observable as a scheduled one.

    It used to call the pipeline helper directly, so the source was collected
    but no JobRun existed: list_runs showed nothing and the backoff, which
    reads run history, could not see the attempt.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "run-seed"},
        json={"url": FEED_URL, "schedule_policy": {"type": "fixed", "interval": "30m"}},
    )
    source_id = created.json()["id"]

    response = await writer.post(
        f"/v1/sources/{source_id}:trigger", headers={"Idempotency-Key": "run-1"}
    )
    assert response.status_code == 200, response.text

    with session_factory() as session:
        jobs = session.execute(sa.text("select id from job")).fetchall()
        runs = session.execute(
            sa.text("select status, error_class from job_run")
        ).fetchall()
    assert jobs, "triggering should create the collect job"
    assert runs, "triggering should record a run"
    # The mock client serves a valid feed, so the outcome is deterministic; a
    # set-valued assertion would accept a regression that fails every run.
    assert runs[0][0] == "succeeded", runs
    assert runs[0][1] is None, runs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trigger_reports_the_run_status(
    with_mock_client: FastAPI,
    writer: httpx.AsyncClient,
) -> None:
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "rs-seed"},
        json={"url": FEED_URL, "schedule_policy": {"type": "fixed", "interval": "30m"}},
    )
    response = await writer.post(
        f"/v1/sources/{created.json()['id']}:trigger",
        headers={"Idempotency-Key": "rs-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_status"] == "succeeded", response.json()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trigger_without_a_lifespan_says_what_is_missing(
    app: FastAPI,
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A bare ASGI transport skips the lifespan, so there is no shared client.

    The old behaviour was an AttributeError surfacing as an opaque 500.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "nl-seed"},
        json={"url": FEED_URL, "schedule_policy": {"type": "fixed", "interval": "30m"}},
    )
    # The fixture app is built without a running lifespan, so http_client is
    # absent unless something else installed it.
    if hasattr(app.state, "http_client"):
        pytest.skip("this app instance has a client; the guard needs one that does not")

    response = await writer.post(
        f"/v1/sources/{created.json()['id']}:trigger",
        headers={"Idempotency-Key": "nl-1"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "http_client_unavailable"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replayed_response_redacts_url_queries(
    writer: httpx.AsyncClient,
) -> None:
    """The replay strips URL queries; the fresh response carried them.

    Source URLs routinely embed access tokens in their query string. The
    fresh execution's response echoes the URL as given (the operator needs
    to see what they sent), but the idempotency store keeps that body for a
    week - long enough for a leaked key to matter. A replay therefore hands
    back the URL without its query: a caller that needs it again fetches the
    resource under its own authenticated request.
    """
    token_url = "https://example.com/feed?token=secret-token-value"
    body = {"url": token_url, "schedule_policy": policy()}
    first = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "redact-1"}, json=body
    )
    assert first.status_code == 201
    assert first.json()["url"] == token_url, (
        "the fresh response must echo the URL as sent"
    )

    replay = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "redact-1"}, json=body
    )
    assert replay.headers.get("idempotent-replay") == "true"
    assert replay.json()["url"] == "https://example.com/feed"
    assert "secret-token-value" not in replay.text
    assert replay.json()["canonical_url"] == "https://example.com/feed"

    # Non-URL strings with a question mark are data, not addresses.
    replayed_policy = replay.json()["schedule_policy"]
    assert replayed_policy == first.json()["schedule_policy"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redaction_leaves_queryless_urls_and_nested_values_alone(
    writer: httpx.AsyncClient,
) -> None:
    """Only `scheme://...?query` shapes are touched."""
    body = {"url": "https://example.com/plain-feed", "schedule_policy": policy()}
    first = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "redact-2"}, json=body
    )
    replay = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "redact-2"}, json=body
    )
    assert first.json()["url"] == replay.json()["url"]
    assert replay.json()["url"] == "https://example.com/plain-feed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_and_patch_carry_the_processing_chain(
    writer: httpx.AsyncClient,
) -> None:
    """The chain config round-trips through create and PATCH, with the
    pipeline validating specs before anything gates on them."""
    chain = [{"plugin_id": "translate", "kind": "translation"}]
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "chain-create-1"},
        json={
            "url": "https://chain.example/feed",
            "schedule_policy": policy(),
            "pipeline_chain": chain,
            "target_lang": "en",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["pipeline_chain"] == chain
    assert created.json()["target_lang"] == "en"

    # Clearing: null chain returns the source to P1 behaviour.
    cleared = await writer.patch(
        f"/v1/sources/{created.json()['id']}",
        headers={"Idempotency-Key": "chain-clear-1"},
        json={"pipeline_chain": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["pipeline_chain"] is None

    # An invalid spec is refused with the documented envelope.
    bad = await writer.patch(
        f"/v1/sources/{created.json()['id']}",
        headers={"Idempotency-Key": "chain-bad-1"},
        json={"pipeline_chain": [{"plugin_id": "translate", "kind": "no-such-kind"}]},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"]["code"] == "invalid_pipeline_chain"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_chain_endpoint_queues_and_audits(
    writer: httpx.AsyncClient,
    app: FastAPI,
    session_factory: sessionmaker[Session],
) -> None:
    """The :backfill-chain verb queues products for stored items.

    Items are seeded before the call on purpose: without them `queued` is
    always 0 and the verb's actual job -- enqueuing for pre-existing items --
    is never exercised. The audit assertion reads audit_log rather than
    trusting the response's own `audit` string, which would be the API
    vouching for itself.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "backfill-create-1"},
        json={
            "url": "https://backfill.example/feed",
            "schedule_policy": policy(),
        },
    )
    source_id = created.json()["id"]
    chain = [{"plugin_id": "translate", "kind": "translation"}]
    await writer.patch(
        f"/v1/sources/{source_id}",
        headers={"Idempotency-Key": "backfill-patch-1"},
        json={"pipeline_chain": chain, "target_lang": "en"},
    )

    # Two items that predate the chain, written straight to the database the
    # app is using (the pipeline only enqueues at store time).
    with session_factory() as session:
        for index in range(2):
            session.execute(
                sa.text(
                    "insert into item (id, source_id, guid, content_hash, title,"
                    " url, publish_status, tenant_id, created_at, updated_at)"
                    " values (:id, :s, :g, :h, :t, :u, 'ready', 'default',"
                    " '2026-01-01T00:00:00.000000Z',"
                    " '2026-01-01T00:00:00.000000Z')"
                ),
                {
                    "id": f"bf-item-{index}",
                    "s": source_id,
                    "g": f"bf-guid-{index}",
                    "h": f"bf-hash-{index}",
                    "t": f"Backfilled {index}",
                    "u": f"https://backfill.example/post/{index}",
                },
            )
        session.commit()

    first = await writer.post(
        f"/v1/sources/{source_id}:backfill-chain",
        headers={"Idempotency-Key": "backfill-run-1"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["queued"] == 2, first.json()

    # The products exist, queued for the worker.
    listed = await writer.get(f"/v1/sources/{source_id}/enrichments")
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"queued"}

    # A second run is a no-op: the unique constraint means re-queuing the same
    # (item, plugin, version, kind, lang, input) does not duplicate products.
    again = await writer.post(
        f"/v1/sources/{source_id}:backfill-chain",
        headers={"Idempotency-Key": "backfill-run-2"},
    )
    assert again.status_code == 200
    assert again.json()["queued"] == 0

    CorrelationAndAuditMiddleware.drain_audit()
    with session_factory() as session:
        audited = session.execute(
            sa.text("select count(*) from audit_log where operation like :pattern"),
            {"pattern": "POST /v1/sources/%:backfill-chain"},
        ).scalar_one()
    assert audited == 2, "each accepted backfill must leave an audit row"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrichment_visibility_and_agent_retry(
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """The agent loop: see a failed product, requeue it, see it queued.

    The retry policy stops at MAX_ATTEMPTS and waits for exactly this — the
    agent's answer to a user saying 'the provider is fixed, try again'.
    """
    from datetime import UTC, datetime

    from adapters.db_models import Item as ItemRow
    from adapters.db_models import ItemEnrichment
    from adapters.repository import timestamp

    chain = [{"plugin_id": "translate", "kind": "translation"}]
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "enrich-vis-1"},
        json={
            "url": "https://vis.example/feed",
            "schedule_policy": policy(),
            "pipeline_chain": chain,
            "target_lang": "en",
        },
    )
    source_id = created.json()["id"]

    with session_factory() as session:
        session.add(
            ItemRow(
                id="vis-item",
                source_id=source_id,
                guid="g-vis",
                content_hash="hash-vis",
                title="Visible",
                url="https://vis.example/1",
                publish_status="pending",
                tenant_id="default",
                created_at="2026-01-01T00:00:00.000000Z",
                updated_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            ItemEnrichment(
                id="enr-vis-1",
                item_id="vis-item",
                plugin_id="translate",
                plugin_version="0.1.0",
                kind="translation",
                lang="en",
                input_hash="hash-vis",
                status="failed",
                error_class="auth",
                detail="provider rejected the key",
                attempt_count=3,
                tenant_id="default",
                created_at=timestamp(datetime.now(UTC)),
                updated_at=timestamp(datetime.now(UTC)),
            )
        )
        session.commit()

    # Visibility: the agent sees the failed row and why.
    listing = await writer.get(f"/v1/sources/{source_id}/enrichments")
    assert listing.status_code == 200
    rows = listing.json()["data"]
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["error_class"] == "auth"
    assert rows[0]["attempt_count"] == 3
    # Status filter works.
    filtered = await writer.get(f"/v1/sources/{source_id}/enrichments?status=queued")
    assert filtered.json()["data"] == []

    # Retry: the failed rows become one job, selected by the endpoint.
    retry = await writer.post(
        f"/v1/sources/{source_id}:retry-enrichments",
        headers={"Idempotency-Key": "enrich-retry-1"},
    )
    assert retry.status_code == 202, retry.text
    handle = retry.json()
    assert handle["counts"]["total"] == 1, handle

    # Nothing is requeued until the work runs; then the ledger says what
    # happened per row, and the row itself is back in the queue waiting for the
    # *enrichment* worker (a different loop, which this test does not run).
    waiting = await writer.get(f"/v1/sources/{source_id}/enrichments?status=failed")
    assert [row["id"] for row in waiting.json()["data"]] == ["enr-vis-1"]

    await sweep_until_terminal(session_factory, handle["work_id"])
    units = await writer.get(f"/v1/works/{handle['work_id']}/units")
    assert [
        (unit["target"], unit["outcome"], unit["detail"])
        for unit in units.json()["data"]
    ] == [("enr-vis-1", "ok", "requeued")]

    requeued = await writer.get(f"/v1/sources/{source_id}/enrichments?status=queued")
    [row] = requeued.json()["data"]
    assert row["attempt_count"] == 0 and row["detail"] is None

    # Idempotent replay: same key answers the same body.
    replay = await writer.post(
        f"/v1/sources/{source_id}:retry-enrichments",
        headers={"Idempotency-Key": "enrich-retry-1"},
    )
    assert replay.headers.get("idempotent-replay") == "true"
    assert replay.json() == handle


# --------------------------------------------------------------------------
# Folders: one per source, default "default"
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_source_defaults_to_the_default_folder(
    writer: httpx.AsyncClient,
) -> None:
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "folder-default-1"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    assert response.status_code == 201
    assert response.json()["folder"] == "default"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_source_can_be_created_into_a_named_folder(
    writer: httpx.AsyncClient,
) -> None:
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "folder-named-1"},
        json={
            "url": FEED_URL,
            "schedule_policy": policy(),
            "folder": "  知乎  ",
        },
    )
    assert response.status_code == 201
    # Trimmed on the way in: a pasted name with stray whitespace is the
    # common mistake, and a folder named " 知乎 " would render as a
    # different sidebar entry from "知乎".
    assert response.json()["folder"] == "知乎"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "x" * 65,
        "bad\nname",
        # A slash would become a stream id no client can round-trip: the
        # folder would list under one name and answer under none.
        "a/b",
        # The stream-id encoding is not a name.
        "user/-/label/x",
    ],
)
async def test_an_unusable_folder_name_is_refused(
    writer: httpx.AsyncClient, bad: str
) -> None:
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": f"folder-bad-{abs(hash(bad))}"},
        json={"url": FEED_URL, "schedule_policy": policy(), "folder": bad},
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_moves_a_source_between_folders(writer: httpx.AsyncClient) -> None:
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "folder-move-1"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]

    moved = await writer.patch(
        f"/v1/sources/{source_id}",
        headers={"Idempotency-Key": "folder-move-2"},
        json={"folder": "阅读"},
    )
    assert moved.status_code == 200
    assert moved.json()["folder"] == "阅读"

    # Explicit null resets to the default -- the same three-state convention
    # pipeline_chain uses for clearing.
    reset = await writer.patch(
        f"/v1/sources/{source_id}",
        headers={"Idempotency-Key": "folder-move-3"},
        json={"folder": None},
    )
    assert reset.status_code == 200
    assert reset.json()["folder"] == "default"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_rejects_an_unusable_folder_name(
    writer: httpx.AsyncClient,
) -> None:
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "folder-badpatch-1"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]
    response = await writer.patch(
        f"/v1/sources/{source_id}",
        headers={"Idempotency-Key": "folder-badpatch-2"},
        json={"folder": "x" * 65},
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_busy_source_says_when_to_retry(
    with_mock_client: FastAPI,
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
) -> None:
    """409 already_running must carry Retry-After.

    The answer is "not now, try later", and without the header the caller has
    to guess the interval -- an agent client ends up either hammering the
    endpoint or backing off far more than the run needs. Every other 429/409
    the API emits already says when; this one was the exception.
    """
    from adapters.repository.jobs import (
        create_job_run,
        ensure_collect_job,
    )

    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "busy-seed"},
        json={"url": FEED_URL, "schedule_policy": {"type": "fixed", "interval": "30m"}},
    )
    source_id = created.json()["id"]

    # Hold the job's only active-run slot, as an in-flight collection would.
    with session_factory() as session:
        job_id = ensure_collect_job(
            session, source_id=source_id, tenant_id="default", at=NOW
        )
        create_job_run(
            session,
            run_id="run-holding-the-slot",
            job_id=job_id,
            correlation_id="c",
            at=NOW,
        )
        session.commit()

    response = await writer.post(
        f"/v1/sources/{source_id}:trigger",
        headers={"Idempotency-Key": "busy-1"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "already_running"
    retry_after = response.headers.get("retry-after")
    assert retry_after is not None, "409 must say when to come back"
    # A positive number of seconds: the client sleeps, then retries.
    assert int(retry_after) > 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_backfill_queues_a_watchable_batch_not_the_whole_backlog(
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chain backfill queues a bounded batch, and repeating it continues.

    Given a source with 25 stored items and a freshly enabled chain
    When :backfill-chain is called with no limit
    Then only the configured batch (3 here) is queued — one call is not a
    burst at the third-party sites those products will fetch
    And a second call queues the next batch rather than repeating the first
    And an explicit limit overrides the default.

    The bug this pins: unbounded queuing turned one call into 122 fetches of
    a single blog, and capping the *scan* instead of the queue made repeat
    calls no-ops (the first `limit` items were already queued, so the count
    stayed 0 and the tail of the backlog was unreachable).
    """
    monkeypatch.setenv("PEARSS_BACKFILL_DEFAULT_LIMIT", "3")

    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "batch-create-1"},
        json={"url": "https://batch.example/feed", "schedule_policy": policy()},
    )
    source_id = created.json()["id"]
    await writer.patch(
        f"/v1/sources/{source_id}",
        headers={"Idempotency-Key": "batch-patch-1"},
        json={
            "pipeline_chain": [{"plugin_id": "fulltext", "kind": "fulltext"}],
        },
    )

    with session_factory() as session:
        for index in range(25):
            session.execute(
                sa.text(
                    "insert into item (id, source_id, guid, content_hash, title,"
                    " url, publish_status, tenant_id, created_at, updated_at)"
                    " values (:id, :s, :g, :h, :t, :u, 'ready', 'default',"
                    " :ts, :ts)"
                ),
                {
                    "id": f"batch-item-{index:02d}",
                    "s": source_id,
                    "g": f"batch-guid-{index}",
                    "h": f"batch-hash-{index}",
                    "t": f"Backlog {index}",
                    "u": f"https://batch.example/post/{index}",
                    "ts": f"2026-01-{index + 1:02d}T00:00:00.000000Z",
                },
            )
        session.commit()

    first = await writer.post(
        f"/v1/sources/{source_id}:backfill-chain",
        headers={"Idempotency-Key": "batch-run-1"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["queued"] == 3, first.json()
    assert first.json()["limit"] == 3, "the response names the batch it used"

    second = await writer.post(
        f"/v1/sources/{source_id}:backfill-chain",
        headers={"Idempotency-Key": "batch-run-2"},
    )
    assert second.json()["queued"] == 3, (
        "repeating the call must reach the next batch, not re-queue the first"
    )

    explicit = await writer.post(
        f"/v1/sources/{source_id}:backfill-chain",
        headers={"Idempotency-Key": "batch-run-3"},
        json={"limit": 5},
    )
    assert explicit.json()["queued"] == 5, "an explicit limit wins"
    assert explicit.json()["limit"] == 5

    with session_factory() as session:
        total = session.execute(
            sa.text("select count(*) from item_enrichment")
        ).scalar()
    assert total == 11, "3 + 3 + 5 rows queued, never more than asked for"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diagnose_answers_the_question_in_one_call(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """The aggregate endpoint behind `diagnose_source` (ADR 0036, issue #73).

    Asserted as one payload rather than as four queries: the point of the
    endpoint is that a caller does not have to know which four, in which order,
    and that stopping early is not possible.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "diagnose-source-1"},
        json={"url": "https://diagnose.example/feed", "schedule_policy": policy()},
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    response = await writer.get(f"/v1/sources/{source_id}:diagnose")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["source"]["id"] == source_id
    assert body["source"]["status"] == "active"
    assert body["source"]["schedule_policy"] == policy()
    # The chain is empty for this source, which is a fact the answer states
    # rather than omits -- an absent key would be indistinguishable from "the
    # question was not asked".
    assert body["source"]["pipeline_chain"] is None
    assert body["chain"] == {
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "oldest_queued_at": None,
        # The rate belongs with the backlog (issue #193): "eleven stuck" is a
        # different answer when they are draining than when they are not.
        "rate_per_min": 0.0,
        "rate_window_minutes": 15.0,
    }
    assert body["runs"] == []
    # The two issue-#152 additions, stated rather than omitted: an absent key
    # would be indistinguishable from "the question was not asked".
    assert body["last_run"] is None
    assert body["failed_enrichments"] == []
    assert body["collection"]["loop_alive"] is True
    assert body["correlation_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diagnose_refuses_an_unknown_source(writer: httpx.AsyncClient) -> None:
    """A diagnostic that invents an answer is worse than one that says 404."""
    response = await writer.get("/v1/sources/nope:diagnose")
    assert response.status_code == 404, response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diagnose_answers_why_the_chain_is_stuck(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-153..155: the failure class, the provider message, the newest run's.

    Measured live (issue #152): `get_source` -> `diagnose_source` ->
    `list_enrichments` was three calls to learn what one call promised
    (ADR 0036), and the reason still needed reading the processor source to act
    on. The body is never part of the answer -- metadata and error text only.
    """
    from datetime import UTC, datetime

    from adapters.db_models import ItemEnrichment
    from adapters.repository import timestamp
    from adapters.repository.jobs import (
        claim_job_run,
        create_job_run,
        ensure_collect_job,
        finish_job_run,
    )
    from app.pipeline import normalise_and_store
    from core.enums import ErrorClass
    from core.job_state import JobRunEvent
    from core.models import ItemDraft

    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "diagnose-stuck-1"},
        json={"url": "https://stuck.example/feed", "schedule_policy": policy()},
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    with session_factory() as session:
        item_id = normalise_and_store(
            session,
            [
                ItemDraft(
                    source_id=source_id,
                    external_id="one",
                    title="An item",
                    canonical_url="https://stuck.example/1",
                    published_at=datetime.now(UTC),
                    content="<p>ITEM-SECRET-MARKER</p>",
                )
            ],
            tenant_id="default",
        )[0].result.item.id
        now = timestamp(datetime.now(UTC))
        for index in range(7):
            session.add(
                ItemEnrichment(
                    id=f"stuck-enr-{index}",
                    item_id=item_id,
                    plugin_id="translate",
                    plugin_version="0.1.0",
                    kind="translation",
                    lang="zh",
                    input_hash=f"hash-{index}",
                    status="failed",
                    error_class="parse",
                    detail=(
                        f"provider rejected the request: HTTP 400 (variant {index})"
                    ),
                    attempt_count=3,
                    tenant_id="default",
                    created_at=now,
                    updated_at=f"2026-01-{index + 1:02d}T00:00:00.000000Z",
                )
            )
        session.flush()
        job_id = ensure_collect_job(
            session, source_id=source_id, tenant_id="default", at=now
        )
        create_job_run(
            session, run_id="stuck-run", job_id=job_id, correlation_id="c", at=now
        )
        session.commit()
        assert claim_job_run(session, run_id="stuck-run", at=now) is True
        finish_job_run(
            session,
            run_id="stuck-run",
            event=JobRunEvent.FAIL,
            correlation_id="c",
            error_class=ErrorClass.PARSE,
            at=now,
        )
        session.commit()

    response = await writer.get(f"/v1/sources/{source_id}:diagnose")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["last_run"]["error_class"] == "parse"
    failed = body["failed_enrichments"]
    assert len(failed) == 5, "at most N=5 failed products"
    assert failed[0]["updated_at"] == "2026-01-07T00:00:00.000000Z", "newest first"
    assert failed[0]["error_class"] == "parse"
    assert "HTTP 400" in failed[0]["detail"]
    assert failed[0]["attempt_count"] == 3
    # Metadata and error text only: never the item body (AGENTS.md, ADR 0042).
    assert "ITEM-SECRET-MARKER" not in response.text


# --------------------------------------------------------------------------
# AC-198: the queue of one source, in one constant-size answer
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac198_the_queue_is_a_fold_over_the_rows(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-198: `:queue` reconciles with the per-row listing, and the rate is derived.

    The measured need: reading per-source backlogs across 82 sources cost 82
    calls, and not one of them said whether anything was moving. The counts are
    the same rows `/v1/sources/{id}/enrichments` renders, so they are reconciled
    here by counting that endpoint's own output -- a queue number that disagrees
    with the rows it summarises is worse than no number.
    """
    from collections import Counter
    from datetime import UTC, datetime

    from adapters.db_models import Item as ItemRow
    from adapters.db_models import ItemEnrichment
    from adapters.repository import RATE_WINDOW_MINUTES, timestamp

    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "queue-1"},
        json={
            "url": "https://queue.example/feed",
            "schedule_policy": policy(),
            "pipeline_chain": [{"plugin_id": "translate", "kind": "translation"}],
            "target_lang": "en",
        },
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    now = timestamp(datetime.now(UTC))
    old = timestamp(datetime(2026, 1, 1, tzinfo=UTC))
    statuses = ["queued", "queued", "running", "succeeded", "failed"]
    with session_factory() as session:
        for index, status in enumerate(statuses):
            item_id = f"queue-item-{index}"
            session.add(
                ItemRow(
                    id=item_id,
                    source_id=source_id,
                    guid=f"queue-guid-{index}",
                    content_hash=f"queue-hash-{index}",
                    title=f"Queued {index}",
                    url=f"https://queue.example/{index}",
                    publish_status="ready",
                    tenant_id="default",
                    created_at=old,
                    updated_at=old,
                )
            )
            session.add(
                ItemEnrichment(
                    id=f"queue-enr-{index}",
                    item_id=item_id,
                    plugin_id="translate",
                    plugin_version="0.1.0",
                    kind="translation",
                    lang="en",
                    input_hash=f"queue-hash-{index}",
                    status=status,
                    attempt_count=0,
                    tenant_id="default",
                    created_at=old,
                    # Only the completed row is recent, so the rate has exactly
                    # one completion to find in its window.
                    updated_at=now if status == "succeeded" else old,
                )
            )
        session.commit()

    response = await writer.get(f"/v1/sources/{source_id}:queue")

    assert response.status_code == 200, response.text
    payload = response.json()
    rows = (
        await writer.get(f"/v1/sources/{source_id}/enrichments", params={"limit": 100})
    ).json()["data"]
    counted = Counter(row["status"] for row in rows)
    assert (
        payload["queued"],
        payload["running"],
        payload["succeeded"],
        payload["failed"],
    ) == (
        counted["queued"],
        counted["running"],
        counted["succeeded"],
        counted["failed"],
    )
    # Derived from the completion stamps: one completion inside a 15-minute
    # window is 1/15 per minute. A stored counter would not follow the rows.
    assert payload["rate_per_min"] == round(1 / RATE_WINDOW_MINUTES, 3)
    assert payload["rate_window_minutes"] == RATE_WINDOW_MINUTES
    assert payload["oldest_queued_at"] == old
    # Constant size: counts, never rows. A `data` array here would make the
    # answer grow with the backlog it is supposed to summarise.
    assert set(payload) == {
        "source_id",
        "queued",
        "running",
        "succeeded",
        "failed",
        "oldest_queued_at",
        "rate_per_min",
        "rate_window_minutes",
        "correlation_id",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_queue_of_an_unknown_source_is_a_404(
    writer: httpx.AsyncClient,
) -> None:
    """A queue of zeros for an id nobody has is a lie shaped like data."""
    response = await writer.get("/v1/sources/no-such-source:queue")

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error"]["code"] == "source_not_found"


# --------------------------------------------------------------------------
# AC-201 / AC-202: the row-level retry
# --------------------------------------------------------------------------


async def _seed_failed_rows(
    writer: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
    *,
    slug: str,
    statuses: list[str],
) -> tuple[str, list[str]]:
    """A source with a chain and one row per status, seeded through the ORM.

    Constructing the rows directly is what the existing enrichment tests do:
    there is no public "create a failed product" call, and what these scenarios
    need is the *state*, which is exactly what a failed row is.
    """
    from datetime import UTC, datetime

    from adapters.db_models import Item as ItemRow
    from adapters.db_models import ItemEnrichment
    from adapters.repository import timestamp

    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": f"{slug}-source"},
        json={
            "url": f"https://{slug}.example/feed",
            "schedule_policy": policy(),
            "pipeline_chain": [{"plugin_id": "translate", "kind": "translation"}],
            "target_lang": "en",
        },
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    now = timestamp(datetime.now(UTC))
    ids: list[str] = []
    with session_factory() as session:
        for index, status in enumerate(statuses):
            item_id = f"{slug}-item-{index}"
            enrichment_id = f"{slug}-enr-{index}"
            ids.append(enrichment_id)
            session.add(
                ItemRow(
                    id=item_id,
                    source_id=source_id,
                    guid=f"{slug}-guid-{index}",
                    content_hash=f"{slug}-hash-{index}",
                    title=f"Row {index}",
                    url=f"https://{slug}.example/{index}",
                    publish_status="ready",
                    tenant_id="default",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ItemEnrichment(
                    id=enrichment_id,
                    item_id=item_id,
                    plugin_id="translate",
                    plugin_version="0.1.0",
                    kind="translation",
                    lang="en",
                    input_hash=f"{slug}-hash-{index}",
                    status=status,
                    error_class=None if status == "queued" else "auth",
                    detail=None
                    if status == "queued"
                    else f"provider said no ({index})",
                    attempt_count=0 if status == "queued" else 3,
                    tenant_id="default",
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    return source_id, ids


async def _row_state(
    session_factory: sessionmaker[Session], enrichment_id: str
) -> tuple[Any, ...]:
    """One row's retry-relevant columns, by value."""
    from sqlalchemy import select

    from adapters.db_models import ItemEnrichment

    with session_factory() as session:
        return tuple(
            session.execute(
                select(
                    ItemEnrichment.status,
                    ItemEnrichment.attempt_count,
                    ItemEnrichment.error_class,
                    ItemEnrichment.detail,
                    ItemEnrichment.next_attempt_at,
                ).where(ItemEnrichment.id == enrichment_id)
            ).one()
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac201_a_row_level_retry_touches_only_that_row(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-201: one row comes back, the others keep their state and their count.

    The measured gap: 23 permanently failed products, three worth retrying, and
    the source-scoped route was all-or-nothing. The untouched rows are asserted
    *by value* rather than as "still failed": a bulk UPDATE that forgot its
    WHERE would change them and still look like a success.
    """
    source_id, (first, second, third) = await _seed_failed_rows(
        writer,
        session_factory,
        slug="rowretry",
        statuses=["failed", "failed", "failed"],
    )

    response = await writer.post(
        f"/v1/enrichments/{second}:retry",
        headers={"Idempotency-Key": "rowretry-one"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["enrichment_id"] == second
    assert payload["status"] == "queued"
    assert payload["requeued"] == 1

    assert await _row_state(session_factory, second) == (
        "queued",
        0,
        None,
        None,
        None,
    )
    for untouched in (first, third):
        assert await _row_state(session_factory, untouched) == (
            "failed",
            3,
            "auth",
            f"provider said no ({'0' if untouched == first else '2'})",
            None,
        )
    assert source_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac202_a_retried_row_restarts_its_budget_exactly_once(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-202: retrying restarts the budget; it is not an unbounded reset.

    Retrying is the caller's decision ("the provider is fixed"), so the attempt
    budget restarts and the stale failure is cleared -- which is what makes a
    row that exhausted its attempts retryable at all. What it must not do is
    keep resetting on its own: a second retry while the row is already queued is
    a no-op (`requeued: 0`), and a row whose product exists is refused outright.
    """
    _, (row, succeeded) = await _seed_failed_rows(
        writer, session_factory, slug="rowbudget", statuses=["failed", "succeeded"]
    )

    first = await writer.post(
        f"/v1/enrichments/{row}:retry", headers={"Idempotency-Key": "rowbudget-1"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["requeued"] == 1
    assert (await _row_state(session_factory, row))[0] == "queued"

    # Already on its way: the request is "make sure this row will run", and that
    # is already true, so nothing is reset a second time.
    again = await writer.post(
        f"/v1/enrichments/{row}:retry", headers={"Idempotency-Key": "rowbudget-2"}
    )
    assert again.status_code == 200, again.text
    assert again.json() == {
        "enrichment_id": row,
        "status": "queued",
        "requeued": 0,
        "correlation_id": again.json()["correlation_id"],
    }
    assert (await _row_state(session_factory, row))[0] == "queued"

    # A product that exists is not a failure to retry: producing it again is a
    # different command.
    refused = await writer.post(
        f"/v1/enrichments/{succeeded}:retry",
        headers={"Idempotency-Key": "rowbudget-3"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["code"] == "enrichment_not_retryable"

    unknown = await writer.post(
        "/v1/enrichments/no-such-row:retry",
        headers={"Idempotency-Key": "rowbudget-4"},
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["detail"]["error"]["code"] == "enrichment_not_found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_source_scoped_retry_still_clears_every_failed_row(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """The coarser granularity still works, through the same requeue.

    It is now a loop over the row-level function rather than its own UPDATE, so
    this pins that the loop covers every failed row (and only failed ones).
    """
    source_id, (first, second, already) = await _seed_failed_rows(
        writer, session_factory, slug="rowany", statuses=["failed", "failed", "queued"]
    )

    response = await writer.post(
        f"/v1/sources/{source_id}:retry-enrichments",
        headers={"Idempotency-Key": "rowany-all"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["kind"] == "retry", response.json()
    assert response.json()["counts"]["total"] == 2, response.json()

    # A second call with a *different* key is still the same command value, so
    # it answers with the handle that is already in flight rather than queueing
    # the same rows twice.
    again = await writer.post(
        f"/v1/sources/{source_id}:retry-enrichments",
        headers={"Idempotency-Key": "rowany-all-again"},
    )
    assert again.status_code == 202, again.text
    assert again.json()["work_id"] == response.json()["work_id"]
    # The selection is the endpoint's, and the ledger is where it shows: exactly
    # the two failed rows are targets, and the one that was already queued is
    # not among them.
    await sweep_until_terminal(session_factory, response.json()["work_id"])
    units = await writer.get(f"/v1/works/{response.json()['work_id']}/units")
    assert sorted(unit["target"] for unit in units.json()["data"]) == sorted(
        [first, second]
    )
    assert already not in [unit["target"] for unit in units.json()["data"]]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrying_a_source_with_no_failures_submits_no_job(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-213: nothing to requeue is an answer, not an empty job.

    The API refuses a work with zero targets, and it should: "run nothing" is a
    mistake when a *caller* names targets. When the server selects them, an empty
    selection is ordinary -- the end state the caller asked for is already true --
    so the answer says so (200, `work_id` null, zero counts) instead of inventing
    a job nobody could see the point of.
    """
    source_id, _ = await _seed_failed_rows(
        writer, session_factory, slug="rowempty", statuses=["queued"]
    )

    response = await writer.post(
        f"/v1/sources/{source_id}:retry-enrichments",
        headers={"Idempotency-Key": "rowempty-none"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["work_id"] is None, body
    assert body["counts"] == {
        "ok": 0,
        "skipped": 0,
        "rejected": 0,
        "failed": 0,
        "queued": 0,
        "total": 0,
    }
    with session_factory() as session:
        assert session.execute(sa.text("select count(*) from work")).scalar_one() == 0
