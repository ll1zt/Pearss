"""A3 ingest endpoint tests (ADR 0022).

The endpoint exists so an external agent can push content into Pearss. These
tests pin the parts of that decision that are security-relevant, because each
one is a way the feature could be implemented wrongly and still look like it
works: content that skips sanitisation, a caller that can choose the source,
a broad token reaching a narrow endpoint (or the reverse), and a submission
that fails outright because it carries no http(s) URL.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]

API_TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"
INGEST_TOKEN = "test-ingest-token"

AGENT_SOURCE_ID = "agent-intake"
RSS_SOURCE_ID = "src-1"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    db_path = tmp_path / "ingest.db"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.begin() as connection:
        # An rss source that must NOT accept pushed items, and an agent source
        # that is the only legitimate target.
        connection.execute(
            text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, tenant_id, created_at, updated_at)"
                " values (:id, 'rss', 'https://blog.example.com/feed',"
                " 'https://blog.example.com/feed', '{}', 'active', 'default',"
                " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            ),
            {"id": RSS_SOURCE_ID},
        )
        connection.execute(
            text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, tenant_id, created_at, updated_at)"
                " values (:id, 'agent', 'agent://intake', 'agent://intake',"
                " '{}', 'active', 'default',"
                " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            ),
            {"id": AGENT_SOURCE_ID},
        )
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    monkeypatch.setenv("PEARSS_INGEST_TOKEN", INGEST_TOKEN)
    return create_app(session_factory)


def _client(app: FastAPI, token: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    )


@pytest_asyncio.fixture
async def agent_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client holding only the ingest scope."""
    async with _client(app, INGEST_TOKEN) as opened:
        yield opened


