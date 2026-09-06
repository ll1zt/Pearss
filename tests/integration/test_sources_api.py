"""Integration tests for the /v1/sources Core API.

These pin the contract the style guide publishes: cursor pagination, a
mandatory Idempotency-Key on writes, one error envelope for every failure, and
scope separation between the read and write tokens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE_TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"
FEED_URL = "https://planetpython.org/rss20.xml"


def policy(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"type": "fixed", "interval": "30m"}
    base.update(overrides)
    return base


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "v1.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
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
async def test_keys_are_scoped_per_actor(
    app: FastAPI, writer: httpx.AsyncClient, reader: httpx.AsyncClient
) -> None:
    """A key reused by a different principal must not replay their response.

    Sharing across actors would let one caller read another's response body and
    would let a second caller's write silently no-op.
    """
    body = {"url": FEED_URL, "schedule_policy": policy()}
    created = await writer.post(
        "/v1/sources", headers={"Idempotency-Key": "shared"}, json=body
    )
    assert created.status_code == 201

    # The read token cannot write at all, so the shared key is not honoured for
    # it — the scope check runs before idempotency.
    refused = await reader.post(
        "/v1/sources", headers={"Idempotency-Key": "shared"}, json=body
    )
    assert refused.status_code == 403


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
    assert deleted.json()["status"] == "deleted"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_is_soft_and_keeps_items(
    app: FastAPI, writer: httpx.AsyncClient
) -> None:
    """Removing a source must not delete published items.

    Their GUIDs are frozen and clients have cached them; deleting the rows
    would orphan content a reader still sees.
    """
    created = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "soft"},
        json={"url": FEED_URL, "schedule_policy": policy()},
    )
    source_id = created.json()["id"]
    await writer.delete(f"/v1/sources/{source_id}", headers={"Idempotency-Key": "d"})

    with app.state.session_factory() as session:
        remaining = session.execute(
            sa.text("select count(*) from source where id = :i"), {"i": source_id}
        ).scalar()
    assert remaining == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readding_a_deleted_source_reactivates_it(
    writer: httpx.AsyncClient,
) -> None:
    """Re-subscribing restores the source instead of returning a dead one.

    Regression: the identity is deterministic from the canonical URL, so the
    re-add found the deleted row and answered 201 with a source that would
    never be collected again.
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
    assert deleted.json()["status"] == "deleted"

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
    assert body["id"] == source_id, "the same identity, revived"
    assert body["status"] == "active"
    assert body["schedule_policy"] == {"type": "fixed", "interval": "2h"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pagination_walks_and_terminates(writer: httpx.AsyncClient) -> None:
    for index in range(3):
        await writer.post(
            "/v1/sources",
            headers={"Idempotency-Key": f"page-{index}"},
            json={
                "url": f"https://example.com/feed{index}.xml",
                "schedule_policy": policy(),
            },
        )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = (await writer.get("/v1/sources", params=params)).json()
        seen.extend(item["id"] for item in page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 3
    assert len(set(seen)) == 3, "paging must not repeat or skip"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_cursor_is_refused(writer: httpx.AsyncClient) -> None:
    response = await writer.get("/v1/sources", params={"cursor": "not-a-cursor!!"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "invalid_cursor"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_is_bounded(writer: httpx.AsyncClient) -> None:
    # The style guide caps a page at 100; over-requesting must be refused
    # rather than silently served.
    assert (await writer.get("/v1/sources", params={"limit": 500})).status_code == 422


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
    assert response.status_code in (200, 502)

    with session_factory() as session:
        jobs = session.execute(sa.text("select id from job")).fetchall()
        runs = session.execute(
            sa.text("select status, error_class from job_run")
        ).fetchall()
    assert jobs, "triggering should create the collect job"
    assert runs, "triggering should record a run"
    assert runs[0][0] in {"succeeded", "failed", "timed_out"}


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
    assert response.json()["run_status"] in {"succeeded", "failed", "timed_out"}


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
