"""Reader continuation must survive membership changes between pages.

Scenario register (normative source: SCENARIOS.md, AC-267..269; issue #274).

A continuation used to be an offset into the *current* filtered set, so any
membership change between page requests -- an item read by another client, a
deletion, a new arrival -- shifted every later page. Read-synchronisation
walks `stream/items/ids` while the user is actively reading in another
client, so this is the common case, not a corner: the user marking page one
read in NNW while Read You walks page two silently dropped an unread item
from the walk (and Read You then treated never-seen ids as read).

Every scenario follows the real client pattern: fetch page one, let the
world change, then continue *from page one's cursor* -- the exact shape
`fetchItemIdsAndContinue` runs. The conftest feed has three items: Second
(2026-01-07, newest), First (2026-01-05), Third (no date, so it sorts last).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.pipeline import normalise_and_store
from core.models import ItemDraft

from .conftest import TENANT
from .test_reader_api import READ_TAG, auth_header, edit, login

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def reader(
    reader_app: FastAPI,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """The shared reader app *and* its client: membership edits need the DB."""
    transport = httpx.ASGITransport(app=reader_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as opened:
        yield reader_app, opened


async def _get(
    client: httpx.AsyncClient, token: str, params: dict[str, str]
) -> dict[str, Any]:
    response = await client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params=params,
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _page_params(**extra: str) -> dict[str, str]:
    base = {"output": "json", "s": "user/-/state/com.google/reading-list", "n": "1"}
    base.update(extra)
    return base


async def _walk_all(client: httpx.AsyncClient, token: str, **params: str) -> list[str]:
    """Walk a stream to the end from a cursor, as Read You does."""
    walked: list[str] = []
    cursor: str | None = None
    while True:
        request = _page_params(**params)
        if cursor:
            request["c"] = cursor
        page = await _get(client, token, request)
        walked.extend(ref["id"] for ref in page["itemRefs"])
        cursor = page["continuation"]
        if not cursor:
            return walked


@pytest.mark.asyncio
async def test_a_walk_covers_a_timestamp_tie_wider_than_one_page(
    reader: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """AC-267, AC-269: the tiebreak must agree with the predicate's direction.

    The stream ordered by `id` **ascending** while the continuation predicate
    assumed descending (`id < last served`). A page boundary that landed inside
    a group of items sharing one `published_at` therefore re-served the group's
    smallest id and never reached the rest: Read You folds everything it has
    seen into its read set, so the unserved ids stayed unread in the client for
    good -- the live symptom was "some articles I read came back unread".

    The pre-existing fixtures could not catch it: every timestamp in them is
    distinct, and a tie only matters when the boundary falls inside the group.
    The undated tail is one huge tie (`nullslast` puts it last), so it is
    exercised here too.
    """
    app, client = reader
    token = await login(client)
    tied = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)

    with app.state.session_factory() as session:
        normalise_and_store(
            session,
            [
                ItemDraft(
                    source_id="src-1",
                    external_id=f"urn:uuid:tied-{n}",
                    title=f"Tied {n}",
                    canonical_url=f"https://blog.example.com/2026/02/tied-{n}",
                    published_at=tied,
                    content=f"<p>Tied {n}</p>",
                )
                for n in range(5)
            ]
            + [
                ItemDraft(
                    source_id="src-1",
                    external_id=f"urn:uuid:undated-{n}",
                    title=f"Undated {n}",
                    canonical_url=f"https://blog.example.com/2026/02/undated-{n}",
                    published_at=None,
                    content=f"<p>Undated {n}</p>",
                )
                for n in range(5)
            ],
            tenant_id=TENANT,
        )
        session.commit()

    every_id = [
        ref["id"]
        for ref in (await _get(client, token, _page_params(n="10000")))["itemRefs"]
    ]
    walked = await _walk_all(client, token, n="2")

    assert len(walked) == len(set(walked)), f"the walk repeated ids: {walked}"
    assert set(walked) == set(every_id), (
        f"the walk missed {sorted(set(every_id) - set(walked))} of {len(every_id)} "
        "items, so a tiebreak disagreed with the continuation predicate"
    )


@pytest.mark.asyncio
async def test_a_read_item_inside_a_tied_group_reaches_the_read_walk(
    reader: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """AC-268: Read You's read-set walk serves a tied group whole.

    Given five read items sharing one published_at -- a group wider than one
    page
    When the client walks the read stream (`s=reading-list&it=read`) at that
    page size, the shape `getReadItemIds` uses on every global sync
    Then every one of them arrives

    This is the shape the user actually hit: `remoteReadIds` missing an entry
    means `toBeReadLocal = remoteRead ∩ localUnread` never carries "the server
    says read" back to the phone, so the article stays unread there for good.
    `it=` is applied in SQL *before* the limit, so the filtered read stream is
    its own query path and needs its own case; the unfiltered walks above do
    not cover it.
    """
    app, client = reader
    token = await login(client)
    tied = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

    with app.state.session_factory() as session:
        normalise_and_store(
            session,
            [
                ItemDraft(
                    source_id="src-1",
                    external_id=f"urn:uuid:read-tie-{n}",
                    title=f"Read Tie {n}",
                    canonical_url=f"https://blog.example.com/2026/03/read-tie-{n}",
                    published_at=tied,
                    content=f"<p>Read Tie {n}</p>",
                )
                for n in range(5)
            ],
            tenant_id=TENANT,
        )
        session.commit()

    # The group is the newest thing in the feed, so it leads the reading list.
    newest_first = [
        ref["id"]
        for ref in (await _get(client, token, _page_params(n="10000")))["itemRefs"]
    ]
    group = newest_first[:5]
    assert len(group) == 5, newest_first

    marked = await edit(client, token, group, add=[READ_TAG])
    assert marked.status_code == 200, marked.text

    walked = await _walk_all(client, token, n="2", it=READ_TAG)
    assert len(walked) == len(set(walked)), f"the read walk repeated ids: {walked}"
    assert set(walked) == set(group), (
        f"the read walk served {sorted(walked)}; "
        f"{sorted(set(group) - set(walked))} of the read items never arrived"
    )


@pytest.mark.asyncio
async def test_user_items_read_between_pages_are_still_all_served(
    reader: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """Given a paged unread walk, when page one is read before page two is
    requested, then the continuation still serves the never-served unread
    items: none is skipped.

    With the offset cursor the unread set shrank by one, so the offset-1
    second page began past an item that had never been served -- NNW marking
    page one read while Read You walked page two hid a real article from the
    sync.
    """
    _, client = reader
    token = await login(client)
    params = _page_params(xt=READ_TAG)

    first = await _get(client, token, params)
    served = [ref["id"] for ref in first["itemRefs"]]
    cursor = first["continuation"]
    assert len(served) == 1 and cursor, "the scenario needs a page and a cursor"

    marked = await edit(client, token, served, add=[READ_TAG])
    assert marked.status_code == 200

    resumed: list[str] = []
    while cursor:
        page = await _get(client, token, dict(params, c=str(cursor)))
        resumed.extend(ref["id"] for ref in page["itemRefs"])
        cursor = page["continuation"]
    # First and Third were never served and must arrive; Second was the
    # boundary and is now read, so it must not reappear.
    assert resumed == ["90191516472070762", "558277528912161179"], (
        f"resuming after marking page one read served {resumed}"
    )


@pytest.mark.asyncio
async def test_user_items_deleted_between_pages_are_not_skipped(
    reader: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """Given a paged walk, when the served item is deleted before page two
    is requested, then the continuation still serves the never-served items.

    Deleting a served item shifted the offset cursor one row deeper, so the
    next page began past an unserved item. Deletion goes through the same
    repository the app uses; no public route removes items.
    """
    app, client = reader
    token = await login(client)
    params = _page_params()

    first = await _get(client, token, params)
    served = [ref["id"] for ref in first["itemRefs"]]
    cursor = first["continuation"]
    assert len(served) == 1 and cursor, "the scenario needs a page and a cursor"

    from adapters.repository import delete_items

    with app.state.session_factory() as session:
        delete_items(session, served)
        session.commit()

    resumed: list[str] = []
    while cursor:
        page = await _get(client, token, dict(params, c=str(cursor)))
        resumed.extend(ref["id"] for ref in page["itemRefs"])
        cursor = page["continuation"]
    assert resumed == ["90191516472070762", "558277528912161179"], (
        f"resuming after deleting the served item served {resumed}"
    )


@pytest.mark.asyncio
async def test_user_items_inserted_between_pages_are_not_duplicated(
    reader: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """Given a paged walk, when a newer item arrives before page two is
    requested, then the continuation continues after the served boundary:
    the arrival is left to the next sync from the top and nothing is
    re-served or skipped.

    The offset cursor slid past the served prefix when the arrival pushed
    rows deeper, re-serving page-one ids -- which Read You folded into its
    read set.
    """
    app, client = reader
    token = await login(client)
    params = _page_params()

    first = await _get(client, token, params)
    served = [ref["id"] for ref in first["itemRefs"]]
    cursor = first["continuation"]
    assert len(served) == 1 and cursor, "the scenario needs a page and a cursor"

    with app.state.session_factory() as session:
        normalise_and_store(
            session,
            [
                ItemDraft(
                    source_id="src-1",
                    external_id="urn:uuid:1a2b3c4d-0004-4000-8000-000000000004",
                    title="Fourth Post Arrived Mid Walk",
                    canonical_url="https://blog.example.com/2026/01/fourth-post",
                    published_at=datetime(2026, 1, 10, tzinfo=UTC),
                    content="<p>Fourth</p>",
                )
            ],
            tenant_id=TENANT,
        )
        session.commit()

    resumed: list[str] = []
    while cursor:
        page = await _get(client, token, dict(params, c=str(cursor)))
        resumed.extend(ref["id"] for ref in page["itemRefs"])
        cursor = page["continuation"]
    # The walk continues after Second in its original position: First, then
    # Third, with no repeats; Fourth is newer than the boundary and belongs
    # to the *next* sync from the top.
    assert resumed == ["90191516472070762", "558277528912161179"], (
        f"resuming after a mid-walk arrival served {resumed}"
    )