@pytest_asyncio.fixture
async def writer_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client holding the general write scope, but not ingest."""
    async with _client(app, API_TOKEN) as opened:
        yield opened


@pytest_asyncio.fixture
async def read_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(app, READ_TOKEN) as opened:
        yield opened


@pytest_asyncio.fixture
async def anonymous_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(app, None) as opened:
        yield opened


def item_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "external_id": "submitted-1",
        "title": "Pushed By An Agent",
        "canonical_url": "https://example.com/2026/09/pushed",
        "published_at": None,
        "content": "<p>Pushed body.</p>",
        "metadata": {"submitted_by": "claude-desktop"},
    }
    payload.update(overrides)
    return payload


def _path(source_id: str = AGENT_SOURCE_ID) -> str:
    return f"/v1/sources/{source_id}:ingest"


async def _post(
    client: httpx.AsyncClient,
    *items: dict[str, object],
    source_id: str = AGENT_SOURCE_ID,
    key: str = "idem-key-1",
) -> httpx.Response:
    return await client.post(
        _path(source_id),
        json={"items": list(items) or [item_payload()]},
        headers={"Idempotency-Key": key},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_token_can_push_into_an_agent_source(
    agent_client: httpx.AsyncClient,
) -> None:
    response = await _post(agent_client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["submitted"] == 1
    assert body["inserted"] == 1
    assert body["results"][0]["action"] == "insert"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pushed_content_is_sanitised_before_storage(
    agent_client: httpx.AsyncClient, writer_client: httpx.AsyncClient
) -> None:
    """A script tag in submitted HTML must not reach the stored body.

    The whole design rests on ingest going through `normalise_and_store`, so
    the sanitising call at the store boundary is not duplicated here. This is
    the test that would catch a future implementation that wrote the body
    itself.
    """
    response = await _post(
        agent_client,
        item_payload(
            content=(
                "<p>ok</p><script>alert(1)</script>"
                "<img src=x onerror=alert(2)>"
                '<a href="javascript:alert(3)">x</a>'
            )
        ),
    )
    assert response.status_code == 201, response.text
    item_id = response.json()["results"][0]["id"]

    stored = await writer_client.get(f"/v1/items/{item_id}/body")
    assert stored.status_code == 200
    body = stored.text
    assert "alert(1)" not in body
    assert "onerror" not in body
    assert "javascript:" not in body
    assert "ok" in body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_request_schema_cannot_name_a_source() -> None:
    """`source_id` must be unrepresentable in the request body, not merely ignored.

    The endpoint this replaced bound ItemDraft, whose `source_id` a caller
    filled in -- so a write-scoped caller could push into any source it liked.
    "Ignored" would be a runtime behaviour that a later refactor could drop
    (binding the dataclass again would look like a simplification); absence
    from the schema is the property that cannot be forgotten. Asserted on the
    model rather than through a request, because an extra JSON key is dropped
    by pydantic either way and would prove nothing.
    """
    from app.ingest_routes import IngestItem, IngestRequest

    assert "source_id" not in IngestItem.model_fields
    assert "source_id" not in IngestRequest.model_fields
    # The other derivations must stay out too: an agent supplies content, not
    # state or storage.
    for forbidden in ("publish_status", "content_ref", "guid"):
        assert forbidden not in IngestItem.model_fields


@pytest.mark.integration
@pytest.mark.asyncio
async def test_caller_cannot_choose_the_source(
    agent_client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """A body-level source_id changes nothing; the item lands in the path's source."""
    response = await agent_client.post(
        _path(),
        json={
            "items": [item_payload()],
            "source_id": RSS_SOURCE_ID,
        },
        headers={"Idempotency-Key": "idem-key-source"},
    )
    assert response.status_code == 201, response.text

    with session_factory() as session:
        owners = (
            session.execute(sa.text("select distinct source_id from item"))
            .scalars()
            .all()
        )
    assert owners == [AGENT_SOURCE_ID]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rss_source_refuses_pushed_items(
    agent_client: httpx.AsyncClient,
) -> None:
    """Pushing into a non-agent source would launder provenance."""
    response = await _post(agent_client, source_id=RSS_SOURCE_ID)
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "not_an_agent_source"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_source_is_404(agent_client: httpx.AsyncClient) -> None:
    response = await _post(agent_client, source_id="does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "source_not_found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_full_token_can_also_ingest(writer_client: httpx.AsyncClient) -> None:
    """The full token carries ingest as well as read/write.

    ADR 0022 requires the *ingest* credential to be isolated -- leaking it
    must not grant anything else -- and that holds because PEARSS_INGEST_TOKEN
    grants only ingest. Withholding ingest from the full token would add no
    security (it can already delete sources) while making it not full.
    """
    response = await _post(writer_client)
    assert response.status_code == 201, response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_token_cannot_use_the_general_write_surface(
    agent_client: httpx.AsyncClient,
) -> None:
    """An ingest token must not be able to delete sources.

    This is the property that makes leaking it survivable.
    """
    response = await agent_client.delete(f"/v1/sources/{AGENT_SOURCE_ID}")
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_token_cannot_ingest(read_client: httpx.AsyncClient) -> None:
    response = await _post(read_client)
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_anonymous_cannot_ingest(anonymous_client: httpx.AsyncClient) -> None:
    response = await _post(anonymous_client)
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_item_visibility_follows_the_source_chain(
    agent_client: httpx.AsyncClient, writer_client: httpx.AsyncClient
) -> None:
    """No chain means visible on submit, per the user's decision (ADR 0022)."""
    response = await _post(agent_client, key="idem-key-visible")
    assert response.status_code == 201, response.text
    item_id = response.json()["results"][0]["id"]

    fetched = await writer_client.get(f"/v1/items/{item_id}")
    assert fetched.status_code == 200
    # `ready`, not merely "not pending": a status introduced later must not be
    # able to satisfy this by accident.
    assert fetched.json()["publish_status"] == "ready"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_chained_agent_source_holds_items_behind_the_gate(
    app: FastAPI, agent_client: httpx.AsyncClient, writer_client: httpx.AsyncClient
) -> None:
    """The other half of the visibility contract, and the reason it is safe.

    "Visible on submit" is a statement about a chainless source, not a rule
    that ingest bypasses the gate. With a chain configured the submitted item
    must wait for it exactly as a collected one does -- otherwise agent
    content would be the one path that publishes unprocessed, and the
    translation/summary chain would silently not apply to it.
    """
    with app.state.session_factory() as session:
        session.execute(
            sa.text(
                "update source set pipeline_chain = :chain, target_lang = 'en'"
                " where id = :sid"
            ),
            {
                "chain": json.dumps(
                    [{"plugin_id": "translation", "kind": "translation", "lang": "en"}]
                ),
                "sid": AGENT_SOURCE_ID,
            },
        )
        session.commit()

    response = await _post(agent_client, key="idem-key-chained")
    assert response.status_code == 201, response.text
    item_id = response.json()["results"][0]["id"]

    # 404 rather than 403: the gate reports a pending item as absent, the same
    # answer every other read path gives (items_routes, ADR 0016).
    fetched = await writer_client.get(f"/v1/items/{item_id}")
    assert fetched.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_item_without_a_usable_url_is_still_accepted(
    agent_client: httpx.AsyncClient,
) -> None:
    """A note with no origin URL must not fail the whole submission.

    `canonicalize_url` rejects anything but http(s), and `normalize` calls it
    unconditionally -- so without the route's own fallback this request would
    raise rather than store.
    """
    response = await _post(
        agent_client,
        item_payload(canonical_url="", external_id="noteless-1"),
        key="idem-key-nourl",
    )
    assert response.status_code == 201, response.text
    assert response.json()["inserted"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_non_http_url_is_accepted_and_preserved(
    agent_client: httpx.AsyncClient, writer_client: httpx.AsyncClient
) -> None:
    """The user allowed non-http(s) URLs; the submitted value is not lost.

    `canonicalize_url` would reject the scheme outright, so the route keys the
    item to its source instead and keeps the original in metadata.
    """
    response = await _post(
        agent_client,
        item_payload(canonical_url="note://scratch/abc", external_id="notescheme-1"),
        key="idem-key-notescheme",
    )
    assert response.status_code == 201, response.text
    item_id = response.json()["results"][0]["id"]

    fetched = await writer_client.get(f"/v1/items/{item_id}")
    assert fetched.status_code == 200
    row = fetched.json()
    # A synthesised, non-colliding link rather than the unusable submitted
    # scheme, and rather than an empty url (which every output surface serves
    # verbatim as the item's link).
    assert row["url"].startswith("https://agent.invalid/")
    assert "notescheme-1" in row["url"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_too_many_items_in_one_request_is_refused(
    agent_client: httpx.AsyncClient,
) -> None:
    from app.ingest_routes import MAX_INGEST_ITEMS

    many = [
        item_payload(external_id=f"bulk-{index}")
        for index in range(MAX_INGEST_ITEMS + 1)
    ]
    response = await agent_client.post(
        _path(),
        json={"items": many},
        headers={"Idempotency-Key": "idem-key-bulk"},
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_item_list_is_refused(agent_client: httpx.AsyncClient) -> None:
    response = await agent_client.post(
        _path(), json={"items": []}, headers={"Idempotency-Key": "idem-key-empty"}
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resubmitting_the_same_external_id_does_not_duplicate(
    agent_client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """Idempotency holds without the client reusing its key.

    Identity is derived from (source, external_id), so a re-push after a
    dropped response is a skip rather than a second item -- an agent that
    mishandles its Idempotency-Key still cannot duplicate content.
    """
    first = await _post(agent_client, key="idem-key-dup-1")
    assert first.status_code == 201
    second = await _post(agent_client, key="idem-key-dup-2")
    assert second.status_code == 201
    assert second.json()["inserted"] == 0
    assert second.json()["results"][0]["action"] == "skip_same_source"

    with session_factory() as session:
        count = session.execute(
            sa.text("select count(*) from item where source_id = :sid"),
            {"sid": AGENT_SOURCE_ID},
        ).scalar_one()
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replaying_the_same_idempotency_key_returns_the_first_response(
    agent_client: httpx.AsyncClient,
) -> None:
    first = await _post(agent_client, key="idem-key-replay")
    second = await _post(agent_client, key="idem-key-replay")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_key_is_required(agent_client: httpx.AsyncClient) -> None:
    response = await agent_client.post(_path(), json={"items": [item_payload()]})
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "idempotency_key_required"
