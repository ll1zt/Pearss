"""Read-state upserts under concurrency.

The Reader API is driven by NNW, which issues edit-tag requests concurrently
and in batches. The write is last-write-wins, so two sessions racing on the
same item must not be able to fail the request: whichever commits second is
simply the winner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item as ItemRow
from adapters.db_models import Source as SourceRow
from adapters.repository import read_states_for, upsert_read_state
from core.reader_state import ReadState as ReadStateDomain
from core.reader_state import TagChange, merge_state

NOW = "2026-01-01T00:00:00.000000Z"


def _seed(session: Session, item_ids: list[str]) -> None:
    session.add(
        SourceRow(
            id="src-1",
            type="rss",
            url="https://example.test/src-1.xml",
            canonical_url="https://example.test/src-1.xml",
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            folder="default",
            status="active",
            trust_level="standard",
            tenant_id="default",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()
    for item_id in item_ids:
        session.add(
            ItemRow(
                id=item_id,
                source_id="src-1",
                guid=f"guid-{item_id}",
                content_hash="hash",
                title="t",
                url=f"https://example.test/{item_id}",
                content_ref=None,
                publish_status="published",
                tenant_id="default",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    session.commit()


def _state(item_id: str, *, is_read: bool = True) -> ReadStateDomain:
    return ReadStateDomain(
        item_id=item_id,
        is_read=is_read,
        is_starred=False,
        updated_at=datetime.now(UTC),
    )


@pytest.mark.integration
def test_a_pending_upsert_does_not_collide_with_a_committed_row(
    session_factory: sessionmaker[Session],
) -> None:
    """The production failure, reduced to one transaction.

    NNW sends edit-tag concurrently. Request A upserts item X (its INSERT is
    pending, uncommitted); request B upserts the same item X and commits.
    When A then touches item Y in the same transaction, the query triggers an
    autoflush, and A's pending INSERT violates the composite primary key --
    taking the whole batch down with a 500. Observed twice on the deployed
    host, from NNW's tag edits, on the previous release as well.

    Last-write-wins means B's committed row must simply win over A's pending
    insert, and A's transaction must continue.
    """
    with session_factory() as seed_session:
        _seed(seed_session, ["race-1", "race-2"])

    # A writes race-1 and holds the transaction open, as edit-tag does while
    # it works through the rest of the batch.
    a = session_factory()
    upsert_read_state(a, _state("race-1", is_read=True), tenant_id="default")

    # B races in, writes the same item, and commits first.
    b = session_factory()
    upsert_read_state(b, _state("race-1", is_read=False), tenant_id="default")
    b.commit()
    b.close()

    # A proceeds to the next item; the query inside the upsert autoflushes A's
    # pending INSERT. This is the exact statement that raised in production.
    try:
        upsert_read_state(a, _state("race-2"), tenant_id="default")
        a.commit()
    except Exception as exc:  # noqa: BLE001 -- the assertion is the diagnosis
        a.rollback()
        pytest.fail(f"the racing write failed the whole transaction: {exc}")
    finally:
        a.close()

    # And the outcome is last-write-wins: B committed after A's intent, so
    # B's value stands.
    with session_factory() as check:
        states = read_states_for(check, ["race-1", "race-2"], tenant_id="default")
        assert states["race-1"].is_read is False, "B's committed write must win"
        assert states["race-2"].is_read is True, "A's later item must still land"


@pytest.mark.integration
def test_the_same_item_upserted_twice_in_one_request_stays_one_row(
    session_factory: sessionmaker[Session],
) -> None:
    """One request can legitimately touch an item twice.

    A batch from NNW may carry the same id more than once (e.g. read and
    starred in two lines), and a replayed request after a timeout repeats ids
    that were already written. Neither may fabricate a second row.
    """
    with session_factory() as seed_session:
        _seed(seed_session, ["twice-1"])

    with session_factory() as session:
        for _ in range(2):
            upsert_read_state(session, _state("twice-1"), tenant_id="default")
        session.commit()

    with session_factory() as session:
        states = read_states_for(session, ["twice-1"], tenant_id="default")
        assert len(states) == 1
        assert states["twice-1"].is_read is True


@pytest.mark.integration
def test_an_upsert_of_an_existing_row_updates_it_in_place(
    session_factory: sessionmaker[Session],
) -> None:
    """The common path: the row exists, so the flags and timestamp move.

    Guarded here because the atomic rewrite changes how the existing-row case
    is executed; the observable contract must not change.
    """
    with session_factory() as seed_session:
        _seed(seed_session, ["update-1"])

    with session_factory() as session:
        upsert_read_state(
            session, _state("update-1", is_read=False), tenant_id="default"
        )
        session.commit()
        upsert_read_state(session, _state("update-1", is_read=True), tenant_id="default")
        session.commit()

        states = read_states_for(session, ["update-1"], tenant_id="default")
        assert states["update-1"].is_read is True
        assert states["update-1"].is_starred is False


@pytest.mark.integration
def test_upsert_preserves_the_unstarred_default_for_a_new_row(
    session_factory: sessionmaker[Session],
) -> None:
    """A new row carries exactly the flags the client sent.

    The rewrite goes through a Core-level merge; a flag the client left unset
    (None) must land as False rather than as an inherited value, which
    `merge_state` is what defines.
    """
    with session_factory() as seed_session:
        _seed(seed_session, ["merge-1"])

    with session_factory() as session:
        merged = merge_state(
            item_id="merge-1",
            current=None,
            change=TagChange(is_read=True, is_starred=None),
            at=datetime.now(UTC),
        )
        upsert_read_state(session, merged, tenant_id="default")
        session.commit()

        states = read_states_for(session, ["merge-1"], tenant_id="default")
        assert states["merge-1"].is_read is True
        assert states["merge-1"].is_starred is False


@pytest.mark.integration
def test_concurrent_read_state_writers_resolve_last_write_wins(
    session_factory: sessionmaker[Session],
) -> None:
    """Two full write batches interleaved; both must complete.

    This drives the shape the deployed failure came from at its own level:
    two sessions each writing several rows, with the second commit landing
    between the first session's first write and its commit.
    """
    with session_factory() as seed_session:
        _seed(seed_session, ["cw-1", "cw-2", "cw-3"])

    first = session_factory()
    second = session_factory()
    try:
        for item_id in ("cw-1", "cw-2", "cw-3"):
            upsert_read_state(
                first, _state(item_id, is_read=True), tenant_id="default"
            )
            if item_id == "cw-1":
                # The second request commits while the first is still working.
                for other in ("cw-1", "cw-3"):
                    upsert_read_state(
                        second, _state(other, is_read=False), tenant_id="default"
                    )
                second.commit()
        first.commit()

        with session_factory() as check:
            states = read_states_for(
                check, ["cw-1", "cw-2", "cw-3"], tenant_id="default"
            )
            assert len(states) == 3
            assert states["cw-1"].is_read is False, "the later write wins"
            assert states["cw-2"].is_read is True
            assert states["cw-3"].is_read is False
    finally:
        first.close()
        second.close()
