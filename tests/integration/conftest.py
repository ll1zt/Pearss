"""Shared fixtures for integration tests.

Extracted from the five copies of the same alembic-backed ``session_factory``
that had accumulated across this directory. Only the *unseeded* variant is
shared: several suites deliberately seed rows right after the upgrade, and
folding those differences into one fixture would have made the seeding
conditional and harder to read than the duplication it removed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_engine_for

REPO_ROOT = Path(__file__).resolve().parents[2]


def migrated_database(tmp_path: Path, filename: str) -> str:
    """Run the real migrations against `tmp_path/filename` and return its URL.

    Using alembic rather than `Base.metadata.create_all` is the point: the
    schema under test is then the one production runs, including the CHECK
    constraints and CASCADE rules that `create_all` would also produce only if
    someone remembered to keep the two in sync.
    """
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / filename)))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return url


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A session factory backed by a migrated, empty, temporary database."""
    engine = create_engine_for(migrated_database(tmp_path, "integration.db"))
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def migrated_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A second, independent migrated database.

    Separate from `session_factory` so a test that needs two databases --
    one for the request, one for assertions -- does not have to reach for a
    relative import or rebuild the migration machinery.
    """
    engine = create_engine_for(migrated_database(tmp_path, "concurrency.db"))
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _reap_sandboxed_children() -> Iterator[None]:
    """Stop any sandboxed collector a test spawned.

    `collector_registry` holds `_SANDBOX_CHILDREN` in a module global and the
    children spawn lazily, so any test that builds the registry -- directly, or
    indirectly by constructing an app -- leaves live subprocesses behind. The
    app's lifespan stops them in production, but suites that drive the app
    through `ASGITransport` never enter it. Without this teardown those
    children outlive the suite, leaking pipes and finally being reaped by the
    OS at interpreter shutdown (visible as ResourceWarnings).

    Autouse and module-global because the leak is: whichever test spawned a
    child, the registry still holds it at the end of any test.
    """
    yield
    from app import collector_registry

    collector_registry.for_each_sandboxed_collector(lambda name, child: child.stop())
