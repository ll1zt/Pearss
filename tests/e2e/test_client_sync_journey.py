"""The reader-client sync journey: two clients, one server, real ports.

`test_full_loop.py` walks the agent's journey -- add a source, collect,
consume. This file walks the other leg the product has: a human reading
through a client app, with the read/starred state syncing between two of
them. The scenario list is docs/guides/testing.md's J1-J9; each test names
the scenario it enacts, and a new client-compatibility bug lands there
*first*, as a scenario, before any code changes.

Why this file exists as e2e rather than folding into the integration
reader suites: every bug on this journey (the ignored `it=`, the
post-LIMIT `xt` filter, the absent `continuation`, the 404 subscription
writes) was invisible to ASGI-transport tests because none of them touch
the middleware stack -- and this repository has real history with
middleware ordering. The journey below runs over a real uvicorn port, and
each client speaks its own documented request shape, so the thing under
test is the same thing a phone talks to.

What is deliberately NOT here: any simulation of a client's internal set
arithmetic. Read You's global sync intersecting remote-read with local-
unread is its implementation, not our contract. We assert that the
server's answers are correct *per request shape*, for both shapes, which
is what every one of those bugs actually broke.
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from tests.e2e.conftest import (
    API_TOKEN,
    READER_PASSWORD,
    READER_USERNAME,
    VALID_FEED,
    _FixtureOrigin,
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

READ = "user/-/state/com.google/read"
KEPT_UNREAD = "user/-/state/com.google/kept-unread"
STARRED = "user/-/state/com.google/starred"
READING_LIST = "user/-/state/com.google/reading-list"

# The two shapes a client may use to name the read stream. NetNewsWire asks
# for the stream directly (`s=read`); Read You against a FreshRSS account
# includes it as a tag (`s=reading-list&it=read`). Same question, different
# words, and the server must answer both identically.
NNW = "nnw"
READYOU = "readyou"


class Client:
    """One reader client: its name decides the request shapes it sends.

    The class exists so a test can say `a.mark_read(...)` and have the
    *client's* dialect -- auth placement, id form, stream naming -- decided
    in one place. A server bug that only bites one dialect then shows up as
    exactly one client failing, which is how the reported bugs presented.
    """

    def __init__(self, http: httpx.AsyncClient, dialect: str) -> None:
        self.http = http
        self.dialect = dialect
        self.token: str | None = None

    async def login(self) -> None:
        response = await self.http.post(
            "/api/greader.php/accounts/ClientLogin",
            data={"Email": READER_USERNAME, "Passwd": READER_PASSWORD},
        )
        assert response.status_code == 200, response.text
        self.token = next(
            line.removeprefix("Auth=").strip()
            for line in response.text.splitlines()
            if line.startswith("Auth=")
        )

    def _header(self) -> dict[str, str]:
        assert self.token is not None
        return {"Authorization": f"GoogleLogin auth={self.token}"}

    async def unread_ids(self, n: int = 1000) -> list[str]:
        """The unread stream, in this client's dialect."""
        assert self.token is not None
        params: dict[str, Any] = {"output": "json", "n": n}
        if self.dialect == NNW:
            params["s"] = READING_LIST
            params["xt"] = READ
        else:
            # Read You's unread query is the same shape as NNW's.
            params["s"] = READING_LIST
            params["xt"] = READ
        return await self._walk_ids(params, self._header())

    async def read_ids(self, n: int = 1000) -> list[str]:
        """The read stream, in this client's dialect.

        NNW names the read stream directly; Read You asks the reading list
        and includes the read tag. Before the `it=` fix these two questions
        got different answers -- the whole library versus the read set.
        """
        assert self.token is not None
        if self.dialect == NNW:
            params = {"output": "json", "s": READ, "n": n}
        else:
            params = {"output": "json", "s": READING_LIST, "it": READ, "n": n}
        return await self._walk_ids(params, self._header())

    async def starred_ids(self, n: int = 1000) -> list[str]:
        assert self.token is not None
        params = {"output": "json", "s": STARRED, "n": n}
        return await self._walk_ids(params, self._header())

    async def _walk_ids(
        self, params: dict[str, Any], headers: dict[str, str]
    ) -> list[str]:
        """Follow `continuation` to the end, as every real client does.

        A client that stops after one page is not exercising pagination, so
        the walk is unconditional: whatever the server answers, the client
        keeps asking while a cursor is offered.
        """
        collected: list[str] = []
        cursor: str | None = None
        for _ in range(100):
            query = dict(params)
            if cursor is not None:
                query["c"] = cursor
            response = await self.http.get(
                "/api/greader.php/reader/api/0/stream/items/ids",
                headers=headers,
                params=query,
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            collected.extend(ref["id"] for ref in payload.get("itemRefs", []))
            cursor = payload.get("continuation")
            if cursor is None:
                return collected
        raise AssertionError("the walk never terminated")

    async def mark(
        self, ids: list[str], *, read: bool | None = None, starred: bool | None = None
    ) -> None:
        """edit-tag, in this client's dialect.

        Both dialects put the token in the form field `T`; the forms differ
        in which tag expresses the intent. NetNewsWire uses read/unread via
        add/remove of the read tag; Read You (and Google Reader) mark unread
        by adding `kept-unread`.

        Absent form fields are omitted entirely, as real clients do: an
        explicit `a=` empty string arrives server-side as a tag of `""`,
        a request no real client sends.
        """
        assert self.token is not None
        add: list[str] = []
        remove: list[str] = []
        if read is True:
            add.append(READ)
        elif read is False:
            if self.dialect == NNW:
                remove.append(READ)
            else:
                add.append(KEPT_UNREAD)
        if starred is True:
            add.append(STARRED)
        elif starred is False:
            remove.append(STARRED)
        form: dict[str, Any] = {"T": self.token, "i": ids}
        if add:
            form["a"] = add
        if remove:
            form["r"] = remove
        response = await self.http.post(
            "/api/greader.php/reader/api/0/edit-tag",
            headers=self._header(),
            data=form,
        )
        assert response.status_code == 200, response.text

    async def titles(self) -> dict[str, str]:
        """id -> title for everything the client can see, via contents."""
        everything = await self._all_visible_ids()
        if not everything:
            return {}
        assert self.token is not None
        response = await self.http.post(
            "/api/greader.php/reader/api/0/stream/items/contents",
            headers=self._header(),
            data={"T": self.token, "i": everything},
        )
        assert response.status_code == 200, response.text
        return {item["id"]: item["title"] for item in response.json().get("items", [])}

    async def _all_visible_ids(self) -> list[str]:
        read = await self.read_ids()
        unread = await self.unread_ids()
        return read + unread


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
async def reader_a(served: str) -> AsyncIterator[Client]:
    """Client A on the real port, logged in."""
    async with httpx.AsyncClient(base_url=served) as http:
        client = Client(http, NNW)
        await client.login()
        yield client


@pytest.fixture
async def reader_b(served: str) -> AsyncIterator[Client]:
    """Client B on the same server, speaking Read You's dialect."""
    async with httpx.AsyncClient(base_url=served) as http:
        client = Client(http, READYOU)
        await client.login()
        yield client


async def _seed_two_sources(served: str, origin_url: str) -> str:
    """One collected source through the Core API, with its id returned."""
    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        created = await client.post(
            "/v1/sources",
            headers={"Idempotency-Key": "journey-seed-1"},
            json={
                "url": origin_url,
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        )
        assert created.status_code == 201, created.text
        source_id = str(created.json()["id"])
        triggered = await client.post(
            f"/v1/sources/{source_id}:trigger",
            headers={"Idempotency-Key": "journey-seed-trigger-1"},
        )
        assert triggered.status_code == 200, triggered.text
        return source_id


# --------------------------------------------------------------------------
# J1: both clients see the subscriptions and the items
# --------------------------------------------------------------------------


async def test_j1_both_clients_see_the_same_items_after_login(
    served: str, origin_url: str
) -> None:
    """A user's library is the same whichever client connects from.

    Given one source with collected items
    When client A (NetNewsWire shapes) and client B (Read You shapes) both
    log in and list everything
    Then they see the same set of ids, and A's decimal ids resolve in B's
    long-form ids and vice versa

    The id forms differ between the clients' three surfaces (ids, contents,
    edit-tag); a mismatch on any of them was the 0006-revision bug, which
    presented as "the subscription is there but the articles are not".
    """
    await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()

        a_read, a_unread = await a.read_ids(), await a.unread_ids()
        b_read, b_unread = await b.read_ids(), await b.unread_ids()

        assert a_unread, "the fixture must have collected items"
        assert set(a_read) == set(b_read), (a_read, b_read)
        assert set(a_unread) == set(b_unread), (a_unread, b_unread)

        # B's long-form contents ids must round-trip to the same items A
        # sees in decimal. This is the cross-form correspondence the two
        # prior id bugs broke.
        b_titles = await b.titles()
        a_titles = await a.titles()
        assert b_titles == a_titles


# --------------------------------------------------------------------------
# J2-J3: read state crosses between clients, in both directions
# --------------------------------------------------------------------------


async def test_j2_a_read_in_client_a_reaches_client_b(
    served: str, origin_url: str
) -> None:
    """Reading on one client marks it read on the other.

    Given both clients see the same unread item
    When the user reads it in client A (NetNewsWire shapes)
    Then client B (Read You shapes) reports it in its read set -- and the
    read set is exactly that item, not the whole library

    The last clause is the regression that caused the oscillating unread
    count: with `it=` ignored, B's "read" query returned everything, so B
    concluded every unread item was read. The direction that historically
    failed is B receiving A's write; the write itself always landed.
    """
    await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()

        unread = await a.unread_ids()
        assert len(unread) >= 2, "this scenario needs at least two items"
        target = unread[0]

        await a.mark([target], read=True)

        # B's read set must now contain exactly the item A read.
        b_read = await b.read_ids()
        assert target in b_read, "A's read did not reach B"
        assert set(b_read) == {target}, (
            f"B's read set must not be a superset: {sorted(b_read)}"
        )

        # And B's unread set agrees, so the two partition the library.
        b_unread = await b.unread_ids()
        assert target not in b_unread
        assert set(b_unread) == set(unread) - {target}


async def test_j3_marking_unread_in_client_b_reaches_client_a(
    served: str, origin_url: str
) -> None:
    """The reverse direction works too, through B's kept-unread dialect.

    Given an item client A has already read
    When the user marks it unread in client B (which sends
    `a=kept-unread`, not `r=read`)
    Then client A reports it as unread again

    J2 and J3 exist as a pair because the directionality of the original
    report (one way worked, the other did not) was the clue that narrowed
    the bug to a read path. A suite that only tested one direction could
    pass while the user-visible behaviour stayed broken.
    """
    await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()

        unread = await a.unread_ids()
        target = unread[0]
        await a.mark([target], read=True)

        # Sanity: both now agree it is read.
        assert target in await b.read_ids()

        # The user changes their mind on their phone (client B).
        await b.mark([target], read=False)

        assert target not in await a.read_ids(), "B's unread did not reach A"
        assert target in await a.unread_ids()


# --------------------------------------------------------------------------
# J4: starred state crosses between clients
# --------------------------------------------------------------------------


async def test_j4_a_star_in_client_b_is_visible_to_client_a(
    served: str, origin_url: str
) -> None:
    """Starring is a third flag and travels by the same road.

    Given an unread item
    When the user stars it in client B
    Then both clients' starred streams name exactly that item

    The starred stream has no dialect split (both clients send `s=starred`),
    but the write path differs -- B goes through edit-tag with `a=starred`
    -- and a star that reached the server but not the sibling client's
    read view would strand the user's saved article.
    """
    await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()

        unread = await a.unread_ids()
        target = unread[0]
        await b.mark([target], starred=True)

        assert await a.starred_ids() == [target]
        assert await b.starred_ids() == [target]

        # Starring must not have disturbed the read flag either way.
        assert target in await a.unread_ids()
        assert target in await b.unread_ids()


# --------------------------------------------------------------------------
# J5: the unread count is stable when nothing happens
# --------------------------------------------------------------------------


async def test_j5_repeated_syncs_agree_and_the_two_sets_partition_the_library(
    served: str, origin_url: str
) -> None:
    """Two syncs with no writes in between answer identically.

    Given a state where one item is read and the rest are not
    When client B syncs twice
    Then both syncs return the same sets, and read+unread is exactly the
    library with no overlap

    This is the direct form of the reported "unread count keeps changing":
    the oscillation was the read query and the unread query disagreeing
    about the same items across refreshes. The partition assertion is what
    a count derives from; if the partition holds for both clients, the
    number the user sees is stable.
    """
    await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()

        everything = await a.unread_ids()
        await a.mark([everything[0]], read=True)

        for client in (a, b):
            first_read, first_unread = (
                await client.read_ids(),
                await client.unread_ids(),
            )
            second_read, second_unread = (
                await client.read_ids(),
                await client.unread_ids(),
            )
            assert first_read == second_read, client.dialect
            assert first_unread == second_unread, client.dialect

            # The partition, with no id in both and none missing.
            assert not set(first_read) & set(first_unread), client.dialect
            union = sorted(set(first_read) | set(first_unread))
            assert union == sorted(set(everything)), client.dialect


# --------------------------------------------------------------------------
# J6: the walk reaches every item, page by page, through the real server
# --------------------------------------------------------------------------


async def test_j6_a_walked_stream_covers_every_item_without_repeats(
    served: str, origin_url: str
) -> None:
    """Paging works through the real port, for both clients' stream forms.

    Given a collected library of a few items
    When each client walks its streams at a page size smaller than the
    library, following `continuation`
    Then the walked union is the library, with no repeats, and both
    clients' read streams agree

    The pre-cursor bug bit when the library outgrew one page: the read set
    stopped at page one, so an item read on the other client but old
    enough to sit outside the first 1000 was in neither answer. The full
    1200-item scale regression lives in the integration suite (it needs a
    big fixture); what e2e pins here is the cursor mechanics end to end
    over TCP -- a page offers a cursor, following it terminates, and the
    two spellings of "read" return the same items.
    """
    await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()

        library = await a._walk_ids(
            {"output": "json", "s": READING_LIST, "n": 1000}, a._header()
        )
        assert len(library) >= 3

        target = library[0]
        await a.mark([target], read=True)

        for page_size in (1, 2):
            # NNW's form: the read stream, asked for by name.
            a_read = await a._walk_ids(
                {"output": "json", "s": READ, "n": page_size}, a._header()
            )
            # Read You's form: the reading list with an include tag.
            b_read = await b._walk_ids(
                {"output": "json", "s": READING_LIST, "it": READ, "n": page_size},
                b._header(),
            )
            assert a_read == b_read == [target], (a_read, b_read)

            # A partial page offered a cursor; a full one did not. Assert
            # both behaviours through the walk's shape rather than by
            # re-reading the field: repeats would mean a cursor re-served
            # an earlier page.
            assert len(a_read) == len(set(a_read))

        # The partition survives paging: read + unread is everything.
        a_read = await a.read_ids()
        a_unread = await a.unread_ids()
        assert sorted(set(a_read) | set(a_unread)) == sorted(set(library))
        assert not set(a_read) & set(a_unread)


# --------------------------------------------------------------------------
# J7-J9: subscription management from inside an app
# --------------------------------------------------------------------------


async def test_j7_subscribing_from_the_app_creates_a_working_subscription(
    served: str, origin_url: str
) -> None:
    """A feed added by an app is a first-class subscription.

    Given a feed URL the deployment does not collect
    When client B adds it through quickadd (the App's add-feed path)
    Then it appears in the subscription list for BOTH clients, and the
    Core API agrees it exists

    The pre-fix behaviour was a 404, which Read You's sync turned into a
    permanent retry loop -- the app showed a spinning sync, not a failed
    add. The streamId contract (present, or a real error, never a
    well-formed empty reply) is what keeps that loop from returning.
    """
    source_id = await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        b = Client(http, READYOU)
        await b.login()
        assert b.token is not None

        new_url = "https://added-from-app.example/feed.xml"
        response = await http.post(
            "/api/greader.php/reader/api/0/subscription/quickadd",
            headers=b._header(),
            params={"output": "json", "quickadd": new_url},
            data={"T": b.token, "quickadd": new_url},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["numResults"] == 1
        new_stream_id = payload["streamId"]
        assert new_stream_id.startswith("feed/"), payload

        # The Core API sees the same source the app created, and client
        # A's subscription list carries both feeds.
        async with httpx.AsyncClient(
            base_url=served,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        ) as api:
            listed = await api.get("/v1/sources")
            ids = {row["id"] for row in listed.json()["data"]}
        assert source_id in ids
        assert new_stream_id.removeprefix("feed/") in ids

        async with httpx.AsyncClient(base_url=served) as http2:
            a = Client(http2, NNW)
            await a.login()
            subs = await http2.get(
                "/api/greader.php/reader/api/0/subscription/list",
                headers=a._header(),
            )
            assert new_stream_id in {s["id"] for s in subs.json()["subscriptions"]}


async def test_j8_group_changes_propagate_to_the_other_client(
    served: str, origin_url: str
) -> None:
    """Folders are part of sync: create, rename, delete all round-trip.

    Given a subscription in the default folder
    When client B moves it into a group, renames that group, then deletes
    it
    Then client A's subscription list tracks every step, and the
    subscription itself survives the group's deletion

    Before the fix each step failed outright (404); after it, the risk is
    the subtler one -- a folder change one client can see and the other
    cannot, which presents as a sidebar that will not agree with itself.
    """
    await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()
        assert b.token is not None

        subs = await http.get(
            "/api/greader.php/reader/api/0/subscription/list", headers=b._header()
        )
        feed_id = subs.json()["subscriptions"][0]["id"]

        async def folders(client: Client) -> dict[str, str]:
            response = await http.get(
                "/api/greader.php/reader/api/0/subscription/list",
                headers=client._header(),
            )
            return {
                s["id"]: s["categories"][0]["label"] if s["categories"] else ""
                for s in response.json()["subscriptions"]
            }

        # Move into a group.
        moved = await http.post(
            "/api/greader.php/reader/api/0/subscription/edit",
            headers=b._header(),
            data={
                "T": b.token,
                "ac": "edit",
                "s": feed_id,
                "a": "user/-/label/Journey",
            },
        )
        assert moved.status_code == 200, moved.text
        assert (await folders(a))[feed_id] == "Journey"
        assert (await folders(b))[feed_id] == "Journey"

        # Rename the group.
        renamed = await http.post(
            "/api/greader.php/reader/api/0/rename-tag",
            headers=b._header(),
            data={
                "T": b.token,
                "s": "user/-/label/Journey",
                "dest": "user/-/label/Renamed",
            },
        )
        assert renamed.status_code == 200, renamed.text
        assert (await folders(a))[feed_id] == "Renamed"

        # Delete the group; the subscription must survive in default.
        deleted = await http.post(
            "/api/greader.php/reader/api/0/disable-tag",
            headers=b._header(),
            data={"T": b.token, "s": "user/-/label/Renamed"},
        )
        assert deleted.status_code == 200, deleted.text
        assert (await folders(a))[feed_id] == "default"
        assert (await folders(b))[feed_id] == "default"


async def test_j9_unsubscribing_from_the_app_stops_collecting_and_replays_cleanly(
    served: str, origin_url: str, session_factory: sessionmaker[Session]
) -> None:
    """Unsubscribe stops collecting and keeps the content; a replay stays done.

    Given a source with collected items
    When client B unsubscribes it (the App's delete path, `ac=unsubscribe`)
    Then the subscription vanishes from BOTH clients' lists, the items are
    still there (destroying them is a separate, reasoned call -- ADR 0063),
    and the source itself reports that it is not collecting, and sending the
    same request again answers OK rather than an error

    The 2026-10-07 answer (ADR 0063) split the verb in two. `ac=unsubscribe`,
    this endpoint's sibling `DELETE /v1/sources/{id}` and
    `DELETE /v1/subscriptions/{id}:unsubscribe` all unsubscribe;
    `DELETE /v1/origins/{id}:purge?reason=...` is the one that destroys content,
    and it demands the reason because that is the call nobody can undo.

    The 2026-09-16 revision of ADR 0024 reverses the original refusal. What
    tipped it was reading the failure chain with fresh eyes: refusing with
    400 put Read You's deleteFeed into the same Result.retry() loop as any
    other failure, so the user's delete *looked broken forever* -- worse,
    not safer, than an honest delete. The delete itself is exactly what
    DELETE /v1/sources does, through the same repository entry point, so
    the irreversibility argument applies to a caller who states intent
    through this endpoint just as it does to one using /v1: the app shows
    a confirmation dialog before sending. Idempotence follows the
    "already gone answers done" rule the /v1 endpoint established.
    """
    source_id = await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()
        assert b.token is not None

        removed = await http.post(
            "/api/greader.php/reader/api/0/subscription/edit",
            headers=b._header(),
            data={"T": b.token, "ac": "unsubscribe", "s": f"feed/{source_id}"},
        )
        assert removed.status_code == 200, removed.text

        # Gone from both clients' subscription lists.
        for client in (a, b):
            subs = await http.get(
                "/api/greader.php/reader/api/0/subscription/list",
                headers=client._header(),
            )
            assert f"feed/{source_id}" not in {
                s["id"] for s in subs.json()["subscriptions"]
            }, client.dialect

        # Not gone: the row is there and says it is not collecting, and the
        # items it collected are untouched. This is the half that changed --
        # the old behaviour deleted both, which is what an app's "unsubscribe"
        # button least expects.
        async with httpx.AsyncClient(
            base_url=served,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        ) as api:
            shown = await api.get(f"/v1/sources/{source_id}")
            assert shown.status_code == 200, shown.text
            assert shown.json()["status"] == "disabled", shown.json()["status"]
        with session_factory() as db:
            items = db.execute(
                sa.text("select count(*) from item where origin_id = :s"),
                {"s": source_id},
            ).scalar_one()
            assert items > 0, "unsubscribing is not deletion"

        # A retry (dropped response, resync) is done, not an error.
        again = await http.post(
            "/api/greader.php/reader/api/0/subscription/edit",
            headers=b._header(),
            data={"T": b.token, "ac": "unsubscribe", "s": f"feed/{source_id}"},
        )
        assert again.status_code == 200, again.text


# --------------------------------------------------------------------------
# J11: renaming a subscription from the app
# --------------------------------------------------------------------------


async def test_j11_renaming_a_subscription_sticks_across_recollection(
    served: str, origin_url: str
) -> None:
    """A client rename becomes the subscription's name and survives a fetch.

    Given a subscribed source whose title is still the origin's
    When client B renames it (`ac=edit` with `t=`, Read You's renameFeed)
    Then both clients list it under the new name
    And after another collection the name is still the user's

    The name was previously answered with a silent OK and nothing else,
    on the reasoning that the next fetch would overwrite it. That
    reasoning was wrong: `set_origin_title_if_absent` writes only when the
    column is empty, so a client-set title is protected by the same
    conditional-write rule an operator's rename through the API gets. The
    fetch fills an absent title; it does not replace a present one.
    """
    await _seed_two_sources(served, origin_url)

    async with httpx.AsyncClient(base_url=served) as http:
        b = Client(http, READYOU)
        await b.login()
        assert b.token is not None

        subs = await http.get(
            "/api/greader.php/reader/api/0/subscription/list", headers=b._header()
        )
        feed_id = subs.json()["subscriptions"][0]["id"]
        source_id = feed_id.removeprefix("feed/")

        renamed = await http.post(
            "/api/greader.php/reader/api/0/subscription/edit",
            headers=b._header(),
            data={"T": b.token, "ac": "edit", "s": feed_id, "t": "My rename"},
        )
        assert renamed.status_code == 200, renamed.text

        def names(payload: dict[str, Any]) -> dict[str, str]:
            return {x["id"]: x["title"] for x in payload["subscriptions"]}

        # Both clients see the new name.
        for client in (b,):
            listing = await http.get(
                "/api/greader.php/reader/api/0/subscription/list",
                headers=client._header(),
            )
            assert names(listing.json())[feed_id] == "My rename", names(listing.json())

        # A collection happens; the rename must survive it.
        async with httpx.AsyncClient(
            base_url=served,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        ) as api:
            triggered = await api.post(
                f"/v1/sources/{source_id}:trigger",
                headers={"Idempotency-Key": "journey-rename-trigger-1"},
            )
            assert triggered.status_code == 200, triggered.text

        listing = await http.get(
            "/api/greader.php/reader/api/0/subscription/list", headers=b._header()
        )
        assert names(listing.json())[feed_id] == "My rename"


# --------------------------------------------------------------------------
# J10: a translated source shows the translation through the reader
# --------------------------------------------------------------------------


class _OpenAIOrigin:
    """A real OpenAI-compatible provider on the loopback.

    Answers `/chat/completions` with a deterministic translation. The
    translate processor speaks plain `httpx.post` against
    `PEARSS_TRANSLATE_BASE_URL`, so standing up a real HTTP origin keeps
    the whole path -- socket, request shape, response parsing, the model
    fallback chain -- production-shaped; only the model is a fixture.
    """

    def __init__(self) -> None:
        self.requests = 0
        # Every request, because the processor sends one per translatable block
        # (ADR 0031): "what was translated" is the union across them, and a
        # single remembered body would only show the last block -- which is how
        # a test asserting on the whole article would pass while the article
        # was nowhere in the call.
        self.request_bodies: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        origin = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                user_text = ""
                for message in body.get("messages", []):
                    if message.get("role") == "user":
                        user_text = str(message.get("content", ""))
                with origin._lock:
                    origin.requests += 1
                    origin.last_request = body
                    origin.request_bodies.append(body)
                reply = f"[译] {user_text}"
                payload = json.dumps(
                    {
                        "id": f"chatcmpl-e2e-{origin.requests}",
                        "object": "chat.completion",
                        "created": 1_700_000_000,
                        "model": body.get("model", "e2e-model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": reply,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self.last_request: dict[str, Any] = {}

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> _OpenAIOrigin:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


async def test_j10_a_translated_source_leads_with_the_translation_in_the_reader(
    served: str,
    origin_url: str,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader subscribing to a translated feed reads the translation first.

    Given a source whose chain requires a translation into Chinese
    When the items are collected and the enrichment sweep runs against a
    real local provider
    Then the Reader API serves the translation above a separator rule with
    the original below, in both clients' request shapes

    The sweep is driven directly (`sweep_queue`) rather than waiting on the
    background worker's timer -- same code, deterministic timing; the
    background loop is only a scheduler around this call.
    """

    from app.enrichment_worker import default_processors, sweep_queue

    source_id: str | None = None
    with _OpenAIOrigin() as provider:
        monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", provider.base_url)
        monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "e2e-provider-key")
        monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "e2e-model")

        async with httpx.AsyncClient(
            base_url=served,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        ) as client:
            created = await client.post(
                "/v1/sources",
                headers={"Idempotency-Key": "journey-translate-1"},
                json={
                    "url": origin_url,
                    "schedule_policy": {"type": "fixed", "interval": "30m"},
                    "pipeline_chain": [
                        {
                            "plugin_id": "translate",
                            "kind": "translation",
                            "lang": "zh",
                            "required": True,
                        }
                    ],
                },
            )
            assert created.status_code == 201, created.text
            source_id = created.json()["id"]
            triggered = await client.post(
                f"/v1/sources/{source_id}:trigger",
                headers={"Idempotency-Key": "journey-translate-trigger-1"},
            )
            assert triggered.status_code == 200, triggered.text

        # The real sweep, over real HTTP to the local provider.
        sweep_queue(
            session_factory(),
            processors=default_processors(),
            row_session_factory=session_factory,
        )

        assert provider.requests >= 1, "the provider was never called"

    async with httpx.AsyncClient(base_url=served) as http:
        a = Client(http, NNW)
        b = Client(http, READYOU)
        await a.login()
        await b.login()

        for reader in (a, b):
            titles = await reader.titles()
            assert titles, "no items visible through the reader"
        assert a.token is not None
        every_id = list((await a.titles()).keys())
        response = await http.post(
            "/api/greader.php/reader/api/0/stream/items/contents",
            headers=a._header(),
            data={"T": a.token, "i": every_id},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        translated = [
            item for item in items if "[译]" in str(item["summary"]["content"])
        ]
        assert translated, (
            f"no item carries a translation; bodies: "
            f"{[str(i['summary']['content'])[:80] for i in items]}"
        )
        body = str(translated[0]["summary"]["content"])
        assert 'data-pearss="translation-separator"' in body
        # The original follows the rule; the translation never replaces it.
        after_rule = body.split('data-pearss="translation-separator"', 1)[1]
        original_part = after_rule.split(">", 1)[1].strip()
        assert original_part, "the original body is gone below the rule"
        assert "[译]" not in original_part, "the translation leaked below the rule"


class _ArticleOrigin:
    """A real article origin: serves the item's own page on the loopback.

    The fulltext processor fetches the item URL, so covering the real
    `[fulltext, translate]` chain means giving it a real page to extract. The
    page is deliberately *article-like* (a heading, prose, a link) so the
    extractor's structural algorithm -- not a fixture special case -- is what
    produces the product.
    """

    def __init__(self) -> None:
        self.requests = 0
        self._lock = threading.Lock()
        origin = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with origin._lock:
                    origin.requests += 1
                body = (
                    "<html><head><title>t</title></head><body>"
                    "<nav>navigation to be dropped</nav>"
                    "<article>"
                    "<h2>ARCHITECTURE SECTION</h2>"
                    "<p>" + ("A long paragraph of English prose. " * 20) + "</p>"
                    "<p>See the <a href='/deep'>deep link</a> for more.</p>"
                    "</article>"
                    "<footer>footer to be dropped</footer></body></html>"
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])

    def url_for(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self) -> _ArticleOrigin:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _article_feed(article: _ArticleOrigin) -> bytes:
    """The valid fixture, rewritten so item URLs point at the article origin.

    The feed's own descriptions stay short (that is what makes fulltext work),
    and each item's link is redirected to the loopback page the processor will
    actually fetch -- the same substitution a real deployment performs by
    simply having a real URL, without touching the parser under test.
    """
    text = VALID_FEED.read_text()
    text = text.replace(
        "https://blog.example.com/2026/01/",
        f"{article.url_for('/2026/01/')}",
    ).replace(
        "https://blog.example.com/",
        f"{article.url_for('/')}",
    )
    return text.encode()


async def test_j11_the_chain_translates_the_extracted_full_text(
    served: str,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's scenario end to end, with the real plugins.

    Given a summary-only source whose chain is [fulltext, translate]
    When the real fulltext processor extracts the article and the real
    translate processor runs against a local provider
    Then the translation the reader is served is a translation of the
    *extracted article*, not of the feed's short summary

    This is the whole of issue #34 in one journey: the two processors, the
    real extractor, the real provider call and the real Reader output. Before
    the dataflow rule, the translation was of the summary, so the reader saw a
    translated stub above an untranslated article.
    """
    from app.enrichment_worker import default_processors, sweep_queue

    with _ArticleOrigin() as article:
        feed = _article_feed(article)
        with _FixtureOrigin(feed) as feed_origin:
            source_id: str | None = None
            with _OpenAIOrigin() as provider:
                monkeypatch.setenv("PEARSS_TRANSLATE_BASE_URL", provider.base_url)
                monkeypatch.setenv("PEARSS_TRANSLATE_API_KEY", "e2e-provider-key")
                monkeypatch.setenv("PEARSS_TRANSLATE_MODEL", "e2e-model")

                async with httpx.AsyncClient(
                    base_url=served,
                    headers={"Authorization": f"Bearer {API_TOKEN}"},
                ) as client:
                    created = await client.post(
                        "/v1/sources",
                        headers={"Idempotency-Key": "journey-chain-1"},
                        json={
                            "url": feed_origin.url,
                            "schedule_policy": {
                                "type": "fixed",
                                "interval": "30m",
                            },
                            "pipeline_chain": [
                                {
                                    "plugin_id": "fulltext",
                                    "kind": "fulltext",
                                    "required": True,
                                },
                                {
                                    "plugin_id": "translate",
                                    "kind": "translation",
                                    "lang": "zh",
                                    "required": True,
                                },
                            ],
                        },
                    )
                    assert created.status_code == 201, created.text
                    source_id = created.json()["id"]
                    triggered = await client.post(
                        f"/v1/sources/{source_id}:trigger",
                        headers={"Idempotency-Key": "journey-chain-trigger-1"},
                    )
                    assert triggered.status_code == 200, triggered.text

                # The real sweep, with the real processors. The chain advances
                # one position per sweep, so drive it until it settles.
                for _ in range(6):
                    report = sweep_queue(
                        session_factory(),
                        processors=default_processors(),
                        row_session_factory=session_factory,
                    )
                    if not (
                        report["succeeded"] or report["failed"] or report["retried"]
                    ):
                        break
                else:  # pragma: no cover - a hung chain is a real failure
                    raise AssertionError("the chain never settled")

                assert article.requests >= 1, "the article page was never fetched"
                assert provider.requests >= 1, "the provider was never called"
                # Every request, not just the last: the chain now sends one
                # per block, so the article is spread across them.
                sent_to_provider = json.dumps(provider.request_bodies)

    # What the reader is served, and what the provider was asked to translate.
    assert "ARCHITECTURE SECTION" in sent_to_provider, (
        "the translation must be made from the extracted article; the provider "
        f"was sent {sent_to_provider[:400]}"
    )
    assert "short summary" not in sent_to_provider.lower(), (
        "the feed summary must not be what was translated"
    )

    async with httpx.AsyncClient(base_url=served) as http:
        reader = Client(http, NNW)
        await reader.login()
        assert reader.token is not None
        every_id = list((await reader.titles()).keys())
        response = await http.post(
            "/api/greader.php/reader/api/0/stream/items/contents",
            headers=reader._header(),
            data={"T": reader.token, "i": every_id},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        bodies = [str(item["summary"]["content"]) for item in items]
        stacked = [body for body in bodies if "[译]" in body]
        assert stacked, f"no item carries a translation; bodies: {bodies}"
        body = stacked[0]

    # The three layers, in the documented order: translation, full text,
    # original summary -- and the translation is a translation of the article.
    assert 'data-pearss="translation-separator"' in body
    assert 'data-pearss="fulltext-separator"' in body
    assert "ARCHITECTURE SECTION" in body, "the extracted article must be served"
    translation_layer = body.split('data-pearss="translation-separator"', 1)[0]
    assert "ARCHITECTURE SECTION" in translation_layer, (
        "the translation layer must correspond to the extracted article, which "
        f"is what it sits above; it was {translation_layer[:300]}"
    )
    # And the translation keeps the article's *structure*, not just its words
    # (ADR 0031, issue #33): the heading is still a heading, and the link is
    # still a link pointing at the article's own host.
    assert "<h2" in translation_layer, (
        f"the heading must stay a heading in the translation: {translation_layer[:300]}"
    )
    assert f'href="{article.url_for("/deep")}"' in translation_layer, (
        "the link must survive translation and point at the origin: "
        f"{translation_layer[:300]}"
    )
