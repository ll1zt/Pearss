"""Integration tests for the Reader API compatibility surface.

Covers the authentication asymmetry that makes or breaks a client login — GET
takes the token from the Authorization header, POST from the form field T —
and the state round-trip that NetNewsWire's "FreshRSS account" type performs.
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

from adapters.raw_payload import RawPayload
from adapters.repository import store_item
from app.main import create_app
from core.normalize import normalize
from plugins.collectors import rss

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

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "insert into source (id, type, schedule_policy, status, tenant_id,"
                " created_at, updated_at) values ('src-1', 'rss', '{}', 'active',"
                " 'default', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
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
        for draft in drafts:
            store_item(session, normalize(draft, tenant_id=TENANT).item)
        session.commit()
        session.execute(sa.text("update item set publish_status = 'ready'"))
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
    assert all(len(item_id) == 32 for item_id in ids)


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
