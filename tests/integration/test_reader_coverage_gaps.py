"""Reader API coverage gaps: the dispositions issue #92 had to settle.

The point-in-time coverage ledger (`docs/integrations/google-reader-api-coverage.md`)
was retired, and it left items whose disposition was "undecided". This file is
the executable half of the ones that are declined; the prose half is the ADR
0024 revision. (`ot`, the one item with a caller, is fixed and pinned in
`test_reader_ot_window.py`.)

The disposition rule is the issue's own: answer **which client breaks**, not
"what does the protocol lack". The client is ReadYou, reconciled at
`ReadYouApp/ReadYou@d2b979c` (the same revision ADR 0024 pinned). Reading it:

  `ot`  sent on every global sync -> IMPLEMENTED (see the ot-window suite).
  `nt`  never sent anywhere -> declined.
  `r`   only ever the *remove* field of edit-tag / subscription-edit, never a
        sort order -> declined (we keep newest-first).
  `ac=subscribe`  never sent; `subscribe()` uses quickadd -> declined (400).
  unknown endpoints  never probed; non-2xx is an exception and a retry, with
        no "200 + empty means unsupported" branch -> declined (404 stays).
  `quickadd` numResults  in the DTO but never read; only `streamId` is
        `requireNotNull`ed -> declined (400 stays).
  `rename-tag` missing label  no 404 dependency -> declined (200 no-op stays).

Each declined item has a test below that pins today's behaviour, so a silent
change is caught and the ADR entry has to be revisited.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from .test_reader_api import auth_header, login

pytestmark = pytest.mark.integration

IDS = "/api/greader.php/reader/api/0/stream/items/ids"
SUBSCRIPTIONS = "/api/greader.php/reader/api/0/subscription/list"
SUBSCRIPTION_EDIT = "/api/greader.php/reader/api/0/subscription/edit"
QUICKADD = "/api/greader.php/reader/api/0/subscription/quickadd"
RENAME_TAG = "/api/greader.php/reader/api/0/rename-tag"
READING_LIST = "user/-/state/com.google/reading-list"


async def _ids(client: httpx.AsyncClient, token: str, **params: str) -> list[str]:
    response = await client.get(
        IDS, headers=auth_header(token), params={"output": "json", **params}
    )
    assert response.status_code == 200, response.text
    return [ref["id"] for ref in response.json()["itemRefs"]]


async def _subscription_ids(client: httpx.AsyncClient, token: str) -> set[str]:
    response = await client.get(SUBSCRIPTIONS, headers=auth_header(token))
    assert response.status_code == 200, response.text
    return {sub["id"] for sub in response.json()["subscriptions"]}


@pytest.mark.asyncio
async def test_nt_is_ignored(reader_client: httpx.AsyncClient) -> None:
    """Declined: no client sends `nt` (ReadYou@d2b979c never adds it).

    If a client ever does, this test fails and forces the ADR 0024 entry to be
    revisited rather than the parameter being half-supported in silence.
    """
    token = await login(reader_client)
    far_future = int(datetime(2030, 1, 1, tzinfo=UTC).timestamp())
    ids = await _ids(reader_client, token, s=READING_LIST, nt=str(far_future))
    assert len(ids) == 3, ids


@pytest.mark.asyncio
async def test_r_o_does_not_change_the_order(reader_client: httpx.AsyncClient) -> None:
    """Declined: ReadYou never sends a sort order.

    `r` appears in ReadYou only as the *remove* field of `edit-tag` and
    `subscription/edit`; a sort order is a different parameter that shares the
    letter. We always serve newest-first, and this pins that `r=o` is inert.
    """
    token = await login(reader_client)
    default = await _ids(reader_client, token, s=READING_LIST)
    ascending = await _ids(reader_client, token, s=READING_LIST, r="o")
    assert ascending == default
    assert len(default) == 3


@pytest.mark.asyncio
async def test_ac_subscribe_is_refused_and_creates_nothing(
    reader_client: httpx.AsyncClient,
) -> None:
    """Declined: ReadYou subscribes through `quickadd`, not `ac=subscribe`.

    The refusal is asserted *and* the absence of a side effect: a 400 that
    still created a source would be worse than an accepted request, because
    the client would retry it forever.
    """
    token = await login(reader_client)
    before = await _subscription_ids(reader_client, token)

    response = await reader_client.post(
        SUBSCRIPTION_EDIT,
        headers=auth_header(token),
        data={"T": token, "ac": "subscribe", "s": "feed/https://new.example.com/feed"},
    )

    assert response.status_code == 400, response.text
    assert await _subscription_ids(reader_client, token) == before


@pytest.mark.asyncio
async def test_an_unknown_reader_endpoint_is_404(
    reader_client: httpx.AsyncClient,
) -> None:
    """Declined: ReadYou never probes an endpoint it does not know.

    Its client throws on any non-2xx and retries; there is no branch that reads
    "200 + empty array" as "unsupported". So miniflux's catch-all `[]` buys us
    nothing and our 404 stays -- the honest answer for a path that does not
    exist.
    """
    token = await login(reader_client)
    response = await reader_client.get(
        "/api/greader.php/reader/api/0/does-not-exist",
        headers=auth_header(token),
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_quickadd_with_an_invalid_url_is_400(
    reader_client: httpx.AsyncClient,
) -> None:
    """Declined: ReadYou reads `streamId` (requireNotNull), never `numResults`.

    miniflux answers `{"numResults": 0}` with 200; we answer 400. The client
    cannot tell the difference -- it never inspects `numResults`, and a 400
    fails the same way a zero result would.
    """
    token = await login(reader_client)
    response = await reader_client.post(
        QUICKADD,
        headers=auth_header(token),
        data={"T": token, "quickadd": "not-a-url"},
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_rename_tag_on_a_missing_folder_is_ok(
    reader_client: httpx.AsyncClient,
) -> None:
    """Declined: ReadYou's `renameTag` has no 404 dependency.

    miniflux 404s a rename whose source label does not exist; we no-op and
    answer OK, which is the idempotent reading of "make it so". No client
    branches on the difference.
    """
    token = await login(reader_client)
    response = await reader_client.post(
        RENAME_TAG,
        headers=auth_header(token),
        data={
            "T": token,
            "s": "user/-/label/Nonexistent",
            "dest": "user/-/label/Renamed",
        },
    )
    assert response.status_code == 200, response.text
    assert response.text == "OK"
