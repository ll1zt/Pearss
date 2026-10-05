"""`ot` (published-after) is honoured on stream/items/ids (issue #92).

ReadYou sends `ot` on every global sync -- `getReadItemIds(since = now - 1
month)` (`GoogleReaderRssService.kt:293`) -- and `fetchItemIdsAndContinue`
(`:672`) pages until `continuation` is null with no page cap. Ignoring the
parameter therefore walked the whole read history each sync, and once that
history exceeds this server's single-walk cap it is truncated, so older
remote-read items stop propagating. The disposition is in ADR 0024's #92
revision; the semantics follow miniflux for the **dated** rows
(`published_at > ot`, applied only for a positive integer).

Undated rows are the exception, and ADR 0024's 2026-10-05 revision is why:
miniflux entries always carry a `published_at`, so its strict `>` says nothing
about a NULL here. A window that drops them makes their read state
unsynchronisable, because Read You derives `remoteRead` from this exact query
and `remoteUnread` from an unwindowed one -- an already-read undated item is in
neither set, so `toBeReadLocal` never carries it back to the phone.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from .test_reader_api import READ_TAG, auth_header, login

pytestmark = pytest.mark.integration

IDS = "/api/greader.php/reader/api/0/stream/items/ids"
EDIT_TAG = "/api/greader.php/reader/api/0/edit-tag"
READING_LIST = "user/-/state/com.google/reading-list"


async def _ids(client: httpx.AsyncClient, token: str, **params: str) -> list[str]:
    response = await client.get(
        IDS, headers=auth_header(token), params={"output": "json", **params}
    )
    assert response.status_code == 200, response.text
    return [ref["id"] for ref in response.json()["itemRefs"]]


@pytest.mark.asyncio
async def test_ot_windows_the_read_stream(reader_client: httpx.AsyncClient) -> None:
    """AC-273, AC-274: ReadYou's `s=reading-list&it=read&ot=<since>`.

    Given three read items -- Second (2026-01-07, newest), First (2026-01-05)
    and Third (no date) -- and `ot` at 2026-01-06
    When the read stream is walked with that window
    Then Second arrives (dated, inside), **and Third arrives** (undated)
    And First does not (dated, older than the bound)

    The bullet that matters is Third. `ot` bounds *dated* history; an undated
    item has no position in that ordering, and dropping it silently left its
    read state unsynchronisable: Read You computes
    `toBeReadLocal = remoteRead ∩ localUnread` from this windowed query, and an
    already-read undated item is in neither `remoteRead` nor `remoteUnread`, so
    the phone never learns the server has it read (issue #285). The feed-level
    path (`syncFeed` -> `getItemIdsForFeed`) sends no `ot` and does serve it,
    which is how the two paths came to disagree.

    First staying out is the other half, and it is unchanged: the window is the
    client's own choice about dated history. Before #92 at all, `ot` was
    ignored and all three came back.
    """
    token = await login(reader_client)
    everything = await _ids(reader_client, token, s=READING_LIST)
    assert len(everything) == 3, everything
    newest, older, undated = everything

    marked = await reader_client.post(
        EDIT_TAG,
        data={"T": token, "i": everything, "a": [READ_TAG]},
    )
    assert marked.status_code == 200, marked.text

    since = int(datetime(2026, 1, 6, tzinfo=UTC).timestamp())
    windowed = await _ids(
        reader_client, token, s=READING_LIST, it=READ_TAG, ot=str(since)
    )
    assert windowed == [newest, undated], (
        "ot=2026-01-06 must serve the dated 2026-01-07 item and the undated "
        f"one, and must not serve the dated 2026-01-05 one ({older}); "
        f"got {windowed}"
    )

    # Without the window the same query serves the whole read set.
    unwindowed = await _ids(reader_client, token, s=READING_LIST, it=READ_TAG)
    assert unwindowed == everything


@pytest.mark.asyncio
async def test_a_non_positive_or_unparseable_ot_disables_the_window(
    reader_client: httpx.AsyncClient,
) -> None:
    """miniflux applies `ot` only when it is a positive integer.

    A client that sends `ot=0` (or a value we cannot read) means "no lower
    bound", not "nothing": filtering to the empty set here would silently
    drop the whole stream. The same discipline as an unrecognised state --
    never widen, never error -- applies in the other direction.
    """
    token = await login(reader_client)
    for ot in ("0", "", "not-a-number"):
        ids = await _ids(reader_client, token, s=READING_LIST, ot=ot)
        assert len(ids) == 3, f"ot={ot!r} must not filter anything, got {ids}"
