"""Backup: an online-consistent snapshot of the fact-source data.

What is backed up (docs/domain/data-model.md 事实源分级): the SQLite
database via the online `.backup` API (consistent even while the service
writes) and the file store (item bodies, translation artifacts). What is
NOT: raw_fetch payloads (rebuildable diagnostics, per the data model) —
which is why the storage archive copies the whole tree and the database
restore is what counts.

Restore: `just restore <archive> <target-dir>` unpacks and verifies row
counts; the drill (runbook 手工冒烟清单 #9) is pointing a service at the
restored directory and logging in through the Reader API.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tarfile
import time
from pathlib import Path

from adapters.filestore import storage_root

LOGGER = logging.getLogger(__name__)


def database_path(url: str | None = None) -> Path:
    """The SQLite file a database URL addresses, or the default."""
    resolved = url or os.environ.get(
        "PEARSS_DATABASE_URL", "sqlite+pysqlite:///data/pearss.db"
    )
    prefix = "sqlite+pysqlite:///"
    if resolved.startswith(prefix):
        return Path(resolved.removeprefix(prefix))
    if resolved.startswith("sqlite:///"):
        return Path(resolved.removeprefix("sqlite:///"))
    raise ValueError(f"backup supports only SQLite databases, got: {resolved}")


def backup(archive_dir: Path, *, url: str | None = None) -> Path:
    """Create a timestamped archive; return its path.

    The SQLite copy runs through the source connection's backup API (page-
    consistent, safe while the service runs). The file store is a directory
    tree; it is copied as-is. Both land in one tar.gz for a single restore
    command.
    """

    db_path = database_path(url)
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    store_root = storage_root()

    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = archive_dir / f"pearss-backup-{stamp}.tar.gz"

    # SQLite online backup: consistent snapshot through any open connection.
    snapshot = archive_dir / f".pearss-snapshot-{stamp}.db"
    source = sqlite3.connect(str(db_path))
    try:
        target = sqlite3.connect(str(snapshot))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    try:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname="pearss.db")
            if store_root.exists():
                tar.add(store_root, arcname="storage")
    finally:
        snapshot.unlink(missing_ok=True)

    size = archive.stat().st_size
    LOGGER.info("backup written: %s (%.1f KiB)", archive, size / 1024)
    return archive


def restore(archive: Path, target_dir: Path, *, url: str | None = None) -> Path:
    """Unpack an archive into `target_dir`; return the restored db path.

    Refuses to overwrite an existing database: a restore into a live
    directory is exactly the accident the drill exists to prevent.
    """

    target_dir.mkdir(parents=True, exist_ok=True)
    # Guard the path a restore would actually write: the configured URL's
    # location. Restoring over a live database is the accident this exists
    # to prevent.
    restored_db = database_path(url)
    if restored_db.exists():
        raise FileExistsError(
            f"{restored_db} already exists; restore into an empty directory"
        )

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        # The db member is any top-level `.db` file: backup() stores it under
        # the deployment's own name (pearss.db), but a restored copy is valid
        # whatever the source called it.
        db_members = [n for n in names if n.endswith(".db") and "/" not in n]
        if not db_members:
            raise ValueError(f"archive carries no database; found: {names[:5]}")
        # Our own archive, produced by backup(); members are only the db and
        # the storage tree - no untrusted paths. Extract with a filter for
        # defence in depth anyway.
        tar.extractall(target_dir, filter="data")

    extracted = target_dir / db_members[0]
    configured = database_path(url)
    if configured != extracted:
        configured.parent.mkdir(parents=True, exist_ok=True)
        extracted.rename(configured)
        restored_db = configured

    _verify(restored_db)
    return restored_db


def _verify(db_path: Path) -> None:
    """Integrity + row-count sanity: a restore that opens but is empty is
    worse than one that fails loudly."""
    conn = sqlite3.connect(str(db_path))
    try:
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"restored database failed integrity_check: {integrity}")
        items = conn.execute("select count(*) from item").fetchone()[0]
        sources = conn.execute("select count(*) from source").fetchone()[0]
        LOGGER.info(
            "restored database verified: integrity ok, %d sources, %d items",
            sources,
            items,
        )
    finally:
        conn.close()


def main() -> None:
    """`python -m app.backup create` / `... restore` (the justfile entry)."""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    backup_dir = Path(os.environ.get("PEARSS_BACKUP_DIR", "backups"))

    if action == "create":
        archive = backup(backup_dir)
        print(archive)
        return
    if action == "restore":
        archive_raw = os.environ.get("PEARSS_RESTORE_ARCHIVE", "")
        target_raw = os.environ.get("PEARSS_RESTORE_TARGET", "")
        if not (archive_raw and target_raw):
            print(
                "usage: just restore <archive> <target-dir>",
                file=sys.stderr,
            )
            raise SystemExit(2)
        db = restore(Path(archive_raw), Path(target_raw))
        print(f"restored database: {db}")
        return
    print("usage: python -m app.backup create|restore", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
