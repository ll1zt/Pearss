"""The migrated schema is built once per session, not once per test.

`migrated_database` runs `alembic upgrade head` on every call, and the
integration suite calls it once for each test that touches the database --
about 350 of 568. Measured at 538 ms a call, that is ~190 s of the suite's
669 s: 28% of it, spent deriving the same 20 migrations into 350 identical
empty files.

The fixtures it backs are deliberately function-scoped: each test gets its own
empty database, so one test's rows cannot reach the next. The fix is therefore
not to share a single database -- it is to build the schema once into a
template and copy the file per test.

Both halves are pinned here. The first test is the acceptance case for issue
#44: the cost is paid once. The second pins the property that makes sharing a
tempting-but-wrong shortcut -- every database is still a real, independent
migration -- so a future "optimization" that hands out one database cannot
pass by being fast.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from tests.integration.conftest import TENANT
from tests.migrated_schema import build, migrated_database

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Enough copies to tell "once per session" from "once per call" apart, without
# paying for many migrations in the failing case.
_COPIES = 4


def _url_for(path: Path) -> str:
    return str(sa.URL.create("sqlite+pysqlite", database=str(path)))


def _schema(path: Path) -> list[tuple[str, str, str]]:
    """A database's own objects, normalized so two can be compared."""
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "select type, name, coalesce(sql, '') from sqlite_master"
            " where name not like 'sqlite_%' order by type, name"
        ).fetchall()
    finally:
        connection.close()


def test_the_schema_is_migrated_once_per_session_not_once_per_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given four tests each asking for their own migrated database,
    When the suite serves them,
    Then the migrations run at most once.

    The count is what CI pays for, so the count is what is asserted. Order
    independence matters and is deliberate: if an earlier test already built
    the template the count is 0, and if this is the first caller it is 1 --
    both are "once per session", and the defect this pins is 4.
    """
    upgrades: list[str] = []
    real_build = build

    def counting_build(destination: Path) -> None:
        upgrades.append(destination.name)
        real_build(destination)

    # `migrated_database` derives the schema through this one function, so
    # counting it counts derivations -- no need to reach into the module's
    # cache, which is private for good reason.
    monkeypatch.setattr("tests.migrated_schema.build", counting_build)

    for index in range(_COPIES):
        directory = tmp_path / f"test-{index}"
        directory.mkdir()
        migrated_database(directory, "integration.db")

    assert len(upgrades) <= 1, (
        f"migrated_database ran {len(upgrades)} migrations to serve {_COPIES} "
        "databases; it should build the schema once and copy it. At ~538 ms "
        "each, once per test is what makes the integration suite take eleven "
        "minutes (issue #44)."
    )


def test_every_database_is_a_real_migration_and_the_copies_are_independent(
    tmp_path: Path,
) -> None:
    """Given the migrated databases the fixtures hand out,
    When their schemas are compared with a fresh migration's,
    Then they match, and a write to one is invisible to the others.

    This is the half that stops the obvious wrong fix. Reusing a single
    database would make the first test above pass and quietly break test
    isolation -- the reason these fixtures are function-scoped at all, since a
    row left behind by one test is exactly the cross-test coupling the suite
    has been bitten by before.
    """
    reference_dir = tmp_path / "reference"
    reference_dir.mkdir()
    reference = reference_dir / "reference.db"
    build(reference)

    first_dir = tmp_path / "first"
    first_dir.mkdir()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    url_first = migrated_database(first_dir, "first.db")
    url_second = migrated_database(second_dir, "second.db")

    # A copy must be the schema production runs, CHECK constraints included --
    # the reason these fixtures use alembic rather than create_all.
    assert _schema(first_dir / "first.db") == _schema(reference)
    assert _schema(second_dir / "second.db") == _schema(reference)

    engine_first = sa.create_engine(url_first)
    engine_second = sa.create_engine(url_second)
    try:
        with engine_first.begin() as connection:
            connection.execute(
                sa.text(
                    "insert into source (id, type, url, canonical_url,"
                    " schedule_policy, status, tenant_id, created_at, updated_at)"
                    " values ('src-1', 'rss', 'https://example.invalid/feed',"
                    " 'https://example.invalid/feed', '{}', 'active', :tenant,"
                    " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                ),
                {"tenant": TENANT},
            )

        with engine_second.connect() as connection:
            assert (
                connection.execute(sa.text("select count(*) from source")).scalar() == 0
            )
    finally:
        engine_first.dispose()
        engine_second.dispose()
