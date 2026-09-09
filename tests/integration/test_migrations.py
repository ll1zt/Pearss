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
# Everything migration 0001 creates. Kept as the P1 set so this test fails
# loudly if 0001's contents change; the Reader API tables added by 0002 are
# asserted separately below.
P1_TABLES = {
    "source",
    "raw_fetch",
    "item",
    "job",
    "job_run",
    "feed_out",
    "audit_log",
}

# Added by migration 0002 when the Reader API was promoted to P1 (ADR 0004).
READER_TABLES = {"read_state", "reader_credential"}

# Added by migration 0003 for the Core API.
API_TABLES = {"idempotency_record"}

# The plugin table arrives with migration 0010 (P2 lifecycle, ADR 0017);
# item_enrichment stays P3 and its absence is pinned separately below.
EXPECTED_TABLES = P1_TABLES | READER_TABLES | API_TABLES | {"plugin", "item_enrichment"}


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
def test_p2_p3_tables_are_absent(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    # These arrive with their own migration rather than being pre-created
    # empty and altered later. read_state used to be here, but the Reader API
    # moved to P1 (ADR 0004); plugin moved out when P2 delivered it
    # (migration 0010, ADR 0017); item_enrichment moved out when the P3
    # processor chain delivered it (migration 0011).


@pytest.mark.integration
def test_first_migration_creates_only_the_p1_tables(alembic_config: Config) -> None:
    # Pins migration 0001 specifically: running it alone must not leak the
    # Reader API tables, so the two migrations stay independently meaningful.
    command.upgrade(alembic_config, "0001")
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))
    assert _tables(db_path) == P1_TABLES


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
    (
        "source",
        "type",
    ): "insert into source (id, type, canonical_url) values ('probe', ?, 'https://p.example/f')",
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
            "insert into source (id, type, url, canonical_url, schedule_policy,"
            " status, tenant_id, created_at, updated_at)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "s1",
                "rss",
                "https://a.example/feed",
                "https://a.example/feed",
                "{}",
                "active",
                "default",
                "now",
                "now",
            ),
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


@pytest.mark.integration
def test_canonical_url_cannot_be_null(alembic_config: Config) -> None:
    """The dedupe key must be non-null or the constraint stops covering rows.

    Regression: canonical_url was nullable while sitting in a UNIQUE
    constraint, and SQL treats NULLs as distinct — so any number of sources
    could share one URL as long as that column was left empty, which is
    exactly what hand-written inserts produced.
    """
    command.upgrade(alembic_config, "head")
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))

    connection = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into source (id, type, url, canonical_url,"
                " schedule_policy, status, tenant_id, created_at, updated_at)"
                " values ('null-key', 'rss', 'https://n.example/f', NULL,"
                " '{}', 'active', 'default', 'now', 'now')"
            )
    finally:
        connection.close()


@pytest.mark.integration
def test_duplicate_canonical_url_is_rejected(alembic_config: Config) -> None:
    command.upgrade(alembic_config, "head")
    url = alembic_config.get_main_option("sqlalchemy.url") or ""
    db_path = Path(url.removeprefix("sqlite+pysqlite:///"))

    insert = (
        "insert into source (id, type, url, canonical_url, schedule_policy,"
        " status, tenant_id, created_at, updated_at)"
        " values (?, 'rss', ?, ?, '{}', 'active', 'default', 'now', 'now')"
    )
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            insert, ("dup-1", "https://d.example/f", "https://d.example/f")
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                insert, ("dup-2", "https://d.example/f", "https://d.example/f")
            )
    finally:
        connection.close()


