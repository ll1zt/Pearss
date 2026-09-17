"""Idempotency records: the scoping invariant, tested at its own layer.

The HTTP surface can only present one write principal (there is a single
write token), so a request-level test cannot distinguish "the key is scoped
per actor" from "the actor is not part of the key at all" -- the read token
is refused by the auth middleware before the lookup runs. The invariant lives
in `find_idempotent` / `store_idempotent`, which key on
(actor, endpoint, key), so that is where it is pinned.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import find_idempotent, store_idempotent

pytestmark = pytest.mark.integration

NOW = "2026-06-01T12:00:00.000000Z"
LATER = "2026-06-02T12:00:00.000000Z"


def _store(
    session: Session,
    *,
    actor: str,
    key: str,
    body: str = "{}",
    endpoint: str = "POST /v1/sources",
) -> None:
    store_idempotent(
        session,
        actor=actor,
        endpoint=endpoint,
        key=key,
        status_code=201,
        response_body=body,
        created_at=NOW,
        expires_at=LATER,
    )
    session.commit()


def test_user_stored_response_cannot_be_overwritten_by_a_late_writer(
    session_factory: sessionmaker[Session],
) -> None:
    """Given a durable response, another writer cannot replace the replay result."""
    with session_factory() as session:
        _store(session, actor="api:full", key="immutable", body='{"first":true}')
        _store(session, actor="api:full", key="immutable", body='{"second":true}')
    with session_factory() as session:
        stored = find_idempotent(
            session,
            actor="api:full",
            endpoint="POST /v1/sources",
            key="immutable",
            now=NOW,
        )
        assert stored is not None
        assert stored.response_body == '{"first":true}'


def test_user_expired_complete_response_can_be_replaced_but_old_owner_is_fenced(
    session_factory: sessionmaker[Session],
) -> None:
    """Given expiry and a new operation, the old owner cannot overwrite its result."""
    from adapters.repository.idempotency import complete_idempotent, reserve_idempotent

    common = {"actor": "api:full", "endpoint": "POST /v1/sources", "key": "expired"}
    with session_factory() as session:
        reserve_idempotent(
            session,
            **common,
            fingerprint="a" * 64,
            owner_token="old",
            now=NOW,
            expires_at=LATER,
        )
        complete_idempotent(
            session,
            **common,
            owner_token="old",
            status_code=201,
            response_body='{"old":true}',
            expires_at=LATER,
        )
        session.commit()
    with session_factory() as session:
        reserved = reserve_idempotent(
            session,
            **common,
            fingerprint="b" * 64,
            owner_token="new",
            now="2026-06-03T00:00:00.000000Z",
            expires_at="2026-06-04T00:00:00.000000Z",
        )
        assert (reserved.owner_token, reserved.status_code, reserved.response_body) == (
            "new",
            0,
            "{}",
        )
        session.commit()
    with session_factory() as session:
        with pytest.raises(RuntimeError, match="no longer owned"):
            complete_idempotent(
                session,
                **common,
                owner_token="old",
                status_code=201,
                response_body='{"late":true}',
                expires_at=LATER,
            )
        session.rollback()
    with session_factory() as session:
        complete_idempotent(
            session,
            **common,
            owner_token="new",
            status_code=202,
            response_body='{"new":true}',
            expires_at="2026-06-04T00:00:00.000000Z",
        )
        session.commit()
        stored = find_idempotent(session, **common, now="2026-06-03T00:00:00.000000Z")
        assert stored is not None
        assert (stored.status_code, stored.response_body) == (202, '{"new":true}')


@pytest.mark.integration
def test_the_same_key_under_a_different_actor_does_not_replay(
    session_factory: sessionmaker[Session],
) -> None:
    """A key is not a global namespace: actor is part of its identity.

    Without this, one caller reusing another's key would read back the other's
    stored response body (and a second write would silently no-op).
    """
    with session_factory() as session:
        _store(session, actor="api:full", key="shared", body='{"mine": true}')

    with session_factory() as session:
        other = find_idempotent(
            session,
            actor="api:other",
            endpoint="POST /v1/sources",
            key="shared",
            now=NOW,
        )
    assert other is None, "another actor's stored response leaked across keys"


@pytest.mark.integration
def test_the_same_key_under_a_different_endpoint_does_not_replay(
    session_factory: sessionmaker[Session],
) -> None:
    """Endpoint is the third component: the key space is per route."""
    with session_factory() as session:
        _store(session, actor="api:full", key="k", endpoint="POST /v1/sources")

    with session_factory() as session:
        other = find_idempotent(
            session,
            actor="api:full",
            endpoint="POST /v1/plugins/rss:disable",
            key="k",
            now=NOW,
        )
    assert other is None


@pytest.mark.integration
def test_the_same_actor_endpoint_and_key_does_replay(
    session_factory: sessionmaker[Session],
) -> None:
    """The positive control: the lookup must actually find its own row.

    Without this, a `find_idempotent` broken to always return None would make
    the isolation tests above pass for the wrong reason.
    """
    with session_factory() as session:
        _store(session, actor="api:full", key="k", body='{"n": 1}')

    with session_factory() as session:
        found = find_idempotent(
            session,
            actor="api:full",
            endpoint="POST /v1/sources",
            key="k",
            now=NOW,
        )
    assert found is not None
    assert found.response_body == '{"n": 1}'


@pytest.mark.integration
def test_an_expired_record_reads_as_absent(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _store(session, actor="api:full", key="k")

    with session_factory() as session:
        expired = find_idempotent(
            session,
            actor="api:full",
            endpoint="POST /v1/sources",
            key="k",
            now="2026-06-03T00:00:00.000000Z",
        )
    assert expired is None
