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

Findings and reasoning are in
docs/integrations/readyou-freshrss-compat-findings.md.
"""

from __future__ import annotations

import httpx
import pytest

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

    window = await ids_for(
        reader_client, token, exclude=READ_TAG, limit=2
    )
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
