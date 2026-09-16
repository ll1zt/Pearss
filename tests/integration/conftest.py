"""Shared fixtures for integration tests.

Extracted from the five copies of the same alembic-backed ``session_factory``
that had accumulated across this directory. Only the *unseeded* variant is
shared: several suites deliberately seed rows right after the upgrade, and
folding those differences into one fixture would have made the seeding
conditional and harder to read than the duplication it removed.

The Reader API pair (`reader_app` / `reader_client`) is here for the opposite
reason: two suites drive the same app shape -- a migrated database with one
RSS source and its items -- and only the questions asked of it differ. Two
copies of the seeding would be two chances for the world under test to drift
apart, which is the failure mode the fixtures exist to prevent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from adapters.raw_payload import RawPayload
from app.main import create_app, create_engine_for
from app.pipeline import normalise_and_store
from plugins.collectors.rss import collector as rss

REPO_ROOT = Path(__file__).resolve().parents[2]
RSS_FIXTURES = REPO_ROOT / "src/plugins/collectors/rss/fixtures"

# A fixture credential, never a real one. Named so that a reader and static
# analysis can both tell it is synthetic.
READER_USERNAME = "reader"
READER_PASSWORD = "fixture-password-not-a-secret"

# The tenant every integration suite drives.
TENANT = "default"


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


@pytest.fixture
def reader_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FastAPI]:
    """A migrated app with the Reader API credential configured and one feed.

    Seeded through `normalise_and_store` rather than `store_item`: it is the
    path the pipeline actually uses and the only one that writes the body to
    the file store and attaches `content_ref`. Seeding with `store_item` left
    every item body-less, so the Reader API could never serve one and no test
    noticed.
    """
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "reader.db")))

    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_READER_USERNAME", READER_USERNAME)
    monkeypatch.setenv("PEARSS_READER_PASSWORD", READER_PASSWORD)
    # Without this the file store falls back to ./data/storage, which is where
    # a developer's real collected bodies live.
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = create_engine_for(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, tenant_id, created_at, updated_at)"
                " values ('src-1', 'rss', 'https://blog.example.com/feed',"
                " 'https://blog.example.com/feed', '{}', 'active', 'default',"
                " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )

    factory = sessionmaker(engine, expire_on_commit=False)
    drafts = rss.parse(
        RawPayload(
            url="https://blog.example.com/feed",
            status_code=200,
            body=(RSS_FIXTURES / "valid.xml").read_bytes(),
            source_id="src-1",
        )
    )
    with Session(engine) as session:
        normalise_and_store(session, drafts, tenant_id=TENANT)
        session.commit()

    application = create_app(factory)
    try:
        yield application
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def reader_client(reader_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An async client bound straight to the reader app.

    ASGITransport is async-only, so a sync client cannot use it; binding to
    the app avoids occupying a port.
    """
    transport = httpx.ASGITransport(app=reader_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as opened:
        yield opened


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
    # Installed plugins are module state too (ADR 0023) and they outrank the
    # shipped collectors in URL matching, so a plugin left registered by one
    # test would claim a later test's sources.
    collector_registry.forget_installed_plugins()
