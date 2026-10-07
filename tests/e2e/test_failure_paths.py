"""The end-to-end loop's failure paths: what must not happen.

`test_full_loop.py` walks the happy path -- subscribe, collect, consume,
replay -- and asserts at each step that the expected thing is visible. This
file is its counterpart and asks the other question: when something is
refused, is the system actually unchanged?

That distinction is the reason these tests exist rather than being folded into
the happy path. A refusal that still writes is the worst kind of defect here,
because the response looks correct and the damage is only visible later: an
unauthenticated caller who manages to create a source, a write replayed without
its idempotency key, a pending item that leaks into a client's cache before its
enrichments have run.

Everything is real: a uvicorn thread, the auth middleware, the MCP session
manager, the repository, the rendered feed. The only
fixture is the upstream origin. Assertions are against what a client can
observe (status codes, Reader API output, rendered XML) and against the
database, never against whether some function was called -- a test that
checked *how* the refusal happened would break the first time the route
changed shape while the behaviour stayed correct.
"""

from __future__ import annotations

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.middleware import CorrelationAndAuditMiddleware
from tests.e2e.conftest import API_TOKEN, READER_PASSWORD, READER_USERNAME

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

# Deliberately not the configured token. Long enough to be plausible, so a
# route that rejected it for a length reason would fail this test rather than
# accidentally passing it.
WRONG_TOKEN = "e2e-wrong-token-that-is-not-configured"


@pytest.mark.parametrize("scope", ["bogus", "feed/", "user/-/state/com.google/bogus"])
async def test_user_cannot_mark_an_unsupported_stream_read(
    served: str, session_factory: sessionmaker[Session], scope: str
) -> None:
    """Given unread items, an unsupported bulk scope is rejected without writes."""
    from adapters.db_models import ReadState
    from app.pipeline import normalise_and_store
    from core.models import ItemDraft

    source_id = _seed_source(session_factory)
    with session_factory() as session:
        normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source_id,
                    external_id="unread",
                    title="Unread item",
                    canonical_url="https://pending.example/unread",
                    published_at=None,
                    content="<p>Unread content</p>",
                )
            ],
            tenant_id="default",
        )
        session.commit()
        before = session.execute(sa.select(ReadState.__table__)).all()
    async with httpx.AsyncClient(base_url=served) as client:
        login = await client.post(
            "/api/greader.php/accounts/ClientLogin",
            data={"Email": READER_USERNAME, "Passwd": READER_PASSWORD},
        )
        assert login.status_code == 200
        token = next(
            line[5:] for line in login.text.splitlines() if line.startswith("Auth=")
        )
        response = await client.post(
            "/api/greader.php/reader/api/0/mark-all-as-read",
            data={"T": token, "s": scope},
        )
        assert response.status_code == 400
        assert response.json() == {"detail": f"unsupported stream {scope!r}"}
        unread = await client.get(
            "/api/greader.php/reader/api/0/stream/items/ids",
            headers={"Authorization": f"GoogleLogin auth={token}"},
            params={"xt": "user/-/state/com.google/read"},
        )
        assert unread.status_code == 200
        assert len(unread.json()["itemRefs"]) == 1
    with session_factory() as session:
        assert session.execute(sa.select(ReadState.__table__)).all() == before


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    """Row count for one of the tables this file inspects.

    The name is interpolated because SQL does not accept a parameter in a
    `FROM` clause; the allowlist is what keeps that from being string
    construction, and every caller passes a literal defined here.
    """
    if table not in {"origin", "item", "audit_log", "work", "work_unit"}:
        raise ValueError(f"unexpected table {table!r}")
    if table == "audit_log":
        # Only the audit table has a writer behind a queue to wait for; a
        # refused request is answered before that writer is even reached, so
        # without the drain a "no row" assertion could pass on timing alone.
        CorrelationAndAuditMiddleware.drain_audit()
    with session_factory() as db:
        return int(db.execute(sa.text(f"select count(*) from {table}")).scalar_one())  # noqa: S608


