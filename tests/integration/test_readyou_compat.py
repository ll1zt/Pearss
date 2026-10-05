"""Read You compatibility: the request shapes this client actually sends.

Every scenario here replays a request Read You really issues, taken from
`GoogleReaderAPI.kt` at Ashinch/ReadYou@d2b979c. They are not invented
edge cases: each one corresponds to a code path in the client, and the
scenario name states the user-visible consequence when it breaks.

Why a separate file from test_reader_api.py: that suite covers the
protocol contract (auth asymmetry, id forms, the publish gate). This one
covers a *specific client's* usage of it. When Read You changes, only
this file should need to change -- which is the point of keeping the two
apart.

Findings and reasoning are recorded in the scenario names below; the
client's request shapes were read from `GoogleReaderAPI.kt` at
Ashinch/ReadYou@d2b979c and replayed against a migrated instance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.pipeline import normalise_and_store
from core.models import ItemDraft

from .conftest import TENANT
from .test_reader_api import (
    READ_TAG,
    READING_LIST,
    STARRED_TAG,
    auth_header,
    edit,
    login,
)

# Read You's read stream expressed as an include tag, which is how it asks
# for read items against a FreshRSS account (`useIt = isFreshRss`).
# GoogleReaderAPI.kt:305 -- `add(Pair(if (useIt) "it" else "s", Stream.Read.tag))`
IT_READ = READ_TAG
# The limit Read You passes on every stream/items/ids call.
READYOU_LIMIT = 1000


async def ids_for(
    client: httpx.AsyncClient,
    token: str,
    *,
    include: str | None = None,
    exclude: str | None = None,
    limit: int | None = None,
    stream: str | None = None,
) -> list[str]:
    """`stream/items/ids` in Read You's shape.

    Read You always sends `output=json`, always sends `n`, and uses `it=` for
    an include filter -- never `s=`, which is the whole reason these tests
    exist.
    """
    params: dict[str, str] = {"output": "json", "s": stream or READING_LIST}
    if limit is not None:
        params["n"] = str(limit)
    if include is not None:
        params["it"] = include
    if exclude is not None:
        params["xt"] = exclude
    response = await client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params=params,
    )
    assert response.status_code == 200, response.text
    refs: list[dict[str, str]] = response.json().get("itemRefs", [])
    return [ref["id"] for ref in refs]


async def subscribe(
    client: httpx.AsyncClient, token: str, feed_url: str
) -> dict[str, object]:
    """Add a feed the way Read You does: quickadd, both places, json out."""
    response = await client.post(
        "/api/greader.php/reader/api/0/subscription/quickadd",
        headers=auth_header(token),
        params={"output": "json", "quickadd": feed_url},
        data={"T": token, "quickadd": feed_url},
    )
    assert response.status_code == 200, response.text
    payload: dict[str, object] = response.json()
    return payload


async def folders_by_feed(client: httpx.AsyncClient, token: str) -> dict[str, str]:
    """Map feed stream id -> folder label, as `subscription/list` reports it."""
    response = await client.get(
        "/api/greader.php/reader/api/0/subscription/list",
        headers=auth_header(token),
    )
    assert response.status_code == 200, response.text
    return {
        sub["id"]: (sub["categories"][0]["label"] if sub["categories"] else "")
        for sub in response.json()["subscriptions"]
    }


# --------------------------------------------------------------------------
# The read stream, asked for with `it=` instead of `s=`
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_read_stream_asked_with_an_include_tag_returns_only_read_items(
    reader_client: httpx.AsyncClient,
) -> None:
    """Read You's read-items query must not answer with the whole library.

    Given three items, one of them read
    When Read You asks for read items with `it=<read>` on the reading list
    Then only the read item comes back

    This is the bug behind the oscillating unread count: the client reads
    the result as "these are read", so answering with everything makes it
    mark every unread item read on every sync.
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)
    assert len(everything) >= 2, "fixture should have several items"

    read_id = everything[0]
    await edit(reader_client, token, [read_id], add=[READ_TAG])

    read_items = await ids_for(
        reader_client, token, include=IT_READ, limit=READYOU_LIMIT
    )
    assert read_items == [read_id], (
        f"asked for read items, got {len(read_items)} of {len(everything)}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_include_tag_for_a_state_nothing_carries_returns_nothing(
    reader_client: httpx.AsyncClient,
) -> None:
    """No item is starred, so the starred include filter yields an empty stream.

    An unimplemented or unmatched filter must answer "nothing", never the
    unfiltered list -- the same rule the `s=starred` stream already follows.
    """
    token = await login(reader_client)
    starred = await ids_for(
        reader_client, token, include=STARRED_TAG, limit=READYOU_LIMIT
    )
    assert starred == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_include_and_exclude_together_intersect_then_subtract(
    reader_client: httpx.AsyncClient,
) -> None:
    """`it` selects, `xt` removes. Both at once must not ignore either.

    Given two read items, one of them also starred
    When the query asks for read (`it`) but not starred (`xt`)
    Then only the unstarred read item comes back
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)
    assert len(everything) >= 2

    plain_read, starred_read = everything[0], everything[1]
    await edit(reader_client, token, [plain_read, starred_read], add=[READ_TAG])
    await edit(reader_client, token, [starred_read], add=[STARRED_TAG])

    # Both are read; the exclusion must remove the starred one.
    both = await ids_for(
        reader_client, token, include=IT_READ, exclude=STARRED_TAG, limit=READYOU_LIMIT
    )
    assert both == [plain_read]
    # Sanity: the star really landed, or the assertion above proves nothing.
    starred_only = await ids_for(
        reader_client, token, include=STARRED_TAG, limit=READYOU_LIMIT
    )
    assert starred_only == [starred_read]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_read_stream_can_be_paginated_to_completion(
    reader_client: httpx.AsyncClient,
) -> None:
    """Read You accumulates ids across pages, so page two must not repeat page one.

    Given one item read
    When both pages of a two-item window are walked
    Then the union is the read item and no id appears twice

    Read You loops while `continuation` is non-null, so a reply that
    repeats the same ids forever would hang the client rather than fail it.
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)
    read_id = everything[0]
    await edit(reader_client, token, [read_id], add=[READ_TAG])

    first = await ids_for(reader_client, token, include=IT_READ, limit=1)
    # The read set is one item wide, so the first page already holds it all;
    # a second page exists only to prove the cursor terminates.
    assert first == [read_id]


