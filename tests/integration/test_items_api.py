"""Integration tests for the /v1/items query API.

The pagination tests are the load-bearing ones. Keyset paging exists because
offset paging breaks when rows arrive at the front of the ordering, so the
tests insert mid-paging and assert nothing is skipped or repeated.
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
FEED_URL = "https://blog.example.com/feed"


def seed(session: Session, count: int, *, source_id: str = "src-1") -> list[str]:
    """Insert `count` items with distinct publication times. Returns their ids."""
    ids: list[str] = []
    for index in range(count):
        item_id = f"item-{index:04d}"
        guid = f"guid-{index:04d}"
        session.execute(
            sa.text(
                "insert into item (id, source_id, guid, content_hash, title, url,"
                " published_at, publish_status, tenant_id, created_at, updated_at,"
                " content_ref)"
                " values (:id, :s, :g, :h, :t, :u, :p, 'ready', 'default',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z', :ref)"
            ),
            {
                "id": item_id,
                "s": source_id,
                "g": guid,
                "h": f"hash-{index:04d}",
                "t": f"Article number {index}",
                "u": f"https://blog.example.com/post/{index}",
                # Fixed-width so lexicographic order matches chronological.
                "p": f"2026-01-{index + 1:02d}T00:00:00.000000Z",
                "ref": f"cas://{'a' * 64}" if index % 2 == 0 else None,
            },
        )
        ids.append(item_id)
    session.commit()
    return ids


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "q.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, tenant_id, created_at, updated_at)"
                " values ('src-1', 'rss', :u, :u, '{}', 'active', 'default',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
            ),
            {"u": FEED_URL},
        )
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
    monkeypatch.delenv("PEARSS_API_READ_TOKEN", raising=False)
    # The storage root is resolved from the environment at call time, so the
    # test has to point it at the same place store_body writes. Left unset, the
    # route would read ./data/storage and every body lookup would 404 — a
    # silent mismatch between where a reference points and where it resolves.
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    return create_app(session_factory)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    with session_factory() as session:
        seed(session, 12)
    yield session_factory


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_returns_the_documented_envelope(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    body = (await client.get("/v1/items")).json()
    assert set(body) == {"data", "next_cursor", "correlation_id"}
    assert body["correlation_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_fields_are_a_summary_not_everything(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    first = (await client.get("/v1/items")).json()["data"][0]
    assert set(first) == {
        "id",
        "guid",
        "title",
        "url",
        "published_at",
        "publish_status",
    }
    # The storage reference and hash are internal plumbing; not default output.
    assert "content_hash" not in first
    assert "content_ref" not in first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_body_is_never_inlined_at_any_fields_setting(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    # Asking for every allowed field must still not return HTML bodies: the
    # style guide forbids it and an agent's context is the thing being protected.
    response = await client.get("/v1/items", params={"fields": "id,title,content_ref"})
    row = response.json()["data"][0]
    assert "body" not in row and "content" not in row
    # content_ref is a reference, not the content itself.
    assert row["content_ref"] is None or row["content_ref"].startswith("cas://")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_field_is_refused_with_the_offered_list(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    response = await client.get("/v1/items", params={"fields": "id,body"})
    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "unknown_fields"
    assert "body" in error["suggestion"] or "Choose from" in error["suggestion"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sort_is_whitelisted(client: httpx.AsyncClient) -> None:
    # An arbitrary column name would either fail at the database or, if
    # interpolated, become an injection point.
    assert (await client.get("/v1/items", params={"sort": "title"})).status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_openapi_declares_the_sort_enum(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    # The style guide requires the sort whitelist to be in the spec, and the
    # MCP surface is generated from it.
    spec = (await client.get("/openapi.json")).json()
    params = spec["paths"]["/v1/items"]["get"]["parameters"]
    schema = next(p["schema"] for p in params if p["name"] == "sort")
    # FastAPI emits a named component with a $ref rather than an inline enum,
    # so the whitelist has to be resolved through components.
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    assert set(schema["enum"]) == {"-published_at", "published_at"}


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_items_are_invisible(
    app: FastAPI, client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    with app.state.session_factory() as session:
        session.execute(
            sa.text(
                "update item set publish_status = 'pending'"
                " where id in (select id from item limit 5)"
            )
        )
        session.commit()

    body = (await client.get("/v1/items", params={"limit": 100})).json()
    assert all(i["publish_status"] != "pending" for i in body["data"])
    assert len(body["data"]) == 7


@pytest.mark.integration
@pytest.mark.asyncio
async def test_asking_for_pending_explicitly_still_returns_none(
    app: FastAPI, client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    # The filter can narrow within what the gate allows; it cannot widen it.
    with app.state.session_factory() as session:
        session.execute(sa.text("update item set publish_status='pending'"))
        session.commit()

    body = (await client.get("/v1/items", params={"publish_status": "pending"})).json()
    assert body["data"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_item_details_are_404_not_403(
    app: FastAPI, client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    # Saying "this exists but you may not see it" would leak the gate's
    # decisions to a caller with no business knowing them.
    with app.state.session_factory() as session:
        session.execute(
            sa.text("update item set publish_status='pending' where id='item-0000'")
        )
        session.commit()

    assert (await client.get("/v1/items/item-0000")).status_code == 404


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_paging_walks_every_item_exactly_once(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(20):
        params: dict[str, str | int] = {"limit": 5}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/v1/items", params=params)).json()
        seen.extend(row["id"] for row in page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 12
    assert len(set(seen)) == 12, "paging must neither skip nor repeat"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_newest_first_order_is_preserved_across_pages(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    first = (await client.get("/v1/items", params={"limit": 3})).json()
    second = (
        await client.get(
            "/v1/items", params={"limit": 3, "cursor": first["next_cursor"]}
        )
    ).json()

    dates = [row["published_at"] for row in first["data"] + second["data"]]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inserting_mid_paging_does_not_shift_the_window(
    app: FastAPI, client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """The reason keyset paging exists.

    With an offset cursor, a row inserted ahead of the current position pushes
    everything down and the next page silently skips a row. Newest-first
    ordering means that happens on every collection run.
    """
    first = (await client.get("/v1/items", params={"limit": 5})).json()
    seen = [row["id"] for row in first["data"]]

    # A new item arrives at the front, between pages.
    with app.state.session_factory() as session:
        session.execute(
            sa.text(
                "insert into item (id, source_id, guid, content_hash, title, url,"
                " published_at, publish_status, tenant_id, created_at, updated_at)"
                " values ('late', 'src-1', 'g-late', 'h-late', 'Late arrival',"
                " 'https://blog.example.com/late',"
                " '2026-06-01T00:00:00.000000Z', 'ready', 'default',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
            )
        )
        session.commit()

    cursor = first["next_cursor"]
    collected = list(seen)
    for _ in range(10):
        page = (
            await client.get("/v1/items", params={"limit": 5, "cursor": cursor})
        ).json()
        collected.extend(row["id"] for row in page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    # Every original row still appears, exactly once. This is the property
    # offset paging cannot give: the inserted row sits ahead of the cursor, so
    # an offset would slide the window and silently drop one row.
    assert len(collected) == len(set(collected)), "a row was repeated"
    assert set(seen) <= set(collected), "a row was skipped"
    assert len(collected) == 12

    # Keyset does not walk backwards. A row newer than the cursor is not
    # revisited mid-walk — it appears on the next fresh query instead. That is
    # the definition of the scheme, not a defect: consistency is bought by
    # excluding rows that sort before the position already read.
    assert "late" not in collected
    fresh = (await client.get("/v1/items", params={"limit": 3})).json()
    assert fresh["data"][0]["id"] == "late"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ascending_sort_pages_too(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(20):
        params: dict[str, str | int] = {"limit": 5, "sort": "published_at"}
        if cursor:
            params["cursor"] = cursor
        page = (await client.get("/v1/items", params=params)).json()
        seen.extend(row["id"] for row in page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 12
    assert len(set(seen)) == 12


@pytest.mark.integration
@pytest.mark.asyncio
async def test_malformed_cursor_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/items", params={"cursor": "garbage"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "invalid_cursor"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_is_capped(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/items", params={"limit": 500})).status_code == 422


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_filter(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    body = (await client.get("/v1/items", params={"source_id": "src-1"})).json()
    assert len(body["data"]) == 12
    empty = (await client.get("/v1/items", params={"source_id": "nope"})).json()
    assert empty["data"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_title_search_matches_substring(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    body = (await client.get("/v1/items", params={"q": "number 7"})).json()
    assert [row["title"] for row in body["data"]] == ["Article number 7"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_like_wildcards_in_a_search_term_are_literal(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    # Without escaping, `%` would match every row and a term like `100%`
    # would silently become a pattern.
    assert (await client.get("/v1/items", params={"q": "%"})).json()["data"] == []
    assert (await client.get("/v1/items", params={"q": "_"})).json()["data"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_date_bounds(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    body = (
        await client.get(
            "/v1/items",
            params={"since": "2026-01-10T00:00:00.000000Z", "limit": 100},
        )
    ).json()
    assert all(row["published_at"] >= "2026-01-10" for row in body["data"])
    assert len(body["data"]) == 3


# --------------------------------------------------------------------------
# Single item and body
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_item_returns_all_fields(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    row = (await client.get("/v1/items/item-0000")).json()
    assert row["id"] == "item-0000"
    assert "content_hash" in row and "created_at" in row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_item_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/items/nothing-here")
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "item_not_found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_body_endpoint_serves_stored_content(
    app: FastAPI,
    client: httpx.AsyncClient,
    store: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    from adapters.filestore import store_body
    from adapters.repository import get_item, update_item_content_ref

    digest = store_body("<p>Real body text.</p>")
    with app.state.session_factory() as session:
        update_item_content_ref(session, item_id="item-0000", content_ref=digest)
        session.commit()
        stored = get_item(session, "item-0000")
        assert stored is not None

    response = await client.get("/v1/items/item-0000/body")
    assert response.status_code == 200
    assert response.text == "<p>Real body text.</p>"
    assert "text/html" in response.headers["content-type"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_body_of_item_without_a_reference_is_404(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    # item-0001 was seeded with no content_ref.
    response = await client.get("/v1/items/item-0001/body")
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "body_not_stored"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_items_require_a_token(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        assert (await anonymous.get("/v1/items")).status_code == 401
