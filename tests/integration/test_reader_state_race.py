"""Read-state upserts under concurrency.

The Reader API is driven by NNW, which issues edit-tag requests concurrently
and in batches. SQLite (WAL) serialises writers, so a write that already
committed wins over an in-flight decision made from an older read -- and the
losing request must still complete, because last-write-wins means "the later
commit wins", never "the whole batch fails".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item as ItemRow
from adapters.db_models import ReadState as ReadStateRow
from adapters.repository import (
    read_states_for,
    upsert_read_state,
)
from core.reader_state import ReadState as ReadStateDomain
from core.reader_state import TagChange, merge_state
from tests.source_seed import seed_source

NOW = "2026-01-01T00:00:00.000000Z"


def _seed(session: Session, item_ids: list[str]) -> None:
    seed_source(
        session,
        source_id="src-1",
        url="https://example.test/src-1.xml",
        schedule_policy='{"type": "fixed", "interval": "30m"}',
    )
    session.commit()
    for item_id in item_ids:
        session.add(
            ItemRow(
                id=item_id,
                origin_id="src-1",
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
def test_an_upsert_wins_over_a_row_committed_after_its_read(
    session_factory: sessionmaker[Session],
) -> None:
    """The production failure, reduced to one transaction.

    NNW sends edit-tag concurrently, and each request reads the current state
    before deciding what to write. Request A reads (no row for race-1), then
    request B commits one, then A writes its stale decision. Under the old
    read-modify-write that insert was pending until commit and A's next query
    autoflushed it onto B's committed row, failing the whole batch with

        IntegrityError: UNIQUE constraint failed: read_state.item_id, ...

    Observed twice on the deployed host, from NNW's tag edits, on the previous
    release as well. The atomic upsert resolves the conflict inside the
    statement: B's committed row is updated, A's batch continues.
    """
    with session_factory() as seed_session:
        _seed(seed_session, ["race-1", "race-2"])

    # A reads (sees no rows) and would insert, but has not written yet.
    a = session_factory()
    states = read_states_for(a, ["race-1", "race-2"], tenant_id="default")
    assert states == {}
    merged = merge_state(
        item_id="race-1",
        current=states.get("race-1"),
        change=TagChange(is_read=True, is_starred=None),
        at=datetime.now(UTC),
    )

    # B commits a row for the same item between A's read and A's write.
    with session_factory() as b:
        upsert_read_state(b, _state("race-1", is_read=False), tenant_id="default")
        b.commit()

    # A writes its now-stale decision, then moves on to the next item -- the
    # second statement is what autoflushed the pending insert into a
    # violation before. Both must succeed.
    upsert_read_state(a, merged, tenant_id="default")
    upsert_read_state(a, _state("race-2"), tenant_id="default")
    a.commit()
    a.close()

    with session_factory() as check:
        states = read_states_for(check, ["race-1", "race-2"], tenant_id="default")
        assert states["race-1"].is_read is True, "A committed last, so A wins"
        assert states["race-2"].is_read is True


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
    with session_factory() as session:
        _seed(session, ["update-1"])
        upsert_read_state(
            session, _state("update-1", is_read=False), tenant_id="default"
        )
        session.commit()
        upsert_read_state(
            session, _state("update-1", is_read=True), tenant_id="default"
        )
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
    with session_factory() as session:
        _seed(session, ["merge-1"])
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
def test_concurrent_batches_resolve_last_write_wins(
    session_factory: sessionmaker[Session],
) -> None:
    """Two full write batches; both must complete, the later commit wins.

    This is the shape the deployed failure came from: two requests each
    writing several rows. WAL serialises the writers -- the second batch's
    statements wait out the first's transaction and then run -- so the second
    batch's values must win on the items it touched, and neither batch may
    fail. The failed version lost the *first* batch instead: its pending
    inserts hit the second batch's committed rows on the next autoflush.
    """
    with session_factory() as seed_session:
        _seed(seed_session, ["cw-1", "cw-2", "cw-3"])

    with session_factory() as first:
        states = read_states_for(first, ["cw-1", "cw-2", "cw-3"], tenant_id="default")
        assert states == {}
        for item_id in ("cw-1", "cw-2", "cw-3"):
            upsert_read_state(first, _state(item_id, is_read=True), tenant_id="default")
        first.commit()

    # The second batch ran after the first committed (WAL serialises
    # writers); it overwrites two of the three items.
    with session_factory() as second:
        for other in ("cw-1", "cw-3"):
            upsert_read_state(second, _state(other, is_read=False), tenant_id="default")
        second.commit()

    with session_factory() as check:
        states = read_states_for(check, ["cw-1", "cw-2", "cw-3"], tenant_id="default")
        assert len(states) == 3
        assert states["cw-1"].is_read is False, "the later commit wins"
        assert states["cw-2"].is_read is True
        assert states["cw-3"].is_read is False


@pytest.mark.integration
def test_mark_all_read_gains_read_without_losing_a_star(
    session_factory: sessionmaker[Session],
) -> None:
    """Mark-all touches unread rows only, and preserves an existing star.

    The bulk rewrite must keep the per-row loop's contract: a starred-but-
    unread item (starred through edit-tag, then bulk-marked) becomes read and
    stays starred; an already-read row is not rewritten at all.
    """
    with session_factory() as session:
        _seed(session, ["bulk-1", "bulk-2"])
        # bulk-1: starred but unread -- the row bulk-marking must update.
        # bulk-2: already read -- the candidates query excludes it entirely.
        session.add(
            ReadStateRow(
                item_id="bulk-1",
                tenant_id="default",
                is_read=False,
                is_starred=True,
                updated_at=NOW,
            )
        )
        session.add(
            ReadStateRow(
                item_id="bulk-2",
                tenant_id="default",
                is_read=True,
                is_starred=True,
                updated_at=NOW,
            )
        )
        session.commit()
        # ADR 0061: the gate is the readiness rows, and `mark_all_read` only
        # touches items the gate admits.
        from adapters.repository import mark_subscription_ready

        for item_id in ("bulk-1", "bulk-2"):
            mark_subscription_ready(
                session,
                item_id=item_id,
                subscription_id="src-1",
                at=NOW,
            )
        session.commit()

    with session_factory() as session:
        from adapters.repository import mark_all_read

        changed = mark_all_read(
            session,
            tenant_id="default",
            before=datetime.now(UTC),
            subscription_id="src-1",
        )
        session.commit()
        assert changed == 1, "only the unread row is a candidate"

        states = read_states_for(session, ["bulk-1", "bulk-2"], tenant_id="default")
        assert states["bulk-1"].is_read is True
        assert states["bulk-1"].is_starred is True, "the star must survive"
        assert states["bulk-2"].is_read is True


@pytest.mark.integration
def test_two_threads_of_edit_tag_requests_both_succeed(
    session_factory: sessionmaker[Session],
) -> None:
    """The production shape, with real concurrency: two client threads.

    NNW fires edit-tag requests from several threads at once, so the writers
    genuinely overlap rather than being sequenced by the test. WAL serialises
    the underlying transactions (the second thread's writes wait out the
    first's), which is exactly the behaviour the fix relies on: the old
    read-modify-write turned that overlap into an IntegrityError instead.
    """
    import threading
    import time

    with session_factory() as seed_session:
        _seed(seed_session, ["th-1", "th-2", "th-3"])

    outcomes: dict[str, str] = {}

    def batch(item_ids: list[str], *, is_read: bool, delay: float) -> None:
        session = session_factory()
        try:
            time.sleep(delay)
            for item_id in item_ids:
                upsert_read_state(
                    session,
                    _state(item_id, is_read=is_read),
                    tenant_id="default",
                )
                time.sleep(0.05)  # widen the overlap window
            session.commit()
            outcomes[",".join(item_ids)] = "ok"
        except Exception as exc:
            session.rollback()
            outcomes[",".join(item_ids)] = f"{type(exc).__name__}: {exc}"
        finally:
            session.close()

    # Thread A walks three items slowly; thread B starts mid-way and touches
    # the first and last of them. Their transactions overlap.
    threads = [
        threading.Thread(
            target=batch,
            kwargs={
                "item_ids": ["th-1", "th-2", "th-3"],
                "is_read": True,
                "delay": 0.0,
            },
        ),
        threading.Thread(
            target=batch,
            kwargs={"item_ids": ["th-1", "th-3"], "is_read": False, "delay": 0.1},
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes["th-1,th-2,th-3"] == "ok", outcomes
    assert outcomes["th-1,th-3"] == "ok", outcomes

    with session_factory() as check:
        states = read_states_for(check, ["th-1", "th-2", "th-3"], tenant_id="default")
        assert len(states) == 3
        # B started later and its commit lands after A's per-item writes for
        # the items it shares; whichever committed last wins per item.
        assert states["th-2"].is_read is True, "only A touched th-2"


@pytest.mark.integration
def test_a_starred_row_stays_starred_when_edit_tag_writes_again(
    session_factory: sessionmaker[Session],
) -> None:
    """A read-only edit-tag must not eat an existing star.

    The client sends only the tag that changed: `a=user/.../read` carries no
    star information, and `merge_state` keeps the current value for what was
    not mentioned. The upsert's conflict branch writes both columns, so the
    merged domain object is what carries the star through -- which is why this
    drives the full star-then-read sequence through merge_state, exactly as
    the edit-tag route composes it, rather than hand-building the domain row.
    """
    with session_factory() as session:
        _seed(session, ["star-1"])
        upsert_read_state(session, _state("star-1", is_read=False), tenant_id="default")
        session.commit()

        # The star arrives on its own edit-tag call.
        starred = merge_state(
            item_id="star-1",
            current=read_states_for(session, ["star-1"], tenant_id="default")["star-1"],
            change=TagChange(is_read=None, is_starred=True),
            at=datetime.now(UTC),
        )
        upsert_read_state(session, starred, tenant_id="default")
        session.commit()

        # Then a separate edit-tag marks it read, mentioning nothing about the
        # star. The merge keeps the star; the upsert must write it back.
        states = read_states_for(session, ["star-1"], tenant_id="default")
        merged = merge_state(
            item_id="star-1",
            current=states["star-1"],
            change=TagChange(is_read=True, is_starred=None),
            at=datetime.now(UTC),
        )
        upsert_read_state(session, merged, tenant_id="default")
        session.commit()

        final = read_states_for(session, ["star-1"], tenant_id="default")["star-1"]
        assert final.is_read is True
        assert final.is_starred is True, "the update branch ate the star"