# --------------------------------------------------------------------------
# The unread stream, which is what the badge counts
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_unread_stream_is_complete_when_a_read_item_is_recent(
    reader_client: httpx.AsyncClient,
) -> None:
    """A `n=` window must be applied *after* the read filter, not before.

    Given three items where the newest is read
    When Read You asks for unread items with a limit of two
    Then it receives the two genuinely unread items

    The bug this catches: the server took the two newest rows, then dropped
    the read one, answering with a single id. The client counts what it
    receives, so the unread badge read 1 where the truth was 2 -- and the
    number drifted as items were read, which is the "count keeps changing"
    symptom.
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)
    assert len(everything) == 3, "this scenario needs exactly three items"

    newest = everything[0]
    await edit(reader_client, token, [newest], add=[READ_TAG])

    window = await ids_for(reader_client, token, exclude=READ_TAG, limit=2)
    assert len(window) == 2, (
        f"a limit of 2 must yield 2 unread items, got {len(window)}: {window}"
    )
    assert newest not in window


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_unread_count_does_not_change_while_nothing_is_marked(
    reader_client: httpx.AsyncClient,
) -> None:
    """Two identical unread queries, no intervening writes, same answer.

    This is the direct regression test for the oscillating badge: whatever
    the client computes from these replies must not flip between refreshes
    when the server state has not changed.
    """
    token = await login(reader_client)
    first = await ids_for(reader_client, token, exclude=READ_TAG, limit=READYOU_LIMIT)
    second = await ids_for(reader_client, token, exclude=READ_TAG, limit=READYOU_LIMIT)
    assert first == second

    # And the complement must be a partition: read + unread == everything,
    # with no id in both. The oscillation was the two sets disagreeing.
    read = await ids_for(reader_client, token, include=IT_READ, limit=READYOU_LIMIT)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)
    assert sorted(read + first) == sorted(everything), (
        "read and unread must partition the reading list"
    )


# --------------------------------------------------------------------------
# Subscribing from the app
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quickadd_subscribes_a_feed_and_answers_with_its_stream_id(
    reader_client: httpx.AsyncClient,
) -> None:
    """Read You adds a feed by URL and needs the new subscription's id back.

    Given a feed URL this deployment does not collect
    When the app calls subscription/quickadd with it
    Then the reply names the new feed's stream id and the source now exists

    The client treats a missing `streamId` as a hard failure (it
    `requireNotNull`s the field), and that exception fails the *entire*
    sync work item, which is then retried forever. So this endpoint
    answering nothing is not a local "add feed failed" -- it is the whole
    account never syncing again.
    """
    token = await login(reader_client)
    feed_url = "https://new.example.com/feed.xml"

    response = await reader_client.post(
        "/api/greader.php/reader/api/0/subscription/quickadd",
        headers=auth_header(token),
        params={"output": "json", "quickadd": feed_url},
        data={"T": token, "quickadd": feed_url},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["numResults"] == 1
    assert payload["query"] == feed_url
    assert payload["streamId"].startswith("feed/"), payload

    # And it is a real subscription, not just a plausible reply.
    listing = await reader_client.get(
        "/api/greader.php/reader/api/0/subscription/list",
        headers=auth_header(token),
    )
    urls = {sub["url"] for sub in listing.json()["subscriptions"]}
    assert feed_url in urls
    assert payload["streamId"] in {sub["id"] for sub in listing.json()["subscriptions"]}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quickadd_for_an_already_subscribed_feed_is_not_an_error(
    reader_client: httpx.AsyncClient,
) -> None:
    """Adding a feed twice answers with the same subscription.

    The intent is "make sure this is subscribed", so a repeat is satisfied
    by the existing source -- the same rule POST /v1/sources follows. A
    client that re-sends after a dropped response must not be told it
    failed, and must not end up with two sources for one URL.
    """
    token = await login(reader_client)
    feed_url = "https://dupe.example.com/feed.xml"

    async def quickadd() -> dict[str, object]:
        response = await reader_client.post(
            "/api/greader.php/reader/api/0/subscription/quickadd",
            headers=auth_header(token),
            params={"quickadd": feed_url},
            data={"T": token, "quickadd": feed_url},
        )
        assert response.status_code == 200, response.text
        payload: dict[str, object] = response.json()
        return payload

    first = await quickadd()
    second = await quickadd()
    assert first["streamId"] == second["streamId"]

    listing = await reader_client.get(
        "/api/greader.php/reader/api/0/subscription/list",
        headers=auth_header(token),
    )
    matching = [s for s in listing.json()["subscriptions"] if s["url"] == feed_url]
    assert len(matching) == 1, f"one URL must mean one subscription: {matching}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quickadd_rejects_a_blank_url_without_subscribing(
    reader_client: httpx.AsyncClient,
) -> None:
    """A blank quickadd must not create a source with an empty URL."""
    token = await login(reader_client)
    response = await reader_client.post(
        "/api/greader.php/reader/api/0/subscription/quickadd",
        headers=auth_header(token),
        params={"quickadd": "   "},
        data={"T": token, "quickadd": "   "},
    )
    assert response.status_code == 400, response.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quickadd_requires_a_token(reader_client: httpx.AsyncClient) -> None:
    """The write endpoint must not be reachable without credentials.

    quickadd creates a source, so it is a write: it takes the token from
    the form field `T` like every other write, and a request without one is
    refused rather than creating an unauthenticated subscription.
    """
    response = await reader_client.post(
        "/api/greader.php/reader/api/0/subscription/quickadd",
        params={"quickadd": "https://anon.example.com/feed.xml"},
        data={"quickadd": "https://anon.example.com/feed.xml"},
    )
    assert response.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_tag_moves_every_source_in_the_folder(
    reader_client: httpx.AsyncClient,
) -> None:
    """Read You renames a group; every subscription in it must follow.

    Given a feed filed under "Work"
    When the app renames that group to "Office"
    Then the subscription list shows it under "Office" and "Work" is gone

    Feeds are the only place a folder name exists, so a rename that
    touched only some of them would split one group into two.
    """
    token = await login(reader_client)
    await subscribe(reader_client, token, "https://a.example.com/feed.xml")
    await reader_client.post(
        "/api/greader.php/reader/api/0/subscription/edit",
        headers=auth_header(token),
        data={"T": token, "ac": "edit", "s": "feed/src-1", "a": "user/-/label/Work"},
    )

    renamed = await reader_client.post(
        "/api/greader.php/reader/api/0/rename-tag",
        headers=auth_header(token),
        data={"T": token, "s": "user/-/label/Work", "dest": "user/-/label/Office"},
    )
    assert renamed.status_code == 200, renamed.text

    folders = await folders_by_feed(reader_client, token)
    assert folders["feed/src-1"] == "Office"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disable_tag_removes_the_folder_but_keeps_the_subscriptions(
    reader_client: httpx.AsyncClient,
) -> None:
    """Read You deletes a group; the feeds inside must survive it.

    Given a feed filed under "Work"
    When the app removes that group
    Then the feed is still subscribed, now in the default folder
    And "Work" is no longer offered as a folder

    The alternative -- refusing while the folder is non-empty -- would leave
    the user with a group they deleted, since the client unsubscribes only
    the feeds it was asked to.
    """
    token = await login(reader_client)
    await subscribe(reader_client, token, "https://b.example.com/feed.xml")
    await reader_client.post(
        "/api/greader.php/reader/api/0/subscription/edit",
        headers=auth_header(token),
        data={"T": token, "ac": "edit", "s": "feed/src-1", "a": "user/-/label/Work"},
    )

    removed = await reader_client.post(
        "/api/greader.php/reader/api/0/disable-tag",
        headers=auth_header(token),
        data={"T": token, "s": "user/-/label/Work"},
    )
    assert removed.status_code == 200, removed.text

    folders = await folders_by_feed(reader_client, token)
    assert folders["feed/src-1"] == "default"

    tags = await reader_client.get(
        "/api/greader.php/reader/api/0/tag/list",
        headers=auth_header(token),
    )
    labels = {tag["id"] for tag in tags.json()["tags"] if tag.get("type") == "folder"}
    assert "user/-/label/Work" not in labels


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rename_and_disable_reject_a_non_label_stream(
    reader_client: httpx.AsyncClient,
) -> None:
    """A malformed group id is refused, not silently ignored.

    Both endpoints act on a folder; an id that names something else must
    not be read as "some folder" and applied to the wrong sources.
    """
    token = await login(reader_client)
    for path, body in (
        ("rename-tag", {"s": "feed/src-1", "dest": "user/-/label/X"}),
        ("disable-tag", {"s": "user/-/state/com.google/starred"}),
    ):
        response = await reader_client.post(
            f"/api/greader.php/reader/api/0/{path}",
            headers=auth_header(token),
            data={"T": token, **body},
        )
        assert response.status_code == 400, f"{path}: {response.text}"


# --------------------------------------------------------------------------
# Pagination: `continuation`
# --------------------------------------------------------------------------


async def walk_ids(
    client: httpx.AsyncClient,
    token: str,
    *,
    include: str | None = None,
    exclude: str | None = None,
    limit: int,
    max_pages: int = 50,
) -> list[str]:
    """Walk `stream/items/ids` the way Read You does: follow `continuation`.

    This is `fetchItemIdsAndContinue` in `GoogleReaderRssService.kt`, which
    loops while the reply carries a non-null `continuation`. Replaying that
    loop is the point: the client's ability to see every item depends on our
    cursor, so asserting against our own paging (rather than the client's
    abstraction) would be testing the wrong contract.
    """
    params: dict[str, str] = {"output": "json", "s": READING_LIST, "n": str(limit)}
    if include is not None:
        params["it"] = include
    if exclude is not None:
        params["xt"] = exclude

    collected: list[str] = []
    cursor: str | None = None
    for _ in range(max_pages):
        query = dict(params)
        if cursor is not None:
            query["c"] = cursor
        response = await client.get(
            "/api/greader.php/reader/api/0/stream/items/ids",
            headers=auth_header(token),
            params=query,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        collected.extend(ref["id"] for ref in payload.get("itemRefs", []))
        cursor = payload.get("continuation")
        if cursor is None:
            return collected
    raise AssertionError(f"pagination did not terminate after {max_pages} pages")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_limited_query_offers_a_cursor_for_the_rest(
    reader_client: httpx.AsyncClient,
) -> None:
    """A short page must say there is more, or the client believes it has all.

    Given three items and a page size of two
    When Read You asks for the first page
    Then the reply carries a continuation, and following it yields the third

    Without the cursor the client's loop terminates after one page and it
    concludes the two ids it saw are the whole stream. That is the same
    failure as returning a wrong set: a silent, plausible-looking subset.
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)
    assert len(everything) == 3, "this scenario needs exactly three items"

    first = await reader_client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"output": "json", "s": READING_LIST, "n": "2"},
    )
    payload = first.json()
    assert len(payload["itemRefs"]) == 2
    assert payload.get("continuation"), (
        f"a partial page must offer a cursor; got {payload.get('continuation')!r}"
    )

    walked = await walk_ids(reader_client, token, limit=2)
    assert sorted(walked) == sorted(everything)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_walking_the_pages_reaches_every_item_exactly_once(
    reader_client: httpx.AsyncClient,
) -> None:
    """The union of the pages is the stream, with no gaps and no repeats.

    Given three items
    When the stream is walked two at a time
    Then every item appears exactly once

    Repeats would be worse than useless: Read You accumulates ids into a
    Set and then treats membership as "read", so a cursor that re-served an
    earlier page would inflate the set rather than fail visibly.
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)

    for page_size in (1, 2, 3, 4):
        walked = await walk_ids(reader_client, token, limit=page_size)
        assert sorted(walked) == sorted(everything), (
            f"page size {page_size} walked {walked}, expected {everything}"
        )
        assert len(walked) == len(set(walked)), f"page size {page_size} repeated ids"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_last_page_carries_no_cursor(
    reader_client: httpx.AsyncClient,
) -> None:
    """Terminating matters as much as continuing: a cursor forever hangs the client.

    Given a page size larger than the stream
    When the query is answered
    Then there is no continuation

    Read You's loop is `while continuation != null`, so a cursor on the
    final page would make it page endlessly, re-fetching the same ids.
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)

    response = await reader_client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"output": "json", "s": READING_LIST, "n": str(len(everything) + 5)},
    )
    payload = response.json()
    assert len(payload["itemRefs"]) == len(everything)
    assert payload.get("continuation") in (None, ""), (
        f"the last page must not offer a cursor; got {payload.get('continuation')!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_paging_a_filtered_stream_walks_only_matching_items(
    reader_client: httpx.AsyncClient,
) -> None:
    """Filters apply to every page, not just the first.

    Given two read items out of three
    When the read stream is walked one item at a time
    Then exactly the read items arrive, and the cursor still terminates

    This is the shape Read You uses for its read set on a FreshRSS account
    (`s=reading-list&it=read`), and the shape whose truncation caused the
    missed read propagation. With a cursor the client can now see all of it.
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)
    await edit(reader_client, token, everything[:2], add=[READ_TAG])

    walked = await walk_ids(reader_client, token, include=IT_READ, limit=1)
    assert sorted(walked) == sorted(everything[:2])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_cursor_at_the_depth_cap_terminates_the_walk(
    reader_client: httpx.AsyncClient,
) -> None:
    """The depth cap must end the stream, not serve the same page forever.

    Given a cursor already at the server's maximum offset
    When the next page is requested
    Then it comes back empty and offers no further cursor

    A clamped cursor that kept being offered would make Read You's
    `while continuation != null` loop run forever, re-requesting one offset
    -- a hang rather than a wrong answer, and one caused purely by our own
    guard.
    """
    token = await login(reader_client)
    for cursor in ("100000", "100001", "999999999"):
        response = await reader_client.get(
            "/api/greader.php/reader/api/0/stream/items/ids",
            headers=auth_header(token),
            params={"output": "json", "s": READING_LIST, "n": "2", "c": cursor},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["itemRefs"] == [], f"cursor {cursor} returned items"
        assert payload.get("continuation") in (None, ""), (
            f"cursor {cursor} offered another cursor; the client would loop"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_new_style_cursor_at_the_depth_cap_terminates_the_walk(
    reader_client: httpx.AsyncClient,
) -> None:
    """AC-270: the cap terminates the keyset walk too, not just the offset one.

    Given a keyset cursor whose served count is at the server's cap
    When the next page is requested
    Then it comes back empty and offers no further cursor

    The cap's first fix covered only the legacy bare-integer cursor, and that
    one terminated by accident: its offset fell past the end of the table. A
    keyset cursor at the cap decoded to `(0, None)` -- page one -- so the walk
    restarted from the top instead of ending, and Read You's
    `while continuation != null` loop never finished. #274 made the cursor's
    `served` counter climb faster, which is what made this reachable in
    practice.
    """
    token = await login(reader_client)
    for cursor in (
        "100000|2026-01-07T00:00:00.000000Z|1",
        "100001|2026-01-07T00:00:00.000000Z|1",
        "100000|-|1",
    ):
        response = await reader_client.get(
            "/api/greader.php/reader/api/0/stream/items/ids",
            headers=auth_header(token),
            params={"output": "json", "s": READING_LIST, "n": "2", "c": cursor},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["itemRefs"] == [], f"cursor {cursor} returned items"
        assert payload.get("continuation") in (None, ""), (
            f"cursor {cursor} offered another cursor; the client would loop"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_malformed_cursor_at_the_depth_cap_still_restarts(
    reader_client: httpx.AsyncClient,
) -> None:
    """AC-272: the cap applies to well-formed cursors, not to broken ones.

    Given a cursor whose served count is past the cap but whose shape is
    unparseable
    When the next page is requested
    Then it restarts from the first page, like any other unparseable cursor --
    it is not reported as "the walk is over"

    The cap used to be read first, so `100000|garbage` (no second separator)
    came back as an empty page: an unparseable cursor was silently promoted to
    "you have finished", the one answer a client cannot recover from. A
    non-numeric external id (`...|xyz`) was worse than wrong -- `int()` raised
    straight through to a 500, which Read You surfaces as a failed sync.
    Restarting is safe: the reply carries a fresh, well-formed cursor, so the
    walk moves on. A *well-formed* cursor at the cap still ends the walk
    (#275, `test_a_new_style_cursor_at_the_depth_cap_terminates_the_walk`).
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)
    assert len(everything) == 3, "this scenario needs the whole feed"

    for cursor in ("100000|garbage", "100000|2026-01-07T00:00:00.000000Z|xyz"):
        response = await reader_client.get(
            "/api/greader.php/reader/api/0/stream/items/ids",
            headers=auth_header(token),
            params={"output": "json", "s": READING_LIST, "n": "1000", "c": cursor},
        )
        assert response.status_code == 200, f"cursor {cursor!r}: {response.text}"
        got = [ref["id"] for ref in response.json()["itemRefs"]]
        assert got == everything, (
            f"cursor {cursor!r} is unparseable, so the walk restarts from the "
            f"top; got {got}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_malformed_cursor_restarts_rather_than_failing(
    reader_client: httpx.AsyncClient,
) -> None:
    """A cursor we cannot parse is treated as "start over", not an error.

    The cursor is ours to mint and the client only echoes it, so a bad value
    means a bug on this side or a hand-written request. Neither is improved
    by a 500, and Read You would surface that as a failed sync.
    """
    token = await login(reader_client)
    everything = await ids_for(reader_client, token, limit=READYOU_LIMIT)

    response = await reader_client.get(
        "/api/greader.php/reader/api/0/stream/items/ids",
        headers=auth_header(token),
        params={"output": "json", "s": READING_LIST, "n": "1000", "c": "not-a-number"},
    )
    assert response.status_code == 200, response.text
    got = [ref["id"] for ref in response.json()["itemRefs"]]
    assert got == everything