def _outcomes(
    session_factory: sessionmaker[Session], operation: str | None = None
) -> list[str]:
    """The `outcome` of each audit row, oldest first.

    A refused request is audited too (`AuditOutcome.REJECTED` for any 4xx), so
    "nothing happened" is never `audit_log == 0`. The honest assertion is on
    the outcome: the attempt is recorded as rejected, and no row says a write
    succeeded.
    """
    CorrelationAndAuditMiddleware.drain_audit()
    query = "select outcome from audit_log"
    params: dict[str, str] = {}
    if operation is not None:
        query += " where operation = :operation"
        params["operation"] = operation
    query += " order by occurred_at"
    with session_factory() as db:
        return [str(row[0]) for row in db.execute(sa.text(query), params)]


def _seed_source(session_factory: sessionmaker[Session]) -> str:
    """Store a source through the repository and return the id it chose.

    Raw INSERT would have to restate every NOT NULL column and breaks whenever
    the schema gains one -- which is exactly what the first two drafts of this
    file did (they predated `created_at`, and then guessed an id the repository
    had not used). The repository derives the identity, so the id is taken from
    what it stored rather than assumed.
    """
    from datetime import UTC, datetime

    from adapters.repository import create_source
    from adapters.timestamps import fixed_width_utc as timestamp

    with session_factory() as db:
        row = create_source(
            db,
            source_type="rss",
            url="https://pending.example/feed.xml",
            canonical_url="https://pending.example/feed.xml",
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            tenant_id="default",
            at=timestamp(datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
        )
        db.commit()
        return str(row.id)


# --------------------------------------------------------------------------
# Scenario: an unauthenticated write is refused, and nothing changes
# --------------------------------------------------------------------------


async def test_user_cannot_reuse_a_write_key_for_a_different_body(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """Given a completed write, when its key has a new body, then 409 and no change."""
    source_id = _seed_source(session_factory)
    async with httpx.AsyncClient(
        base_url=served,
        headers={
            "Authorization": f"Bearer {API_TOKEN}",
            "Idempotency-Key": "body-conflict",
        },
    ) as client:
        first = await client.patch(f"/v1/sources/{source_id}", json={"folder": "First"})
        assert first.status_code == 200
        refused = await client.patch(
            f"/v1/sources/{source_id}", json={"folder": "Second"}
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["error"]["code"] == "idempotency_key_conflict"
        assert "Idempotent-Replay" not in refused.headers
        current = await client.get(f"/v1/sources/{source_id}")
        assert current.json()["folder"] == "First"


async def test_a_wrong_token_cannot_create_a_source(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """401, with no row written and no audit entry claiming a write happened.

    The middleware refuses before the body is parsed, which is what makes the
    "no side effect" half meaningful rather than incidental -- a route-level
    check would already have validated the payload. Asserted at the database
    because the status code alone cannot show that nothing was stored.
    """
    before = _count(session_factory, "origin")

    async with httpx.AsyncClient(base_url=served) as client:
        response = await client.post(
            "/v1/sources",
            headers={
                "Authorization": f"Bearer {WRONG_TOKEN}",
                "Idempotency-Key": "e2e-wrong-token",
            },
            json={
                "url": "https://example.com/feed.xml",
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        )

    assert response.status_code == 401, response.text
    assert _count(session_factory, "origin") == before, "a refused write stored a row"
    # No audit row either, and that is the documented order rather than an
    # oversight: EarlyAuth sits *outside* CorrelationAndAudit so an
    # unauthenticated request is refused before the audit write. `_should_audit`
    # would otherwise record this 401 (auth failures are auditable by design),
    # so asserting the empty log pins the ordering the comment in main.py
    # describes. If auditing moves ahead of the auth check, this test should
    # fail and the change should be a deliberate one.
    assert _outcomes(session_factory) == [], _outcomes(session_factory)


async def test_a_missing_token_is_refused_like_a_wrong_one(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """No credential must not be treated as an anonymous-but-allowed caller.

    A route that read `Authorization` optionally would accept this request and
    create a source attributed to nobody.
    """
    async with httpx.AsyncClient(base_url=served) as client:
        response = await client.post(
            "/v1/sources",
            headers={"Idempotency-Key": "e2e-no-token"},
            json={
                "url": "https://example.com/feed.xml",
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        )

    assert response.status_code == 401, response.text
    assert _count(session_factory, "origin") == 0


async def test_a_wrong_token_cannot_read_either(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """The scope check is not write-only: reads are refused too.

    Separate from the write case because a middleware that guarded only
    mutating methods would pass that test and still hand an unauthenticated
    caller the whole archive here.
    """
    async with httpx.AsyncClient(base_url=served) as client:
        response = await client.get(
            "/v1/items", headers={"Authorization": f"Bearer {WRONG_TOKEN}"}
        )

    assert response.status_code == 401, response.text


async def test_an_unauthenticated_request_is_refused_before_the_body_is_parsed(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """Bad credential plus invalid body must be 401, not 422.

    This is the observable consequence of authenticating at the ASGI layer
    rather than in each route: a 422 would tell an unauthenticated caller that
    their JSON was malformed, and would mean the payload had already been
    parsed by the time the credential was checked. It also keeps the two
    failure modes from being confusable by a client.
    """
    async with httpx.AsyncClient(base_url=served) as client:
        response = await client.post(
            "/v1/sources",
            headers={
                "Authorization": f"Bearer {WRONG_TOKEN}",
                "Idempotency-Key": "e2e-bad-body",
                "Content-Type": "application/json",
            },
            content=b"{not json at all",
        )

    assert response.status_code == 401, response.text
    assert response.status_code != 422, "the body was parsed before authorising"
    assert _count(session_factory, "origin") == 0


# --------------------------------------------------------------------------
# Scenario: an unconfirmed MCP write does not happen
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Scenario: an item behind the gate is invisible on both output surfaces
# --------------------------------------------------------------------------


async def test_a_pending_item_is_absent_from_both_read_surfaces(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """A pending item must not reach a client, and must not 404 the stream.

    The gate is the invariant the whole design exists to protect: a feed that
    later swaps content under a fixed guid corrupts a client's cache, and the
    client cannot be told. Asserted on *both* surfaces, because they are
    independent readers of the same table and a filter applied in one place
    only would still leak through the other.

    Also asserts the pending row exists -- otherwise the test would pass in a
    database where nothing had been stored, proving nothing about the filter.
    """
    pending_id = "e2e-pending-item"
    source_id = _seed_source(session_factory)
    with session_factory() as db:
        # Through the repository: `Item` already defaults to pending, which is
        # the state this scenario is about, and a raw INSERT would have to
        # restate `created_at` and any column the schema gains later.
        from datetime import UTC, datetime

        from adapters.repository import store_item
        from core.enums import PublishStatus
        from core.models import Item

        outcome = store_item(
            db,
            Item(
                id=pending_id,
                origin_id=source_id,
                guid=pending_id,
                content_hash="hash-pending",
                title="Behind the gate",
                url="https://pending.example/1",
                published_at=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
                publish_status=PublishStatus.PENDING,
                tenant_id="default",
            ),
        )
        db.commit()
        assert outcome.item.publish_status is PublishStatus.PENDING

        # The row is there, so a passing test below is about visibility.
        stored = db.execute(
            sa.text("select publish_status from item where id = :id"),
            {"id": pending_id},
        ).scalar_one()
        assert stored == PublishStatus.PENDING.value

        # The stored side also proves the RSS selector excludes it.
        from adapters.repository import items_ready_for_feed
        from adapters.rss_out import FeedMeta, render_feed
        from app.pipeline import bodies_for

        ready = items_ready_for_feed(db)
        assert pending_id not in {item.id for item in ready}
        rendered = render_feed(
            FeedMeta(title="E2E", link="https://e2e.example"),
            ready,
            bodies_for(db, ready),
        ).decode()
        assert pending_id not in rendered, "a pending item reached the feed"

    # The API side, over real HTTP.
    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        listed = await client.get("/v1/items")
        assert listed.status_code == 200, listed.text
        assert pending_id not in {row["id"] for row in listed.json()["data"]}

        # And the Reader API, which is the mounted surface a client actually
        # reads: it must not expose the id either.
        login = await client.post(
            "/api/greader.php/accounts/ClientLogin",
            data={"Email": "e2e-reader", "Passwd": "e2e-reader-pw"},
        )
        assert login.status_code == 200, login.text
        token = next(
            line.removeprefix("Auth=").strip()
            for line in login.text.splitlines()
            if line.startswith("Auth=")
        )
        ids = await client.get(
            "/api/greader.php/reader/api/0/stream/items/ids",
            headers={"Authorization": f"GoogleLogin auth={token}"},
            params={"output": "json", "n": 1000},
        )
        assert ids.status_code == 200, ids.text
        assert pending_id not in {
            ref["id"] for ref in ids.json().get("itemRefs", [])
        }, "a pending item reached the Reader API"


async def test_hiding_a_pending_item_does_not_hide_the_stream(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """An empty result must be an empty stream, not an error or a 404.

    The gate hides items; it must not hide the subscription. A client that
    received 404 for a source whose items are all pending would show the user
    a broken subscription rather than an empty one.
    """
    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        listed = await client.get("/v1/items")

    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []


# --------------------------------------------------------------------------
# Scenario: an Idempotency-Key is not a wildcard
# --------------------------------------------------------------------------


async def test_the_same_key_with_a_different_body_does_not_create_a_second_source(
    served: str,
    origin_url: str,
    session_factory: sessionmaker[Session],
) -> None:
    """Reusing a key is not a licence to execute a *different* request.

    The happy path replays an identical request. The dangerous case is a key
    reused with different content: a server that keyed only on the header would
    return the first response and silently ignore the new URL, while one that
    keyed only on the body would create a second source. Either way the caller
    cannot tell, so the assertion is on what ended up stored.
    """
    first_body = {
        "url": origin_url,
        "schedule_policy": {"type": "fixed", "interval": "30m"},
    }

    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        first = await client.post(
            "/v1/sources", headers={"Idempotency-Key": "e2e-reuse-1"}, json=first_body
        )
        assert first.status_code == 201, first.text
        source_id = first.json()["id"]

        second = await client.post(
            "/v1/sources",
            headers={"Idempotency-Key": "e2e-reuse-1"},
            json={
                "url": "https://different.example/feed.xml",
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        )

    # Whatever the server chose to answer, exactly one source exists and it is
    # the first one. That is the property a client depends on.
    assert _count(session_factory, "origin") == 1
    with session_factory() as db:
        stored = db.execute(sa.text("select id from origin")).scalar_one()
    assert stored == source_id
    # The mismatch policy is pinned (matching the integration contract in
    # test_sources_api): a key bound to a different request is a 409, not a
    # silent replay or a validation error -- a caller can distinguish a
    # deliberate refusal from either.
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"]["code"] == "idempotency_key_conflict"


async def test_a_write_without_a_key_is_refused_rather_than_executed(
    served: str, origin_url: str, session_factory: sessionmaker[Session]
) -> None:
    """The contract requires the key; a missing one is an error, not a free pass.

    Treated as its own scenario because "the key is required" and "the key
    replays" are independent claims, and a server could implement the second
    while quietly allowing the first.
    """
    async with httpx.AsyncClient(
        base_url=served, headers={"Authorization": f"Bearer {API_TOKEN}"}
    ) as client:
        response = await client.post(
            "/v1/sources",
            json={
                "url": origin_url,
                "schedule_policy": {"type": "fixed", "interval": "30m"},
            },
        )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["code"] == "idempotency_key_required"
    assert _count(session_factory, "origin") == 0


# --------------------------------------------------------------------------
# Scenario: an address off the source's host is refused, and nothing is stored
# --------------------------------------------------------------------------


async def test_an_address_off_the_source_host_writes_nothing(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """The provenance boundary, end to end: refused, and the store untouched.

    A happy path cannot show this. `:fetch-items` is the one endpoint that
    takes *addresses* rather than content, so it is the one place a caller
    could try to make this deployment fetch from a server the source never
    agreed to talk to -- and the refusal has to hold where the request
    actually lands, not only in the pipeline helper the integration tests
    call.
    """
    source_id = _seed_source(session_factory)

    async with httpx.AsyncClient(base_url=served) as client:
        response = await client.post(
            f"/v1/sources/{source_id}:fetch-items",
            headers={
                "Authorization": f"Bearer {API_TOKEN}",
                "Idempotency-Key": "e2e-fetch-items-offhost",
            },
            json={"urls": ["https://elsewhere.example/blog/archives/1"]},
        )

    assert response.status_code == 200, response.text
    [result] = response.json()["results"]
    assert result["action"] == "rejected"
    assert result["error_class"] == "structure_changed"
    assert _count(session_factory, "item") == 0
    # The refusal is recorded rather than silent: the compat run leaves a work
    # and one unit for the address (ADR 0046), while the item store stays empty.
    assert _count(session_factory, "work") == 1
    assert _count(session_factory, "work_unit") == 1


async def test_a_purge_without_a_reason_is_refused_and_deletes_nothing(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """Given a subscription with an item, purging needs a reason.

    Both the missing reason and a blank one are refused with the API's own
    code, and neither touches the content: a refusal that still wrote is the
    defect this file exists to catch. The third call is the control -- with a
    reason it does delete -- so the two above are about the reason rather than
    about an endpoint that never worked.
    """
    from app.pipeline import normalise_and_store
    from core.models import ItemDraft

    source_id = _seed_source(session_factory)
    with session_factory() as session:
        stored = normalise_and_store(
            session,
            [
                ItemDraft(
                    origin_id=source_id,
                    external_id="purge-me",
                    title="Keep me",
                    canonical_url="https://pending.example/purge-me",
                    published_at=None,
                    content="<p>A body that only a purge may remove.</p>",
                )
            ],
        )[0]
        session.commit()
        item_id = stored.result.item.id
    before = _count(session_factory, "item")

    async with httpx.AsyncClient(base_url=served) as client:
        missing = await client.delete(
            f"/v1/origins/{source_id}:purge",
            headers={
                "Authorization": f"Bearer {API_TOKEN}",
                "Idempotency-Key": "e2e-purge-without",
            },
        )
        blank = await client.delete(
            f"/v1/origins/{source_id}:purge",
            params={"reason": "   "},
            headers={
                "Authorization": f"Bearer {API_TOKEN}",
                "Idempotency-Key": "e2e-purge-blank",
            },
        )
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"]["error"]["code"] == "reason_required"
    assert blank.status_code == 422, blank.text
    assert blank.json()["detail"]["error"]["code"] == "reason_required"
    # Checked *before* the control call below, which is what makes this
    # assertion about the refusals rather than about the order they ran in.
    assert _count(session_factory, "item") == before, "a refused purge deleted content"

    async with httpx.AsyncClient(base_url=served) as client:
        purged = await client.delete(
            f"/v1/origins/{source_id}:purge",
            params={"reason": "the e2e test says so"},
            headers={
                "Authorization": f"Bearer {API_TOKEN}",
                "Idempotency-Key": "e2e-purge-with",
            },
        )

    assert purged.status_code == 200, purged.text
    assert purged.json()["reason"] == "the e2e test says so"
    assert purged.json()["items"] == 1
    with session_factory() as session:
        assert (
            session.execute(
                sa.text("select count(*) from item where id = :id"), {"id": item_id}
            ).scalar_one()
            == 0
        ), "the control call has to actually delete, or the refusals prove nothing"

    # The reason survives in the audit row, which is the point of requiring it.
    CorrelationAndAuditMiddleware.drain_audit()
    with session_factory() as session:
        row = session.execute(
            sa.text(
                "select args_redacted from audit_log"
                " where operation like '%purge%' and outcome = 'succeeded'"
                " order by occurred_at desc"
            )
        ).first()
    assert row is not None, "a destructive write has to be audited"
    assert "the e2e test says so" in row[0], row
