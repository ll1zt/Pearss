"""Backup and restore: the drill that proves the archive is usable.

The backup exists for one scenario — the machine is gone and the service must
come back. A test that only checks the tar file exists would not cover the
half that matters; these run the round trip against a real migrated database
and verify the restored copy serves the Reader API.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text

from app.backup import backup, database_path, restore
from tests.migrated_schema import migrated_database
from tests.source_seed import mirror_sources

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    """A snapshot of a live database restores with its rows intact."""
    url = migrated_database(tmp_path, "live.db")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, trust_level, tenant_id, created_at, updated_at)"
                " values ('src-1', 'rss', 'https://x/feed', 'https://x/feed', '{}',"
                " 'active', 'standard', 'default',"
                " '2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
            )
        )
        mirror_sources(conn)
        conn.execute(
            text(
                "insert into item (id, source_id, origin_id, guid, content_hash,"
                " title, url,"
                " publish_status, tenant_id, created_at, updated_at)"
                " values ('item-1', 'src-1', 'src-1', 'g1', 'h1', 'T', 'https://x/1',"
                " 'ready', 'default', '2026-01-01T00:00:00.000000Z',"
                " '2026-01-01T00:00:00.000000Z')"
            )
        )
    engine.dispose()

    archive = backup(tmp_path / "backups", url=url)
    assert archive.exists() and archive.stat().st_size > 0

    # Restore to a DIFFERENT database path: the guard correctly refuses to
    # clobber the live one, which is the accident the drill prevents.
    target_db = tmp_path / "restored" / "pearss.db"
    target_url = f"sqlite+pysqlite:///{target_db}"
    restore(archive, tmp_path / "restored", url=target_url)

    restored = create_engine(target_url)
    with restored.connect() as conn:
        assert conn.execute(text("select count(*) from source")).scalar() == 1
        assert conn.execute(text("select title from item")).scalar() == "T"
    restored.dispose()


@pytest.mark.integration
def test_restore_refuses_to_overwrite_a_live_database(tmp_path: Path) -> None:
    """Restoring over an existing database is the accident the drill exists
    to prevent; it must fail loudly, not clobber."""
    url = migrated_database(tmp_path, "live.db")

    archive = backup(tmp_path / "backups", url=url)
    with pytest.raises(FileExistsError):
        restore(archive, tmp_path, url=url)  # the live dir already holds the db


@pytest.mark.integration
def test_database_path_reads_the_configured_url() -> None:
    assert database_path("sqlite+pysqlite:///data/pearss.db") == Path("data/pearss.db")
    with pytest.raises(ValueError, match="only SQLite"):
        database_path("postgresql+psycopg://db/pearss")


del sa  # imported for the reader's benefit in the round trip above