# --------------------------------------------------------------------------
# The scale at which the missing cursor actually bit
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_read_item_marked_beyond_the_first_page_still_propagates(
    reader_app: FastAPI, reader_client: httpx.AsyncClient
) -> None:
    """The regression this endpoint's cursor exists for, at real scale.

    Given more items than fit in one page
    And an item far enough back to sit outside a first page of 1000
    When another client marks it read and this client syncs
    Then the read is visible in the client's read set

    This is the exact shape of the reported bug: NetNewsWire marked items
    read, Read You never showed them read. Read You's sync intersects the
    remote read set with its local unread set, so an item missing from the
    read set -- because the read query returned only its first page and we
    never offered a cursor -- could not be reconciled. The reverse direction
    worked because it writes through edit-tag, which is not paged.

    The count is deliberately past the page size: with 1200 items a single
    1000-item page cannot contain the oldest ones, which is what makes this
    a regression test rather than a restatement of the paging tests above.
    """
    token = await login(reader_client)
    headers = auth_header(token)
    factory = reader_app.state.session_factory
    base = datetime(2026, 1, 1, tzinfo=UTC)

    drafts = [
        ItemDraft(
            source_id="src-1",
            external_id=f"scale-{index}",
            title=f"Scale item {index}",
            canonical_url=f"https://blog.example.com/scale/{index}",
            published_at=base + timedelta(minutes=index),
            content=f"<p>body {index}</p>",
        )
        for index in range(1200)
    ]
    with Session(factory.kw["bind"]) as session:
        for start in range(0, len(drafts), 200):
            normalise_and_store(session, drafts[start : start + 200], tenant_id=TENANT)
        session.commit()

    async def walk(include: str | None = None) -> list[str]:
        """Read You's `fetchItemIdsAndContinue`: follow the cursor to the end."""
        collected: list[str] = []
        cursor: str | None = None
        for _ in range(60):
            params: dict[str, str] = {
                "output": "json",
                "s": READING_LIST,
                "n": str(READYOU_LIMIT),
            }
            if include is not None:
                params["it"] = include
            if cursor is not None:
                params["c"] = cursor
            response = await reader_client.get(
                "/api/greader.php/reader/api/0/stream/items/ids",
                headers=headers,
                params=params,
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            collected.extend(ref["id"] for ref in payload["itemRefs"])
            cursor = payload.get("continuation")
            if cursor is None:
                return collected
        raise AssertionError("the walk never terminated")

    everything = await walk()
    assert len(everything) >= 1200, f"expected the whole stream, got {len(everything)}"

    # The oldest item: last in newest-first order, so far outside one page.
    oldest = everything[-1]
    written = await reader_client.post(
        "/api/greader.php/reader/api/0/edit-tag",
        data={"T": token, "i": [oldest], "a": [READ_TAG]},
    )
    assert written.status_code == 200

    # The client's local view is its pre-sync state: it holds every item and
    # still considers this one unread, because the mark happened elsewhere.
    local_unread = set(everything)
    read_set = set(await walk(include=IT_READ))

    assert oldest in read_set, (
        "the read set is missing an item that was marked read; a client "
        "cannot reconcile what it is not told about"
    )
    assert read_set & local_unread == {oldest}
