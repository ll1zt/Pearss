"""The work ledger's persistence, and its one hard constraint.

Scenario register (normative source: SCENARIOS.md, AC-180..182; issue #187).

The composite key `(work_id, target)` is the ledger's whole idea: one row per
target per work, so replaying a target cannot produce a second row. What
enforces it is the database, not a caller-side check -- a SELECT-then-INSERT
race is precisely the case the constraint exists for, and only a refused insert
proves it is really there.

Runs against a real migrated SQLite database (the repository's own fixture):
the constraint under test is a schema fact, so a fake store would test nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import (
    append_unit,
    counts_for_work,
    create_work,
    units_for_work,
    work_by_id,
)
from core.enums import ErrorClass, UnitOutcome, WorkStatus
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration


def test_a_work_row_round_trips_through_the_repository(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-180: the migrated table exists and the repository reads it back.

    The migration round trip itself is `just migrate-check` (CI); this is the
    local half: the columns the ledger needs survive a write and a read.
    """
    with session_factory() as session:
        created = create_work(
            session,
            kind="fetch",
            targets=["https://a.example/1"],
            params={"subscription": "s1"},
            fingerprint="fp-round-trip",
            tenant_id="default",
        )
        session.commit()
        work_id = created.id

    with session_factory() as session:
        record = work_by_id(session, work_id)

    assert record is not None
    assert record.id == work_id
    assert record.kind == "fetch"
    assert record.targets == ["https://a.example/1"]
    assert record.params == {"subscription": "s1"}
    assert record.status is WorkStatus.QUEUED
    assert record.finished_at is None


def test_the_ledger_refuses_a_second_row_for_the_same_target(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-181: a repeated target is refused by the database, not by the caller."""
    with session_factory() as session:
        work = create_work(
            session,
            kind="fetch",
            targets=["https://a.example/1"],
            params={},
            fingerprint="fp-duplicate",
            tenant_id="default",
        )
        append_unit(
            session,
            work_id=work.id,
            target="https://a.example/1",
            outcome=UnitOutcome.OK,
            detail="",
        )
        session.commit()
        work_id = work.id

    with session_factory() as session:
        with pytest.raises(IntegrityError):
            append_unit(
                session,
                work_id=work_id,
                target="https://a.example/1",
                outcome=UnitOutcome.FAILED,
                detail="second opinion",
            )
            session.flush()
        session.rollback()

    # The first row is untouched, and the ledger still has exactly one row.
    with session_factory() as session:
        rows = units_for_work(session, work_id)
    assert [row.outcome for row in rows] == [UnitOutcome.OK]


def test_ledger_rows_carry_their_target_and_class(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-180: the ledger row is the attribution.

    A failed target leaves no item, so the ledger is the only place its class
    can live; `target` is what makes "why did *this* address fail" answerable.
    """
    with session_factory() as session:
        work = create_work(
            session,
            kind="fetch",
            targets=["https://a.example/rejected", "https://a.example/slow"],
            params={},
            fingerprint="fp-attribution",
            tenant_id="default",
        )
        append_unit(
            session,
            work_id=work.id,
            target="https://a.example/rejected",
            outcome=UnitOutcome.REJECTED,
            error_class=ErrorClass.STRUCTURE_CHANGED,
            detail="address is not on the source's own host",
        )
        append_unit(
            session,
            work_id=work.id,
            target="https://a.example/slow",
            outcome=UnitOutcome.FAILED,
            error_class=ErrorClass.NETWORK,
            detail="origin unreachable",
        )
        session.commit()
        work_id = work.id

    with session_factory() as session:
        rows = {row.target: row for row in units_for_work(session, work_id)}

    assert set(rows) == {"https://a.example/rejected", "https://a.example/slow"}
    assert rows["https://a.example/rejected"].outcome is UnitOutcome.REJECTED
    assert (
        rows["https://a.example/rejected"].error_class is ErrorClass.STRUCTURE_CHANGED
    )
    assert rows["https://a.example/slow"].error_class is ErrorClass.NETWORK


def test_counts_agree_with_the_rows_in_the_database(
    session_factory: sessionmaker[Session],
) -> None:
    """AC-182 (the persistence half): the fold reads the ledger, nothing else."""
    with session_factory() as session:
        work = create_work(
            session,
            kind="fetch",
            targets=[f"https://a.example/{n}" for n in range(1, 7)],
            params={},
            fingerprint="fp-counts",
            tenant_id="default",
        )
        for target, outcome in (
            ("https://a.example/1", UnitOutcome.OK),
            ("https://a.example/2", UnitOutcome.OK),
            ("https://a.example/3", UnitOutcome.SKIPPED),
            ("https://a.example/4", UnitOutcome.REJECTED),
        ):
            append_unit(
                session, work_id=work.id, target=target, outcome=outcome, detail=""
            )
        session.commit()
        work_id = work.id

    with session_factory() as session:
        counts = counts_for_work(session, work_id)

    assert counts == {
        "ok": 2,
        "skipped": 1,
        "rejected": 1,
        "failed": 0,
        "queued": 2,
        "total": 6,
    }


def test_the_work_row_has_no_counter_column(tmp_path: Path) -> None:
    """AC-182 (the structural half): there is nothing to read but the rows.

    A guard rather than a behaviour: if a `counts`/`ok_count` column ever
    appears, the fold and the stored number become two answers to one question.
    Read from the migrated schema, so it also fails if a future migration adds
    one behind the model's back.
    """
    url = migrated_database(tmp_path, "work_columns.db")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            columns = [
                row[1] for row in conn.execute(sa.text("pragma table_info(work)"))
            ]
    finally:
        engine.dispose()

    assert columns, "the work table is missing"
    assert not [name for name in columns if "count" in name], columns
