"""Migration tests.

docs/guides/testing.md requires every migration to be verified in both
directions on an empty database. These run against a temporary SQLite file,
never the development database.

They also assert that the enum taxonomies are enforced by the database rather
than only by the application: AGENTS.md mandates the five ErrorClass values,
and a constraint the database does not enforce is a rule that only holds until
something writes around the app.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent
EXPECTED_TABLES = {
    "source",
    "raw_fetch",
    "item",
    "job",
    "job_run",
    "feed_out",
    "audit_log",
}


@pytest.fixture
def alembic_config(tmp_path: Path) -> Config:
    """Alembic config pointed at a throwaway database."""
    db_path = tmp_path / "migration_test.db"
    config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    return config


@pytest.mark.integration
def test_migrate_creates_a_missing_data_directory(tmp_path: Path) -> None:
    """A fresh clone has no data/ directory, and SQLite will not create one.

    Regression test: `git clone && just migrate` used to fail with "unable to
    open database file" because the directory is gitignored and therefore
    absent. Using a nested path here matters — tmp_path already exists, so a
    database directly inside it would hide the bug.
    """
    missing_dir = tmp_path / "absent" / "nested"
    db_path = missing_dir / "pearss.db"
    assert not missing_dir.exists()

    config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")

    command.upgrade(config, "head")

    assert db_path.exists()
    assert _tables(db_path) == EXPECTED_TABLES


def _tables(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows} - {"alembic_version"}


@pytest.mark.integration
def test_upgrade_head_creates_all_p1_tables(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))
    assert _tables(db_path) == EXPECTED_TABLES


@pytest.mark.integration
def test_downgrade_base_removes_every_table(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))
    assert _tables(db_path) == set()


@pytest.mark.integration
def test_upgrade_downgrade_upgrade_round_trip(alembic_config: Config) -> None:
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))

    command.upgrade(alembic_config, "head")
    first = _tables(db_path)

    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    assert _tables(db_path) == first


@pytest.mark.integration
def test_p3_tables_are_absent(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))
    tables = _tables(db_path)
    # These arrive with their own migration in P2/P3 rather than being
    # pre-created empty and altered later.
    assert "item_enrichment" not in tables
    assert "read_state" not in tables
    assert "plugin" not in tables


# Table and column names are looked up from this mapping rather than being
# interpolated into SQL: SQL identifiers cannot be parameterised, so the safe
# way to vary them is an allowlist. That also keeps ruff's S608 quiet for a
# real reason rather than by suppression.
PROBE_STATEMENTS: dict[tuple[str, str], str] = {
    (
        "raw_fetch",
        "error_class",
    ): "insert into raw_fetch (id, error_class) values ('probe', ?)",
    (
        "item",
        "publish_status",
    ): "insert into item (id, publish_status) values ('probe', ?)",
    ("source", "type"): "insert into source (id, type) values ('probe', ?)",
    ("job_run", "status"): "insert into job_run (id, status) values ('probe', ?)",
}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("target", "bogus"),
    [
        (("raw_fetch", "error_class"), "not_a_real_class"),
        (("item", "publish_status"), "sort_of_published"),
        (("source", "type"), "carrier_pigeon"),
        (("job_run", "status"), "almost_done"),
    ],
)
def test_database_rejects_out_of_taxonomy_values(
    alembic_config: Config,
    target: tuple[str, str],
    bogus: str,
) -> None:
    command.upgrade(alembic_config, "head")
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))

    statement = PROBE_STATEMENTS[target]
    connection = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, (bogus,))
    finally:
        connection.close()


@pytest.mark.integration
def test_error_class_taxonomy_is_accepted(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "insert into source (id, type, schedule_policy, status, tenant_id,"
            " created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?)",
            ("s1", "rss", "{}", "active", "default", "now", "now"),
        )
        for index, error_class in enumerate(
            ["network", "parse", "structure_changed", "blocked", "auth"]
        ):
            connection.execute(
                "insert into raw_fetch (id, source_id, status, error_class,"
                " fetched_at, correlation_id) values (?, ?, ?, ?, ?, ?)",
                (f"f{index}", "s1", "failed", error_class, "now", f"c{index}"),
            )
        connection.commit()
    finally:
        connection.close()
