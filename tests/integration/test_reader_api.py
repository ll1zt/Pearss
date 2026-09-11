"""Integration tests for the Reader API compatibility surface.

Covers the authentication asymmetry that makes or breaks a client login — GET
takes the token from the Authorization header, POST from the form field T —
and the state round-trip that NetNewsWire's "FreshRSS account" type performs.
"""

from __future__ import annotations

import re
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

from adapters.raw_payload import RawPayload
from app.main import create_app
from app.pipeline import normalise_and_store
from plugins.collectors.rss import collector as rss

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "src/plugins/collectors/rss/fixtures"
TENANT = "default"
# A fixture credential, never a real one. Marked synthetic in the name so
# static analysis and a reader can both tell it is not a secret.
USERNAME = "reader"
TEST_PASSWORD = "fixture-password-not-a-secret"

READ_TAG = "user/-/state/com.google/read"
STARRED_TAG = "user/-/state/com.google/starred"
READING_LIST = "user/-/state/com.google/reading-list"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """A migrated app with the Reader API credential configured."""
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "reader.db")))

    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_READER_USERNAME", USERNAME)
    monkeypatch.setenv("PEARSS_READER_PASSWORD", TEST_PASSWORD)
    # Without this the file store falls back to ./data/storage, which is where
    # a developer's real collected bodies live.
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))

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
                " values ('src-1', 'rss', 'https://blog.example.com/feed',"
                " 'https://blog.example.com/feed', '{}', 'active', 'default',"
                " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )

    factory = sessionmaker(engine, expire_on_commit=False)
    drafts = rss.parse(
        RawPayload(
            url="https://blog.example.com/feed",
            status_code=200,
            body=(FIXTURES / "valid.xml").read_bytes(),
            source_id="src-1",
        )
    )
    with Session(engine) as session:
        # normalise_and_store rather than store_item: it is the path the
        # pipeline actually uses, and the only one that writes the body to the
        # file store and attaches content_ref. Seeding with store_item left
        # every item body-less, so the Reader API could never serve one and no
        # test noticed.
        normalise_and_store(session, drafts, tenant_id=TENANT)
        session.commit()

    application = create_app(factory)
    try:
        yield application
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An async client bound straight to the ASGI app.

    ASGITransport is async-only, so a sync client cannot use it; binding to
    the app avoids occupying a port.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as opened:
        yield opened


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"GoogleLogin auth={token}"}


async def login(client: httpx.AsyncClient, *, password: str = TEST_PASSWORD) -> str:
    """Return an auth token, or "" if the login was refused."""
    response = await client.post(
        "/api/greader.php/accounts/ClientLogin",
        data={"Email": USERNAME, "Passwd": password},
    )
    if response.status_code != 200:
        return ""
    return next(
        line.removeprefix("Auth=")
        for line in response.text.splitlines()
        if line.startswith("Auth=")
    )


async def fetch_ids(
    client: httpx.AsyncClient,
    token: str,
    *,
    excluded: str | None = None,
) -> list[str]:
    """Item ids from the ids endpoint, optionally excluding a state stream."""
    query: dict[str, str] = {}
    if excluded is not None:
        query["xt"] = excluded
    response = await client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params=query,
    )
    refs: list[dict[str, str]] = response.json().get("itemRefs", [])
    return [ref["id"] for ref in refs]


