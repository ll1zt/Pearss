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

import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker
from uvicorn.config import Config as UvicornConfig
from uvicorn.server import Server

from adapters.raw_payload import RawPayload
from app.main import create_app, create_engine_for
from app.pipeline import normalise_and_store
from plugins.collectors.rss import collector as rss
from tests.migrated_schema import migrated_database
from tests.source_seed import seed_source

REPO_ROOT = Path(__file__).resolve().parents[2]
RSS_FIXTURES = REPO_ROOT / "src/plugins/collectors/rss/fixtures"

# A fixture credential, never a real one. Named so that a reader and static
# analysis can both tell it is synthetic.
READER_USERNAME = "reader"
READER_PASSWORD = "fixture-password-not-a-secret"

# The tenant every integration suite drives.
TENANT = "default"


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
def reader_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """A migrated app with the Reader API credential configured and one feed.

    Seeded through `normalise_and_store` rather than `store_item`: it is the
    path the pipeline actually uses and the only one that writes the body to
    the file store and attaches `content_ref`. Seeding with `store_item` left
    every item body-less, so the Reader API could never serve one and no test
    noticed.
    """
    url = migrated_database(tmp_path, "reader.db")

    monkeypatch.setenv("PEARSS_DATABASE_URL", url)
    monkeypatch.setenv("PEARSS_READER_USERNAME", READER_USERNAME)
    monkeypatch.setenv("PEARSS_READER_PASSWORD", READER_PASSWORD)
    # Without this the file store falls back to ./data/storage, which is where
    # a developer's real collected bodies live.
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))

    engine = create_engine_for(url)
    with engine.begin() as conn:
        seed_source(conn, source_id="src-1", url="https://blog.example.com/feed")

    factory = sessionmaker(engine, expire_on_commit=False)
    drafts = rss.parse(
        RawPayload(
            url="https://blog.example.com/feed",
            status_code=200,
            body=(RSS_FIXTURES / "valid.xml").read_bytes(),
            origin_id="src-1",
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

    Each application owns a collector registry now (issue #84), and building
    one starts its sandboxed children (they spawn lazily, but reading a
    manifest is the first use). The app's lifespan stops them in production,
    but suites that drive the app through `ASGITransport` never enter it.
    `reset_registries` stops every registry built in this process -- module
    default and per-application alike -- so whichever test spawned a child,
    none outlive the test. Without it they leak pipes and are reaped by the OS
    at interpreter shutdown (visible as ResourceWarnings).

    Autouse and process-wide because the leak is: the child is held by
    whichever registry the test built, and the registry instance is not
    reachable from the fixture that created the application.
    """
    yield
    from app import collector_registry

    # Installed plugins are per-registry state too (ADR 0023) and they outrank
    # the shipped collectors in URL matching, so a plugin left registered by
    # one test would claim a later test's sources.
    collector_registry.reset_registries()


# --------------------------------------------------------------------------
# Serving the app on a real port
# --------------------------------------------------------------------------
#
# Two suites need a real uvicorn (the MCP tests drive the wire protocol, so
# ASGITransport is not enough). The port MUST be picked by the OS per process:
# the same SHA can have two CI runs in flight — `push` and `pull_request` are
# separate events and the workflow has no concurrency group — and a hardcoded
# port makes the second run fail to bind. The failure mode was not a clean
# error either: uvicorn died on its thread and the client waited on a server
# that would never answer, hanging the job until the 60-minute cap (#37).


def free_port() -> int:
    """An unused loopback port, asked of the OS.

    Bind-then-release; a race with another process is theoretically possible
    and practically irrelevant here (the kernel does not hand the same port to
    two listeners, and the window is microseconds).
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ServerInThread:
    """The app served on its own thread, on a port chosen at construction.

    `__enter__` raises if the server never comes up, so a bind failure is a
    loud test error instead of a client hanging on a dead endpoint.
    """

    def __init__(self, app: Any, port: int) -> None:
        self._server = Server(
            UvicornConfig(app, host="127.0.0.1", port=port, log_level="critical")
        )
        self._port = port
        # A bind failure raises inside the server thread; without a landing
        # spot it becomes an unhandled-thread warning (or vanishes), which is
        # how a taken port turned into a silent hang instead of an error.
        self._failure: BaseException | None = None

    @property
    def port(self) -> int:
        return self._port

    def base_url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self._port}{path}"

    def _run(self) -> None:
        try:
            self._server.run()
        except BaseException as exc:  # re-raised by __enter__ in the test thread
            self._failure = exc

    def __enter__(self) -> ServerInThread:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for uvicorn's own `started` flag, not for the port to accept a
        # connection: a probe cannot tell OUR server from whatever else holds
        # that port, so it reported success when the bind had actually failed.
        # The flag is set after startup completes, which is also what the MCP
        # session manager needs before it accepts connections.
        for _ in range(200):
            if self._server.started:
                return self
            if not self._thread.is_alive():
                break
            time.sleep(0.05)
        self._server.should_exit = True
        self._thread.join(timeout=15)
        raise RuntimeError(
            f"test server never came up on 127.0.0.1:{self._port}; "
            "it failed to start (port in use?) instead of hanging the client"
        ) from self._failure

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        if getattr(self, "_thread", None) is not None:
            self._thread.join(timeout=15)


@pytest.fixture
def unreadable_model_dir(tmp_path: Path) -> Iterator[Path]:
    """A model directory this process cannot look into.

    The shape a multi-user host has: the weights are there, owned by another
    account. `Path.is_file()` raises `PermissionError` inside it rather than
    answering "no", which is what turned a documented skip into four failures
    (issue #179). Skipped when the suite runs as root, where no directory is
    unreadable and the case cannot be built.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read any directory; this case needs one it cannot")
    root = tmp_path / "models"
    model = root / "multilingual-e5-small"
    model.mkdir(parents=True)
    (model / "manifest.json").write_text('{"name": "multilingual-e5-small"}')
    root.chmod(0o000)
    try:
        yield model
    finally:
        # tmp_path's cleanup has to be able to remove it.
        root.chmod(0o700)


# The statuses a work never leaves. Shared so a test can ask "is my work done?"
# without restating the vocabulary.
TERMINAL_WORK_STATUSES = frozenset({"succeeded", "partial", "failed"})


async def sweep_until_terminal(
    factory: sessionmaker[Session], work_id: str, *, sweeps: int = 10
) -> None:
    """Run the real worker until `work_id` is terminal.

    A sweep claims the *oldest* queued work and these modules share one database,
    so one sweep may run somebody else's work. Sweeping until *this* work is
    terminal is what makes an assertion about "my work" be about my work.

    Async on purpose: the tests that need it are async, and `asyncio.run` cannot
    be called from inside a running loop.
    """
    from adapters.repository import work_by_id
    from app.work_worker import WorkWorker

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(599, text="no request belongs to this test")
        )
    )
    try:
        worker = WorkWorker(factory, client=client)
        for _ in range(sweeps):
            with factory() as session:
                work = work_by_id(session, work_id)
            if work is not None and work.status.value in TERMINAL_WORK_STATUSES:
                return
            await worker.sweep_once()
    finally:
        client.close()
    raise AssertionError(f"work {work_id!r} never reached a terminal status")


async def run_work_to_completion(
    env: dict[str, str], work_id: str, *, sweeps: int = 10
) -> None:
    """`sweep_until_terminal` for a test that holds an environment, not a factory."""
    from app.main import create_engine_for

    engine = create_engine_for(env["PEARSS_DATABASE_URL"])
    try:
        await sweep_until_terminal(
            sessionmaker(engine, expire_on_commit=False), work_id, sweeps=sweeps
        )
    finally:
        engine.dispose()
