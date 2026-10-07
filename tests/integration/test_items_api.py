"""Integration tests for the /v1/items query API.

The pagination tests are the load-bearing ones. Keyset paging exists because
offset paging breaks when rows arrive at the front of the ordering, so the
tests insert mid-paging and assert nothing is skipped or repeated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item
from app.main import create_app, create_engine_for
from app.pipeline import normalise_and_store
from tests.migrated_schema import migrated_database
from tests.source_seed import seed_source

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE_TOKEN = "test-write-token"
FEED_URL = "https://blog.example.com/feed"


def open_gate(session: Session, *, item_ids: list[str], origin_id: str) -> None:
    """Record readiness for seeded items, the way the pipeline does.

    ADR 0061: visibility is `(item, subscription)`, so a raw INSERT that sets
    `publish_status='ready'` leaves the item invisible -- that column is the
    content's state, not the gate.
    """
    for item_id in item_ids:
        session.execute(
            sa.text(
                "insert into item_subscription_ready"
                " (item_id, subscription_id, ready_at)"
                " select :id, s.id, :at from subscription s where s.origin_id = :o"
            ),
            {"id": item_id, "at": "2026-01-01T00:00:00.000000Z", "o": origin_id},
        )
    session.commit()


def seed(session: Session, count: int, *, source_id: str = "src-1") -> list[str]:
    """Insert `count` items with distinct publication times. Returns their ids."""
    ids: list[str] = []
    for index in range(count):
        item_id = f"item-{index:04d}"
        guid = f"guid-{index:04d}"
        session.execute(
            sa.text(
                "insert into item (id, origin_id, guid, content_hash,"
                " title, url,"
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
    open_gate(session, item_ids=ids, origin_id=source_id)
    return ids


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "q.db")

    engine = create_engine_for(url)
    with engine.begin() as conn:
        seed_source(
            conn,
            source_id="src-1",
            url=FEED_URL,
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
    # `total` is the filtered row count, not the page length: it is what lets a
    # caller size a destructive action ("clear this source") without paging to
    # the end to find out.
    assert set(body) == {"data", "next_cursor", "total", "correlation_id"}
    assert body["correlation_id"]
    assert body["total"] == len(body["data"]), "one page, so the totals agree"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_total_counts_the_filter_not_the_page(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """A limited page must still report how much the filter selects.

    Reporting the page length instead would make the number useless for the
    decision it exists to inform, and would agree with the data only by
    accident on a single-page response.
    """
    body = (await client.get("/v1/items", params={"limit": 3})).json()
    assert len(body["data"]) == 3
    assert body["total"] > 3


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
    # `relevance` and `hybrid` are whitelisted but only meaningful with
    # `full_text`: the route refuses each without one rather than quietly ranking
    # nothing or quietly answering with one path, so the whitelist and the runtime
    # conditions are different statements and both belong here.
    assert set(schema["enum"]) == {
        "-published_at",
        "published_at",
        "relevance",
        "hybrid",
    }


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
        # ADR 0061: the gate is the readiness rows, so closing it means
        # removing them -- flipping the status alone hides nothing.
        session.execute(
            sa.text(
                "delete from item_subscription_ready"
                " where item_id in (select id from item limit 5)"
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
        session.execute(sa.text("delete from item_subscription_ready"))
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
        session.execute(
            sa.text("delete from item_subscription_ready where item_id='item-0000'")
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
                "insert into item (id, origin_id, guid, content_hash,"
                " title, url,"
                " published_at, publish_status, tenant_id, created_at, updated_at)"
                " values ('late', 'src-1', 'g-late', 'h-late', 'Late arrival',"
                " 'https://blog.example.com/late',"
                " '2026-06-01T00:00:00.000000Z', 'ready', 'default',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
            )
        )
        open_gate(session, item_ids=["late"], origin_id="src-1")
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_until_without_a_fractional_part_includes_the_instant_it_names(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """Regression: bounds are re-rendered to the stored width before comparing.

    The items in this store are published exactly at midnight. Compared as
    raw text, `until=2026-01-03T00:00:00Z` sorts after the stored
    `...T00:00:00.000000Z` and excluded the very instant it named.
    """
    body = (
        await client.get("/v1/items", params={"until": "2026-01-03T00:00:00Z"})
    ).json()
    assert len(body["data"]) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_date_bounds_accept_an_explicit_offset(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    # 2026-01-04T08:00:00+08:00 is 2026-01-04T00:00:00Z, so items 04 through 12.
    body = (
        await client.get(
            "/v1/items", params={"since": "2026-01-04T08:00:00+08:00", "limit": 100}
        )
    ).json()
    assert len(body["data"]) == 9


@pytest.mark.integration
@pytest.mark.asyncio
async def test_date_bounds_that_cannot_parse_are_refused(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    garbage = await client.get("/v1/items", params={"until": "not-a-date"})
    assert garbage.status_code == 400
    assert garbage.json()["detail"]["error"]["code"] == "invalid_until"

    naive = await client.get("/v1/items", params={"since": "2026-01-03T00:00:00"})
    assert naive.status_code == 400
    assert naive.json()["detail"]["error"]["code"] == "invalid_since"


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
        # Seeded as 'ready'; serving the body is what promotes it.
        assert stored.publish_status == "ready"

    response = await client.get("/v1/items/item-0000/body")
    assert response.status_code == 200
    assert response.text == "<p>Real body text.</p>"
    assert "text/html" in response.headers["content-type"]

    # A successful body read freezes the item: the body is now the artifact a
    # client can cache, so later collection must not overwrite it. Losing this
    # silently would let a re-fetch change content under a cached URL.
    with app.state.session_factory() as session:
        after = get_item(session, "item-0000")
        assert after is not None
        assert after.publish_status == "published"


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


# --------------------------------------------------------------------------
# Deletion (ADR 0021)
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_item_removes_it_from_every_read_path(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """`store` seeds twelve items; this deletes one and checks it is gone."""
    before = await client.get("/v1/items", params={"limit": 100})
    target = before.json()["data"][1]["id"]

    response = await client.delete(
        f"/v1/items/{target}", headers={"Idempotency-Key": "del-1"}
    )
    assert response.status_code == 200
    assert response.json()["items"] == 1

    assert (await client.get(f"/v1/items/{target}")).status_code == 404
    after = await client.get("/v1/items", params={"limit": 100})
    remaining = [row["id"] for row in after.json()["data"]]
    assert target not in remaining
    assert len(remaining) == len(before.json()["data"]) - 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_items_is_a_batch(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    before = await client.get("/v1/items", params={"limit": 100})
    ids = [row["id"] for row in before.json()["data"]]
    victims = [ids[0], ids[2]]

    response = await client.post(
        "/v1/items:delete",
        headers={"Idempotency-Key": "del-batch"},
        json={"item_ids": victims},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 2
    assert body["items"] == 2

    after = await client.get("/v1/items", params={"limit": 100})
    remaining = [row["id"] for row in after.json()["data"]]
    assert all(v not in remaining for v in victims)
    assert len(remaining) == len(ids) - 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deleting_an_unknown_item_is_not_a_404(
    client: httpx.AsyncClient,
) -> None:
    """Idempotence: the retry must report the same success as the first call."""
    first = await client.delete(
        "/v1/items/never-existed", headers={"Idempotency-Key": "del-x1"}
    )
    second = await client.delete(
        "/v1/items/never-existed", headers={"Idempotency-Key": "del-x2"}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["items"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replayed_delete_returns_the_first_result(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """The same Idempotency-Key must not delete twice, and must replay."""
    listed = await client.get("/v1/items", params={"limit": 100})
    target = listed.json()["data"][0]["id"]

    first = await client.delete(
        f"/v1/items/{target}", headers={"Idempotency-Key": "same-key"}
    )
    second = await client.delete(
        f"/v1/items/{target}", headers={"Idempotency-Key": "same-key"}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json(), "a replay, not a second deletion"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_requires_a_write_token(app: FastAPI) -> None:
    """Deletion is a write, so the read token must not reach it."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        response = await anonymous.delete(
            "/v1/items/item-0000", headers={"Idempotency-Key": "anon"}
        )
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_requires_an_idempotency_key(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/v1/items/item-0000")
    assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_id_list_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """An empty batch is a caller mistake, not a silent no-op.

    Answering 200 to "delete nothing" hides a bug that produced an empty list
    where a populated one was meant.
    """
    response = await client.post(
        "/v1/items:delete",
        headers={"Idempotency-Key": "empty-batch"},
        json={"item_ids": []},
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_items_chain_rows_are_asked_for_directly(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """AC-64 (issue #72): "why is this item held" in one call.

    The item is *pending*, which is the case the route exists for -- and the
    only read that answers for a pending item, because the gate hides it
    everywhere else. Answering 404 here would make the diagnostic useless for
    the situation that needs it.
    """
    with store() as session:
        session.execute(
            sa.text(
                "insert into item_enrichment (id, item_id, plugin_id,"
                " plugin_version, kind, lang, status, attempt_count, tenant_id,"
                " input_hash, created_at, updated_at)"
                " values ('enr-1', 'item-0000', 'translate', '1', 'translation',"
                " 'en', 'failed', 3, 'default', 'hash', '2026-01-01T00:00:00.000000Z',"
                " '2026-01-01T00:00:00.000000Z')"
            )
        )
        session.execute(
            sa.text("update item set publish_status = 'pending' where id = 'item-0000'")
        )
        session.execute(
            sa.text("delete from item_subscription_ready where item_id = 'item-0000'")
        )
        session.commit()

    response = await client.get("/v1/items/item-0000/enrichments")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["id"] for row in body["data"]] == ["enr-1"]
    assert body["data"][0]["status"] == "failed"
    assert body["data"][0]["attempt_count"] == 3
    assert body["total"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_items_chain_rows_are_a_404(client: httpx.AsyncClient) -> None:
    """A diagnostic that invents an empty answer is worse than one that says 404."""
    response = await client.get("/v1/items/nope/enrichments")
    assert response.status_code == 404, response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_source_scoped_listing_is_unchanged(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """AC-65: the existing shape keeps working, and both routes agree."""
    with store() as session:
        session.execute(
            sa.text(
                "insert into item_enrichment (id, item_id, plugin_id,"
                " plugin_version, kind, lang, status, attempt_count, tenant_id,"
                " input_hash, created_at, updated_at)"
                " values ('enr-2', 'item-0000', 'translate', '1', 'translation',"
                " 'en', 'queued', 0, 'default', 'hash2',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
            )
        )
        session.commit()

    by_source = await client.get("/v1/sources/src-1/enrichments")
    by_item = await client.get("/v1/items/item-0000/enrichments")
    assert by_source.status_code == 200, by_source.text
    assert by_item.status_code == 200, by_item.text
    assert [row["id"] for row in by_source.json()["data"]] == ["enr-2"]
    # The same rows, and the same shape: the two routes differ only in which
    # rows they select.
    assert by_item.json()["data"] == by_source.json()["data"]


# --------------------------------------------------------------------------
# AC-108..109 from the entry point
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_related_items_of_an_unknown_item_is_a_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/items/no-such-item/related")

    assert response.status_code == 404, response.text
    assert "item_not_found" in response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_related_items_without_a_vector_says_so(
    client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """409 rather than an empty list: "no product" is not "no neighbours"."""
    with session_factory() as session:
        (item_id,) = seed(session, 1)

    response = await client.get(f"/v1/items/{item_id}/related")

    assert response.status_code == 409, response.text
    assert "item_has_no_embedding" in response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pending_item_is_absent_from_the_neighbour_endpoints(
    client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """The gate holds on these reads too, and reports absent rather than forbidden."""
    with session_factory() as session:
        (item_id,) = seed(session, 1)
        session.execute(
            sa.update(Item).where(Item.id == item_id).values(publish_status="pending")
        )
        session.commit()

    for path in ("related", "duplicates"):
        response = await client.get(f"/v1/items/{item_id}/{path}")
        assert response.status_code == 404, response.text
        assert "item_not_found" in response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicates_are_listed_and_nothing_is_changed(
    client: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-109 from the entry point, including the half that matters: both survive."""
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(_EMBED_FIXTURES))
    from plugins.processors.embed.encoder import Encoder

    encoder = Encoder(_EMBED_FIXTURES)
    with session_factory() as session:
        original, copy = _seed_pair_with_vectors(session, encoder)

    response = await client.get(f"/v1/items/{original}/duplicates")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["data"]] == [copy]

    with session_factory() as session:
        survivors = set(session.scalars(sa.select(Item.id)).all())
    assert {original, copy} <= survivors, "a hint must not have deleted anything"


_EMBED_FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "plugins"
    / "processors"
    / "embed"
    / "fixtures"
)


def _seed_pair_with_vectors(session: Session, encoder: Any) -> tuple[str, str]:
    """An item and a near copy of it, each with the vector the worker would store."""
    import base64
    import json as _json
    import struct

    from adapters.db_models import ItemEnrichment
    from adapters.filestore import store_body
    from adapters.repository import create_source
    from adapters.repository.enrichment import chain_for_source
    from app.enrichment_worker import _open_gate_if_complete
    from core.models import ItemDraft

    source = create_source(
        session,
        source_type="rss",
        url="https://example.test/neighbours",
        canonical_url="https://example.test/neighbours",
        schedule_policy="{}",
        pipeline_chain=_json.dumps([{"plugin_id": "embed", "kind": "embedding"}]),
        tenant_id="default",
        at="2026-01-01T00:00:00.000000Z",
    )
    session.commit()
    made: list[str] = []
    for external_id, text in (
        ("original", "Linux 内核的调度器是怎么工作的"),
        ("copy", "Linux 内核的调度器如何工作"),
    ):
        item = normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source.id,
                    external_id=external_id,
                    title=text,
                    canonical_url=f"https://example.test/{external_id}",
                    published_at=None,
                    content=text,
                )
            ],
            chain=chain_for_source(session, source_id=source.id),
        )[0].result.item
        session.commit()
        vector = encoder.encode(text)
        session.execute(
            sa.update(ItemEnrichment)
            .where(ItemEnrichment.item_id == item.id)
            .values(
                status="succeeded",
                payload_ref=store_body(
                    base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode(
                        "ascii"
                    )
                ),
            )
        )
        session.commit()
        _open_gate_if_complete(session, item_id=item.id, content_hash=item.content_hash)
        session.commit()
        made.append(item.id)
    return made[0], made[1]


# --------------------------------------------------------------------------
# AC-197: an item by its address
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac197_an_item_is_found_by_its_address(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """AC-197: `url` answers "is this address stored?", exactly.

    The measured need: after a 568-address backfill there was no way to ask
    whether one address had landed. `q` searches titles and `full_text` searches
    bodies; neither is an identity question, and paging the whole list to look
    for a url costs a request per page.
    """
    response = await client.get(
        "/v1/items", params={"url": "https://blog.example.com/post/3"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["data"]] == ["item-0003"]
    assert body["total"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac197_a_shared_link_finds_the_stored_item(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """AC-197: the address people actually paste, not the stored form.

    A link copied out of a newsletter carries campaign parameters and often a
    fragment. The lookup normalises the way identity does, so the answer is
    about the resource rather than about the string -- and a parameter that is
    *not* a known tracker is preserved, because for many sites it is part of the
    address (`?page=2` is a different page, not tracking noise).
    """
    tracked = await client.get(
        "/v1/items",
        params={
            "url": "https://blog.example.com/post/3?utm_source=news&utm_medium=x#top"
        },
    )
    assert [item["id"] for item in tracked.json()["data"]] == ["item-0003"]

    meaningful = await client.get(
        "/v1/items", params={"url": "https://blog.example.com/post/3?page=2"}
    )
    assert meaningful.json()["data"] == [], (
        "an unknown query parameter identifies a different resource, so this "
        "must not match the row that was stored without it"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac197_a_miss_is_an_empty_page_and_a_bad_url_is_a_refusal(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """AC-197: "not stored" and "not an address" are different answers.

    An address nobody stored is ordinary information (200, empty). Something
    that cannot be an address at all is a refusal, because answering "no such
    item" would be true and useless.
    """
    missing = await client.get(
        "/v1/items", params={"url": "https://blog.example.com/post/9999"}
    )
    assert missing.status_code == 200, missing.text
    assert missing.json()["data"] == []
    assert missing.json()["total"] == 0

    refused = await client.get("/v1/items", params={"url": "mailto:hi@example.com"})
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"]["code"] == "invalid_url"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac197_the_gate_applies_to_the_address_lookup_too(
    app: FastAPI, client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """AC-197: an unpublished item is not discoverable by address either.

    The gate lives in the shared condition list, which is why this filter cannot
    be an exception to it: "find the item whose address is X" must not be the way
    around "pending rows are invisible".
    """
    with app.state.session_factory() as session:
        session.execute(
            sa.text("update item set publish_status = 'pending' where id = 'item-0003'")
        )
        session.execute(
            sa.text("delete from item_subscription_ready where item_id = 'item-0003'")
        )
        session.commit()

    response = await client.get(
        "/v1/items", params={"url": "https://blog.example.com/post/3"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


# --------------------------------------------------------------------------
# AC-203 / AC-204: consumption state as a first-class atom
# --------------------------------------------------------------------------


def _row_facts(session: sessionmaker[Session], item_id: str) -> tuple[Any, ...]:
    """The item's own columns: the ones a read-state write must not touch."""
    with session() as db:
        return tuple(
            db.execute(
                sa.text(
                    "select guid, content_hash, publish_status, content_ref,"
                    " updated_at from item where id = :id"
                ),
                {"id": item_id},
            ).one()
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac203_read_state_round_trips_and_absent_means_false(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """AC-203: write, read back, and no row reads as unread/unstarred.

    The gap was vocabulary rather than storage: the table has existed since P1,
    but the only way to write it was Google Reader's dialect, so an agent could
    not mark an item read at all (issue #196). An item nobody has touched
    answers with `false`/`false` and `updated_at: null` -- the absence of a row
    is a state, and saying so is what keeps a caller from writing one just to
    find out.
    """
    before = await client.get("/v1/items/item-0003/read-state")

    assert before.status_code == 200, before.text
    assert before.json()["is_read"] is False
    assert before.json()["is_starred"] is False
    assert before.json()["updated_at"] is None

    written = await client.patch(
        "/v1/items/item-0003/read-state",
        headers={"Idempotency-Key": "read-state-1"},
        json={"is_read": True},
    )
    assert written.status_code == 200, written.text
    assert written.json()["is_read"] is True
    assert written.json()["is_starred"] is False
    assert written.json()["updated_at"] is not None

    read_back = await client.get("/v1/items/item-0003/read-state")
    assert read_back.json()["is_read"] is True

    # Omitted means unchanged: this is what lets a client star an item without
    # claiming to know whether it has been read.
    starred = await client.patch(
        "/v1/items/item-0003/read-state",
        headers={"Idempotency-Key": "read-state-2"},
        json={"is_starred": True},
    )
    assert starred.json() == {
        "item_id": "item-0003",
        "is_read": True,
        "is_starred": True,
        "updated_at": starred.json()["updated_at"],
        "correlation_id": starred.json()["correlation_id"],
    }

    # Repeating the write is one state, and the same key is the same response.
    replay = await client.patch(
        "/v1/items/item-0003/read-state",
        headers={"Idempotency-Key": "read-state-2"},
        json={"is_starred": True},
    )
    assert replay.headers.get("idempotent-replay") == "true"
    assert replay.json() == starred.json()
    assert (await client.get("/v1/items/item-0003/read-state")).json()[
        "is_read"
    ] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac204_writing_read_state_does_not_touch_the_item(
    client: httpx.AsyncClient,
    store: sessionmaker[Session],
    session_factory: sessionmaker[Session],
) -> None:
    """AC-204: the item's own columns are pinned by literal value.

    "Mark it read" must not be a write to the item: a published item's content
    is frozen (clients cache it), so a read-state write that reached `guid`,
    `content_hash`, `publish_status` or `content_ref` would either break a
    client's cache or corrupt identity. Literal values, because a relative
    assertion would pass if the columns stopped being selected at all.
    """
    before = _row_facts(session_factory, "item-0003")
    assert before == (
        "guid-0003",
        "hash-0003",
        "ready",
        None,
        "2026-01-01T00:00:00.000000Z",
    )

    written = await client.patch(
        "/v1/items/item-0003/read-state",
        headers={"Idempotency-Key": "read-state-facts"},
        json={"is_read": True, "is_starred": True},
    )

    assert written.status_code == 200, written.text
    assert _row_facts(session_factory, "item-0003") == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_state_refuses_an_unknown_item_and_an_empty_patch(
    client: httpx.AsyncClient, store: sessionmaker[Session]
) -> None:
    """Two refusals: no such item, and a patch that changes nothing.

    An empty patch is refused for the same reason the source PATCH refuses one:
    it looks like a write and is not, so answering 200 would tell a caller
    something happened.
    """
    unknown = await client.get("/v1/items/no-such-item/read-state")
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["detail"]["error"]["code"] == "item_not_found"

    empty = await client.patch(
        "/v1/items/item-0003/read-state",
        headers={"Idempotency-Key": "read-state-empty"},
        json={},
    )
    assert empty.status_code == 400, empty.text
    assert empty.json()["detail"]["error"]["code"] == "empty_patch"

    write_unknown = await client.patch(
        "/v1/items/no-such-item/read-state",
        headers={"Idempotency-Key": "read-state-unknown"},
        json={"is_read": True},
    )
    assert write_unknown.status_code == 404, write_unknown.text
