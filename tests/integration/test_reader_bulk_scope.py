"""Bulk reading stays inside the folder the reader selected."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from adapters import reader_api
from adapters.db_models import Item as ItemRow
from adapters.db_models import ReadState as ReadStateRow
from adapters.raw_payload import RawPayload
from adapters.repository import create_source
from app.pipeline import normalise_and_store
from plugins.collectors.rss import collector as rss

from .test_reader_api import (
    READ_TAG,
    READING_LIST,
    STARRED_TAG,
    auth_header,
    edit,
    login,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_marking_one_folder_read_preserves_the_other_folders_state(
    reader_app: FastAPI, reader_client: httpx.AsyncClient
) -> None:
    """Given two starred unread items, reading Work leaves News starred and unread."""
    body = (
        Path(__file__).resolve().parents[2]
        / "src/plugins/collectors/rss/fixtures/valid.xml"
    ).read_bytes()
    with reader_app.state.session_factory() as session:
        for folder in ("Work", "News"):
            url = f"https://{folder.lower()}.example/feed.xml"
            source = create_source(
                session,
                source_type="rss",
                url=url,
                canonical_url=url,
                schedule_policy='{"type":"fixed","interval":"30m"}',
                tenant_id="default",
                folder=folder,
                at="2026-01-01T00:00:00.000000Z",
            )
            drafts = rss.parse(
                RawPayload(url=url, status_code=200, body=body, source_id=source.id)
            )
            normalise_and_store(session, drafts, tenant_id="default")
        session.commit()

    token = await login(reader_client)
    folder_ids: dict[str, list[str]] = {}
    for folder in ("Work", "News"):
        response = await reader_client.get(
            "/api/greader.php/reader/api/0/stream/items/ids",
            headers=auth_header(token),
            params={"s": f"user/-/label/{folder}"},
        )
        assert response.status_code == 200
        folder_ids[folder] = [ref["id"] for ref in response.json()["itemRefs"]]
        assert len(folder_ids[folder]) >= 2
        starred = await edit(
            reader_client, token, [folder_ids[folder][0]], add=[STARRED_TAG]
        )
        assert starred.status_code == 200

    already_read = folder_ids["News"][1]
    marked = await edit(
        reader_client, token, [already_read], add=[READ_TAG, STARRED_TAG]
    )
    assert marked.status_code == 200
    with reader_app.state.session_factory() as session:
        bodies_before = session.execute(select(ItemRow.__table__)).all()

    response = await reader_client.post(
        "/api/greader.php/reader/api/0/mark-all-as-read",
        data={"T": token, "s": "user/-/label/Work"},
    )
    assert response.status_code == 200
    assert response.text == "OK"

    read = await reader_client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"s": READ_TAG},
    )
    assert {ref["id"] for ref in read.json()["itemRefs"]} == {
        *folder_ids["Work"],
        already_read,
    }
    starred = await reader_client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"s": STARRED_TAG},
    )
    assert {ref["id"] for ref in starred.json()["itemRefs"]} == {
        folder_ids["Work"][0],
        folder_ids["News"][0],
        already_read,
    }
    with reader_app.state.session_factory() as session:
        assert session.execute(select(ItemRow.__table__)).all() == bodies_before
        states_before_replay = session.execute(select(ReadStateRow.__table__)).all()
    replay = await reader_client.post(
        "/api/greader.php/reader/api/0/mark-all-as-read",
        data={"T": token, "s": "user/-/label/Work"},
    )
    assert replay.status_code == 200
    with reader_app.state.session_factory() as session:
        assert (
            session.execute(select(ReadStateRow.__table__)).all()
            == states_before_replay
        )


@pytest.mark.parametrize("scope", ["user/-/label/Empty", "feed/missing"])
async def test_user_marking_an_empty_scope_read_leaves_the_library_unchanged(
    reader_app: FastAPI, reader_client: httpx.AsyncClient, scope: str
) -> None:
    """Given a nonempty library, marking an absent folder or feed is a no-op."""
    token = await login(reader_client)
    with reader_app.state.session_factory() as session:
        before = session.execute(select(ReadStateRow.__table__)).all()
        assert len(session.execute(select(ItemRow.id)).all()) == 3
    response = await reader_client.post(
        "/api/greader.php/reader/api/0/mark-all-as-read",
        data={"T": token, "s": scope},
    )
    assert response.status_code == 200
    assert response.text == "OK"
    with reader_app.state.session_factory() as session:
        assert session.execute(select(ReadStateRow.__table__)).all() == before


async def test_mark_all_as_read_honours_a_microsecond_cutoff(
    reader_app: FastAPI, reader_client: httpx.AsyncClient
) -> None:
    """AC-328 end to end: `ts` keeps the unit the client sent.

    1700000000000000 is 2023-11-14 as microseconds and year 53 million as
    seconds. The fixture's dated items are January 2026, so only the
    microsecond reading leaves them unread; reading the value as seconds would
    sweep them in, which is what this guards against.
    """
    with reader_app.state.session_factory() as session:
        dated = [
            row.id
            for row in session.execute(select(ItemRow.__table__)).all()
            if row.published_at is not None
        ]
    assert dated, "the fixture must carry dated items for this to mean anything"

    token = await login(reader_client)
    response = await reader_client.post(
        "/api/greader.php/reader/api/0/mark-all-as-read",
        data={"T": token, "s": READING_LIST, "ts": "1700000000000000"},
    )
    assert response.status_code == 200

    read = await reader_client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"s": READ_TAG},
    )
    read_ids = {int(ref["id"]) for ref in read.json()["itemRefs"]}
    assert read_ids.isdisjoint(reader_api.reader_id(item_id) for item_id in dated)


async def test_mark_all_as_read_for_an_unknown_scope_is_refused_without_writing(
    reader_app: FastAPI, reader_client: httpx.AsyncClient
) -> None:
    """An unsupported stream is refused without changing stored read state."""
    with reader_app.state.session_factory() as session:
        before = session.execute(select(ReadStateRow.__table__)).all()

    unmodelled = (
        "bogus",
        "feed/",
        "user/-/state/com.google/bogus",
        "user/1234/label/Work",
    )
    for scope in unmodelled:
        response = await reader_client.post(
            "/api/greader.php/reader/api/0/mark-all-as-read",
            data={"T": await login(reader_client), "s": scope},
        )
        assert response.status_code == 400, (scope, response.text)

    with reader_app.state.session_factory() as session:
        assert session.execute(select(ReadStateRow.__table__)).all() == before
