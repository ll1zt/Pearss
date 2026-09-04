"""FastAPI application assembly.

The top layer wires components together and carries no domain rules: what may
appear in a feed is decided in core and applied by the adapters, not here.

One notable constraint: this is P1, so there is no processor chain yet. Items
become `ready` as soon as they are stored, because with an empty pipeline the
gate has nothing to wait for. The gate itself stays in the query path, so
introducing a chain in P3 changes only the promotion step, not the output.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import (
    items_ready_for_feed,
    mark_published,
)
from adapters.rss_out import FeedMeta, render_feed
from app.pipeline import bodies_for, normalise_and_store
from app.reader_routes import ReaderAuth, configure_reader_credentials
from app.reader_routes import router as reader_router
from core.models import ItemDraft

LOGGER = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./data/pearss.db"
TENANT = "default"


def database_url() -> str:
    """Resolved from the environment, never hardcoded with credentials."""
    return os.environ.get("PEARSS_DATABASE_URL", DEFAULT_DATABASE_URL)


def _ensure_parent_directory(url: str) -> None:
    """Create the directory for a file-backed SQLite database if missing."""
    if not url.startswith("sqlite"):
        return
    _, _, path = url.partition("///")
    if not path or path.startswith(":memory:"):
        return
    parent = Path(path).expanduser().parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)


def create_session_factory(url: str | None = None) -> sessionmaker[Session]:
    """Build the session factory, creating the database directory if needed."""
    resolved = url or database_url()
    _ensure_parent_directory(resolved)
    engine = create_engine(resolved)
    return sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the shared httpx client for the process lifetime.

    A client per request would re-do the TLS handshake and discard the
    connection pool on every fetch.
    """
    # pool is the wait for a free connection, not the time to establish one:
    # tying it to the connect timeout made the first request give up mid-TLS-
    # handshake on a slow origin. It gets the total budget, matching
    # adapters/http.py.
    # connect covers the TLS handshake too, so it needs the same headroom as
    # adapters/http.py; 5s dropped about a third of requests to slow origins.
    timeout = httpx.Timeout(connect=15.0, read=15.0, write=15.0, pool=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        app.state.http_client = client
        yield
    # Exiting the context manager closes the client.


def create_app(session_factory: sessionmaker[Session] | None = None) -> FastAPI:
    """Build the application.

    The session factory is injected so tests can point at a temporary database
    without touching the environment.
    """
    app = FastAPI(
        title="Pearss",
        description="Single-user, agent-first RSS+ system.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.session_factory = session_factory or create_session_factory()

    # The Reader API is a compatibility surface with its own authentication,
    # mounted separately per docs/api/style-guide.md rather than mixed into
    # /v1. Credentials come from the environment, so this is a no-op when they
    # are unset and the endpoints simply reject every login.
    app.state.reader_auth = ReaderAuth(app.state.session_factory)
    configure_reader_credentials(app.state.session_factory)
    app.include_router(reader_router)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        """Liveness probe with no dependency on the database."""
        return {"status": "ok"}

    # Declared `def`, not `async def`, on purpose. The work is synchronous
    # (SQLAlchemy and lxml), so FastAPI runs it in a threadpool and the event
    # loop stays free. Marking it `async def` would make every request block
    # the loop and serialise the server — verified by
    # test_concurrent_feed_requests_do_not_serialise, which fails with a
    # blocking call under `async def` and passes under `def`.
    @app.get("/feeds/{feed_id}.xml", tags=["feeds"])
    def read_feed(feed_id: str) -> Response:
        """Render one RSS feed.

        Only items the gate permits are included. Rendering also records
        first publication, which freezes those items' content — so this
        endpoint is not side-effect free, and a client's first poll is the
        moment an item becomes permanent.
        """
        if feed_id != "default":
            raise HTTPException(status_code=404, detail="unknown feed")

        with app.state.session_factory() as session:
            items = items_ready_for_feed(session)
            now = datetime.now(UTC)
            published = mark_published(session, items, at=now)
            session.commit()

            if published:
                LOGGER.info("first publication recorded for %d item(s)", published)

            body = render_feed(
                FeedMeta(
                    title="Pearss",
                    link="/",
                    description="All collected items.",
                ),
                items,
                bodies_for(session, items),
            )

        return Response(
            content=body,
            media_type="application/rss+xml; charset=utf-8",
        )

    @app.post("/internal/items", tags=["internal"])
    def ingest_draft(draft: ItemDraft) -> dict[str, str]:
        """Normalise and store a draft.

        Internal and unauthenticated for now: authentication and the audit
        middleware are part of the P1 control-plane work, and this endpoint
        exists so the pipeline can be exercised end to end before then.
        """
        with app.state.session_factory() as session:
            ingested = normalise_and_store(session, [draft])[0]

        item = ingested.result.item
        outcome = ingested.outcome
        return {
            "id": item.id,
            "guid": item.guid,
            "action": outcome.action.value,
            "reason": outcome.reason,
        }

    return app


app = create_app()