async def edit(
    client: httpx.AsyncClient,
    token: str,
    ids: list[str],
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> httpx.Response:
    return await client.post(
        "/api/greader.php/reader/api/0/edit-tag",
        data={"T": token, "i": ids, "a": add or [], "r": remove or []},
    )


async def categories_for(
    client: httpx.AsyncClient, token: str, item_id: str
) -> list[str]:
    response = await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        data={"T": token, "i": [item_id]},
    )
    payload: dict[str, list[dict[str, list[str]]]] = response.json()
    return payload["items"][0]["categories"]


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.asyncio
async def test_repeated_logins_do_not_invalidate_outstanding_tokens(
    client: httpx.AsyncClient,
) -> None:
    """A ClientLogin must not kill the token a previous login minted.

    The auth token is derived from the stored scrypt hash, and scrypt salts
    are random - so the credential sync that rehashed on every login minted
    a fresh token each time, and NNW's login-then-parallel-requests flow
    answered its own requests with 401s. The sync now rehashes only when
    the password actually changed.
    """
    first = await login(client)
    assert first, "first login failed"
    second = await login(client)
    assert second, "second login failed"

    assert second == first, "each login minted a different token"

    # The first token must still authenticate after the second login.
    check = await client.get(
        "/api/greader.php/reader/api/0/tag/list?output=json",
        headers={"Authorization": f"GoogleLogin auth={first}"},
    )
    assert check.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_client_login_returns_a_token(client: httpx.AsyncClient) -> None:
    token = await login(client)
    assert len(token) == 64
    assert all(char in "0123456789abcdef" for char in token)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_client_login_json_output(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/greader.php/accounts/ClientLogin?output=json",
        data={"Email": USERNAME, "Passwd": TEST_PASSWORD},
    )
    assert response.status_code == 200
    assert set(response.json()) == {"SID", "LSID", "Auth"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_password_is_rejected(client: httpx.AsyncClient) -> None:
    assert await login(client, password="wrong") == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_failure_does_not_reveal_which_part_was_wrong(
    client: httpx.AsyncClient,
) -> None:
    bad_user = await client.post(
        "/api/greader.php/accounts/ClientLogin",
        data={"Email": "nobody", "Passwd": TEST_PASSWORD},
    )
    bad_password = await client.post(
        "/api/greader.php/accounts/ClientLogin",
        data={"Email": USERNAME, "Passwd": "wrong"},
    )
    assert bad_user.status_code == bad_password.status_code == 401
    assert bad_user.text == bad_password.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_endpoint_echoes_the_token(client: httpx.AsyncClient) -> None:
    token = await login(client)
    response = await client.get(
        "/api/greader.php/reader/api/0/token", headers=auth_header(token)
    )
    assert response.status_code == 200
    assert response.text == token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_token_is_unauthorized_with_the_expected_header(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/greader.php/reader/api/0/user-info")
    assert response.status_code == 401
    assert response.headers.get("X-Reader-Google-Bad-Token") == "true"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_does_not_accept_the_token_in_the_query_string(
    client: httpx.AsyncClient,
) -> None:
    # A query token must not authenticate a GET: clients relying on this would
    # leak tokens into server logs and referrers.
    token = await login(client)
    response = await client.get(
        "/api/greader.php/reader/api/0/user-info", params={"T": token}
    )
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_does_not_accept_the_token_in_the_header(
    client: httpx.AsyncClient,
) -> None:
    token = await login(client)
    response = await client.post(
        "/api/greader.php/reader/api/0/edit-tag",
        headers=auth_header(token),
        data={"i": ["x"], "a": [READ_TAG]},
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Read endpoints
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_info(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/greader.php/reader/api/0/user-info",
        headers=auth_header(await login(client)),
    )
    assert response.status_code == 200
    assert response.json()["userName"] == TENANT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscription_list(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/greader.php/reader/api/0/subscription/list",
        headers=auth_header(await login(client)),
    )
    subscriptions = response.json()["subscriptions"]
    assert len(subscriptions) == 1
    assert subscriptions[0]["id"] == "feed/src-1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscription_list_excludes_a_deleted_source(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """A soft-deleted source must not linger in the client's sidebar.

    The source row survives (its items are published and frozen), but the
    subscription itself is gone, so the listing has to say so.
    """
    with app.state.session_factory() as session:
        session.execute(
            sa.text("update source set status = 'deleted' where id = 'src-1'")
        )
        session.commit()

    response = await client.get(
        "/api/greader.php/reader/api/0/subscription/list",
        headers=auth_header(await login(client)),
    )
    assert response.json()["subscriptions"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tag_list_offers_the_starred_state(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/greader.php/reader/api/0/tag/list",
        headers=auth_header(await login(client)),
    )
    assert {"id": STARRED_TAG} in response.json()["tags"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stream_item_ids(client: httpx.AsyncClient) -> None:
    ids = await fetch_ids(client, await login(client))
    assert len(ids) == 3
    # Decimal, and within Swift's 64-bit Int. NetNewsWire does Int(id)!
    # on these, so anything else makes it drop every entry silently: the
    # subscription appears but no articles do.
    assert all(item_id.isdigit() for item_id in ids)
    assert all(int(item_id) < 2**63 for item_id in ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pending_items_are_invisible_to_the_reader_api(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    # Acceptance criterion 7 from docs/integrations/netnewswire.md.
    with app.state.session_factory() as session:
        session.execute(sa.text("update item set publish_status = 'pending'"))
        session.commit()

    assert await fetch_ids(client, await login(client)) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_contents_path_also_hides_pending_items(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """The gate is enforced per endpoint, not just in the ids stream.

    `stream/items/contents` applies its own pending filter, so a client that
    already knows an id cannot read a gated item's body by asking for contents
    directly. Testing only the ids endpoint would leave that branch unverified.
    """
    token = await login(client)
    ids = await fetch_ids(client, token)
    assert ids, "the fixture must have visible items to begin with"

    with app.state.session_factory() as session:
        session.execute(sa.text("update item set publish_status = 'pending'"))
        session.commit()

    response = await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        data={"T": token, "i": ids},
    )
    assert response.json()["items"] == [], (
        "contents must not leak a pending item that the ids stream hides"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stream_contents_returns_long_form_ids(
    client: httpx.AsyncClient,
) -> None:
    token = await login(client)
    ids = await fetch_ids(client, token)
    response = await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        data={"T": token, "i": ids},
    )
    items = response.json()["items"]
    assert len(items) == 3
    assert all(
        item["id"].startswith("tag:google.com,2005:reader/item/") for item in items
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stream_contents_accepts_header_auth_without_form_token(
    client: httpx.AsyncClient,
) -> None:
    """NetNewsWire authenticates this POST with the Authorization header.

    Contents is a read that happens to use POST because the item ids go in the
    body. Requiring the form field `T` -- the convention for *writes* -- made
    every real client request come back 401, so the article list filled in but
    every body was blank. The existing tests all sent `T`, which is why the
    suite stayed green while the client saw nothing.
    """
    token = await login(client)
    ids = await fetch_ids(client, token)
    response = await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        headers=auth_header(token),
        data={"i": ids},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stream_contents_carries_the_article_body(
    client: httpx.AsyncClient,
) -> None:
    """A contents response with empty bodies is indistinguishable from a
    missing body, so assert the text actually arrives."""
    token = await login(client)
    ids = await fetch_ids(client, token)
    response = await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        headers=auth_header(token),
        data={"i": ids},
    )
    items = response.json()["items"]
    assert items
    for item in items:
        body = (item.get("summary") or {}).get("content") or ""
        assert body.strip(), f"item {item.get('id')} has no body"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscription_list_shows_a_readable_name_not_the_id(
    client: httpx.AsyncClient,
) -> None:
    """The sidebar entry is the only handle a reader has on a feed.

    It used to be the source's 32-character hex id, which is not a name: the
    user could not tell one subscription from another. The recorded title wins;
    failing that a label derived from the URL is shown, and never the id while
    a URL exists.
    """
    token = await login(client)
    response = await client.get(
        "/api/greader.php/reader/api/0/subscription/list",
        headers=auth_header(token),
        params={"output": "json"},
    )
    subs = response.json()["subscriptions"]
    assert subs
    for sub in subs:
        assert sub["title"] != sub["id"].removeprefix("feed/"), (
            "subscription title is the raw source id"
        )
        assert not re.fullmatch(r"[0-9a-f]{32}", sub["title"]), (
            f"subscription shown by hex id: {sub['title']}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contents_origin_carries_a_readable_name(
    client: httpx.AsyncClient,
) -> None:
    token = await login(client)
    ids = await fetch_ids(client, token)
    response = await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        headers=auth_header(token),
        data={"i": ids},
    )
    items = response.json()["items"]
    assert items
    for item in items:
        title = (item.get("origin") or {}).get("title") or ""
        assert title and not re.fullmatch(r"[0-9a-f]{32}", title), (
            f"origin shown by hex id: {title!r}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_starred_stream_returns_only_starred_items(
    client: httpx.AsyncClient,
) -> None:
    """Nothing is starred, so the starred stream must be empty.

    It used to return the whole library: `visible_ids_for_stream` recognised
    `feed/<id>` and fell through to "everything" for every other shape, and a
    client that asks "what is starred?" then renders every answer as starred.
    That is how freshly collected feeds appeared starred in NetNewsWire while
    the database held zero starred rows.
    """
    token = await login(client)
    ids = await fetch_ids(client, token)
    assert ids, "the fixture should have items"

    starred = await client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"output": "json", "n": 1000, "s": STARRED_TAG},
    )
    assert starred.json()["itemRefs"] == []

    reading = await client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"output": "json", "n": 1000, "s": READING_LIST},
    )
    assert reading.json()["itemRefs"], "the reading list should still have items"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_folder_is_empty_rather_than_everything(
    client: httpx.AsyncClient,
) -> None:
    """A filter we do not implement must not answer with the unfiltered set.

    `All` is the one folder subscription/list advertises, so it has to resolve
    to the whole library; `Work` resolves to nothing.
    """
    token = await login(client)

    async def for_label(label: str) -> list[object]:
        response = await client.get(
            "/api/greader.php/reader/api/0/stream/items/ids",
            headers=auth_header(token),
            params={"output": "json", "n": 1000, "s": f"user/-/label/{label}"},
        )
        refs: list[object] = response.json()["itemRefs"]
        return refs

    assert await for_label("All")
    assert await for_label("Work") == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_starred_items_do_appear_in_the_starred_stream(
    client: httpx.AsyncClient,
) -> None:
    """The starred stream returns the starred items and only those.

    Asserting `refs` is non-empty would pass if the starred filter were dropped
    entirely (the whole library would come back). The set comparison is what
    pins the filter to the actual tag.
    """
    token = await login(client)
    ids = await fetch_ids(client, token)
    assert len(ids) > 1, "the fixture must have more than one item"
    target = ids[0]

    await edit(client, token, [target], add=[STARRED_TAG])

    response = await client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"output": "json", "n": 1000, "s": STARRED_TAG},
    )
    refs = [item["id"] for item in response.json()["itemRefs"]]
    assert refs == [target], "the starred stream must contain exactly the starred item"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_serving_a_body_through_the_reader_api_freezes_it(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """state-machines.md: every output adapter records first publication.

    Only a `published` item has immutable content. If the freeze were recorded
    solely by the RSS render, an item consumed through the Reader API would
    stay `ready` forever -- and the guarantee that exists because clients cache
    content would not be in force on the path where clients actually cache it.
    """
    from adapters.db_models import Item as ItemRow
    from adapters.repository import resolve_reader_ids

    token = await login(client)
    ids = await fetch_ids(client, token)

    with app.state.session_factory() as session:
        internal = list(resolve_reader_ids(session, [int(x) for x in ids]).values())
        before = {
            row.id: row.publish_status
            for row in session.query(ItemRow).filter(ItemRow.id.in_(internal)).all()
        }
    assert set(before.values()) == {"ready"}, "fixture should start unfrozen"

    await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        headers=auth_header(token),
        data={"i": ids},
    )

    with app.state.session_factory() as session:
        after = {
            row.id: row.publish_status
            for row in session.query(ItemRow).filter(ItemRow.id.in_(internal)).all()
        }
    assert set(after.values()) == {"published"}, f"Reader API did not freeze: {after}"


# --------------------------------------------------------------------------
# State writes
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mark_read_then_exclude_read(client: httpx.AsyncClient) -> None:
    token = await login(client)
    ids = await fetch_ids(client, token)

    response = await edit(client, token, [ids[0]], add=[READ_TAG])
    assert response.status_code == 200
    assert response.text == "OK"

    unread = await fetch_ids(client, token, excluded=READ_TAG)
    assert ids[0] not in unread
    assert len(unread) == len(ids) - 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_marking_read_is_idempotent(client: httpx.AsyncClient) -> None:
    token = await login(client)
    ids = await fetch_ids(client, token)

    for _ in range(3):
        await edit(client, token, [ids[0]], add=[STARRED_TAG])
        await edit(client, token, [ids[0]], add=[READ_TAG])

    categories = await categories_for(client, token, ids[0])
    # Starring survives repeated marking-as-read: the two flags are
    # independent, and neither write clears the other.
    assert STARRED_TAG in categories
    assert READ_TAG in categories


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unmark_read(client: httpx.AsyncClient) -> None:
    token = await login(client)
    ids = await fetch_ids(client, token)

    await edit(client, token, [ids[0]], add=[READ_TAG])
    await edit(client, token, [ids[0]], remove=[READ_TAG])

    assert ids[0] in await fetch_ids(client, token, excluded=READ_TAG)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contradictory_tags_are_rejected(client: httpx.AsyncClient) -> None:
    token = await login(client)
    response = await client.post(
        "/api/greader.php/reader/api/0/edit-tag",
        data={"T": token, "i": ["x"], "a": [STARRED_TAG], "r": [STARRED_TAG]},
    )
    assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mark_all_as_read(client: httpx.AsyncClient) -> None:
    token = await login(client)
    response = await client.post(
        "/api/greader.php/reader/api/0/mark-all-as-read",
        data={"T": token, "s": READING_LIST},
    )
    assert response.status_code == 200
    assert await fetch_ids(client, token, excluded=READ_TAG) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mark_all_as_read_leaves_item_content_untouched(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    # Same invariant as the edit-tag case, but for the bulk path, which takes a
    # different route through the repository and so needs its own assertion.
    token = await login(client)
    with app.state.session_factory() as session:
        before = session.execute(
            sa.text("select title, guid, publish_status, content_hash from item")
        ).fetchall()

    await client.post(
        "/api/greader.php/reader/api/0/mark-all-as-read",
        data={"T": token, "s": READING_LIST},
    )

    with app.state.session_factory() as session:
        after = session.execute(
            sa.text("select title, guid, publish_status, content_hash from item")
        ).fetchall()
    assert before == after


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_state_never_touches_item_content(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    token = await login(client)
    with app.state.session_factory() as session:
        before = session.execute(
            sa.text("select title, guid, publish_status, content_hash from item")
        ).fetchall()

    ids = await fetch_ids(client, token)
    await edit(client, token, ids, add=[READ_TAG, STARRED_TAG])

    with app.state.session_factory() as session:
        after = session.execute(
            sa.text("select title, guid, publish_status, content_hash from item")
        ).fetchall()
    assert before == after


@pytest.mark.integration
@pytest.mark.asyncio
async def test_environment_credential_is_never_stored_in_plaintext(
    app: FastAPI,
) -> None:
    with app.state.session_factory() as session:
        rows = session.execute(
            sa.text("select password_hash from reader_credential")
        ).fetchall()
    assert rows, "the credential should have been synced from the environment"
    for (stored,) in rows:
        assert TEST_PASSWORD not in stored
        assert stored.startswith("scrypt$")


# --------------------------------------------------------------------------
# Verb tolerance on the read endpoints
# --------------------------------------------------------------------------
#
# NetNewsWire POSTs these with every parameter in the query string, while the
# Google Reader convention and the reference docs describe them as GET. Serving
# only GET produced 405 Method Not Allowed on the unread and starred refreshes
# and the client showed no articles. The lesson is recorded in ADR 0006: for an
# outward contract, the client's actual traffic is the specification.


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/greader.php/reader/api/0/user-info",
        "/api/greader.php/reader/api/0/subscription/list",
        "/api/greader.php/reader/api/0/tag/list",
        "/api/greader.php/reader/api/0/token",
    ],
)
async def test_read_endpoints_accept_both_verbs(
    client: httpx.AsyncClient, path: str
) -> None:
    token = await login(client)
    header = {"Authorization": f"GoogleLogin auth={token}"}
    get = await client.get(path, headers=header)
    post = await client.post(path, headers=header)
    assert get.status_code == 200
    assert post.status_code == 200, f"{path} rejected POST"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_items_ids_accepts_post_with_query_parameters(
    client: httpx.AsyncClient,
) -> None:
    """The exact shape NetNewsWire sends."""
    token = await login(client)
    header = {"Authorization": f"GoogleLogin auth={token}"}
    params: dict[str, str | int] = {
        "n": 1000,
        "output": "json",
        "s": "user/-/state/com.google/reading-list",
        "xt": READ_TAG,
    }
    got = await client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=header,
        params=params,
    )
    posted = await client.post(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=header,
        params=params,
    )
    assert got.status_code == 200
    assert posted.status_code == 200
    assert posted.json() == got.json()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_post_may_authenticate_with_a_form_body_token(
    app: FastAPI,
) -> None:
    """Other clients send T in the body rather than the header.

    The query string is still refused for reads, because a token in a URL is
    captured by access logs and proxies.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as opened:
        token = await login(opened)
        response = await opened.post(
            "/api/greader.php/reader/api/0/user-info", data={"T": token}
        )
        assert response.status_code == 200


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unauthenticated_post_to_a_read_is_still_refused(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/greader.php/reader/api/0/user-info")
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_writes_still_require_the_form_token(
    client: httpx.AsyncClient,
) -> None:
    """The asymmetry is kept where clients depend on it.

    Relaxing reads must not quietly relax writes: a write authenticated by
    header alone would let a token land in server logs and referrers.
    """
    token = await login(client)
    ids = await fetch_ids(client, token)
    response = await client.post(
        "/api/greader.php/reader/api/0/edit-tag",
        headers=auth_header(token),
        data={"i": [ids[0]], "a": [READ_TAG]},
    )
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_translation_merges_above_the_original_in_contents(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """NNW receives translation + original, separated by a marker rule.

    The reader subscribed to a translated source: the translation leads, the
    original stays below for reference and is never replaced — products do
    not rewrite items.
    """
    from datetime import UTC, datetime

    from adapters import filestore
    from adapters.db_models import ItemEnrichment
    from adapters.repository import timestamp

    token = await login(client)
    ids = await fetch_ids(client, token)
    assert ids

    # The seed item that the ids resolve to: attach a successful product.
    with app.state.session_factory() as session:
        from adapters.repository import resolve_reader_ids

        internal = list(resolve_reader_ids(session, [int(x) for x in ids]).values())
        target_internal = internal[0] if internal else None
        ref = filestore.store_body("<p>Translated body.</p>")
        session.add(
            ItemEnrichment(
                id="enr-merge-1",
                item_id=target_internal,
                plugin_id="translate",
                plugin_version="0.1.0",
                kind="translation",
                lang="en",
                payload_ref=ref,
                input_hash="any-version",
                status="succeeded",
                tenant_id="default",
                created_at=timestamp(datetime.now(UTC)),
                updated_at=timestamp(datetime.now(UTC)),
            )
        )
        session.commit()

    response = await client.post(
        "/api/greader.php/reader/api/0/stream/items/contents",
        headers=auth_header(token),
        data={"i": ids[:1]},
    )
    body = response.json()["items"][0]["summary"]["content"]
    assert body.startswith("<p>Translated body.</p>"), body[:120]
    assert 'data-pearss="translation-separator"' in body
    # The original body is below the rule, not replaced.
    assert (
        body.rstrip().endswith("</p>")
        and len(body) > len("<p>Translated body.</p>") + 40
    )