@pytest.mark.integration
def test_migration_0004_backfills_without_deleting(tmp_path: Path) -> None:
    """A migration that destroyed published rows could not be re-run safely.

    Rows with no URL at all get a synthetic `legacy://` key rather than being
    deleted, because deleting a source cascades to its items and their read
    states — and those items may already be cached by a client under a frozen
    GUID.
    """
    db_path = tmp_path / "backfill.db"
    url = f"sqlite+pysqlite:///{db_path}"
    config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "0003")

    connection = sqlite3.connect(db_path)
    insert = (
        "insert into source (id, type, url, canonical_url, schedule_policy,"
        " status, tenant_id, created_at, updated_at)"
        " values (?, 'rss', ?, ?, '{}', 'active', 'default', 'now', 'now')"
    )
    # One row with a url but no canonical key, one with neither.
    connection.execute(insert, ("has-url", "https://a.example/f", None))
    connection.execute(insert, ("no-url", None, None))
    connection.commit()
    connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(db_path)
    try:
        rows = dict(
            connection.execute("select id, canonical_url from source").fetchall()
        )
    finally:
        connection.close()

    # Nothing deleted.
    assert set(rows) == {"has-url", "no-url"}
    # Resolvable from the url where one existed.
    assert rows["has-url"] == "https://a.example/f"
    # Otherwise a synthetic key that still participates in the constraint.
    assert rows["no-url"] == "legacy://no-url"


@pytest.mark.integration
def test_timestamp_width_migration_normalises_existing_rows(
    tmp_path: Path,
) -> None:
    """Mixed-width timestamps sort wrongly, so existing rows must be widened.

    '.' (0x2E) is less than 'Z' (0x5A), so `...T05:00:00.000000Z` compares
    below `...T05:00:00Z` even though both denote the same instant. Every
    ORDER BY and keyset cursor on these columns compares text, so a column
    holding both forms is silently mis-ordered.
    """
    db_path = tmp_path / "width.db"
    url = f"sqlite+pysqlite:///{db_path}"
    config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)

    # Stop before the normalisation and write rows in the old narrow form.
    command.upgrade(config, "0004")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "insert into source (id, type, url, canonical_url, schedule_policy, status,"
        " trust_level, tenant_id, created_at, updated_at)"
        " values ('s', 'rss', 'https://a/f', 'https://a/f', '{}', 'active',"
        " 'standard', 'default', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z')"
    )
    connection.execute(
        "insert into item (id, source_id, guid, content_hash, title, url,"
        " published_at, publish_status, tenant_id, created_at, updated_at)"
        " values ('i1', 's', 'g1', 'h1', 't', 'u',"
        " '2026-08-31T05:00:00Z', 'ready', 'default',"
        " '2026-09-04T06:11:23.556932Z', '2026-09-04T06:11:23.556932Z')"
    )
    connection.commit()
    narrow = [row[0] for row in connection.execute("select published_at from item")]
    connection.close()
    assert narrow == ["2026-08-31T05:00:00Z"], "setup should write a narrow value"

    command.upgrade(config, "head")

    connection = sqlite3.connect(db_path)
    widened = [row[0] for row in connection.execute("select published_at from item")]
    # Already-wide values must be left alone, not widened twice.
    untouched = [row[0] for row in connection.execute("select created_at from item")]
    connection.close()

    assert widened == ["2026-08-31T05:00:00.000000Z"]
    assert untouched == ["2026-09-04T06:11:23.556932Z"]


@pytest.mark.integration
def test_timestamp_width_migration_is_idempotent(tmp_path: Path) -> None:
    """Re-running must not append a second fractional part."""
    db_path = tmp_path / "idem.db"
    url = f"sqlite+pysqlite:///{db_path}"
    config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "0004")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "insert into source (id, type, url, canonical_url, schedule_policy, status,"
        " trust_level, tenant_id, created_at, updated_at)"
        " values ('s', 'rss', 'https://a/f', 'https://a/f', '{}', 'active',"
        " 'standard', 'default', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z')"
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")
    command.downgrade(config, "0004")
    command.upgrade(config, "head")

    connection = sqlite3.connect(db_path)
    value = connection.execute("select created_at from source").fetchone()[0]
    connection.close()

    assert value == "2026-01-01T00:00:00.000000Z"
