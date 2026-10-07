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
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from adapters.correlation import current as current_correlation_id
from adapters.deadline_transport import DeadlineTransport
from app.auth import auth_configured
from app.hardening import (
    AuthFailureRateLimitMiddleware,
    BodySizeLimitMiddleware,
    EarlyAuthMiddleware,
    SecurityHeadersMiddleware,
    env_int,
)
from app.ingest_routes import router as ingest_router
from app.items_routes import router as items_router
from app.log_setup import configure_logging, log_startup_banner
from app.mcp_guard import (
    MCPAuthMiddleware,
    MCPHeaderValidationMiddleware,
    MCPPathGuard,
    warn_if_unconfigured,
)
from app.mcp_server import build_mcp_app
from app.middleware import CorrelationAndAuditMiddleware
from app.origins_routes import router as origins_router
from app.plugins_routes import router as plugins_router
from app.reader import ReaderAuth, configure_reader_credentials
from app.reader import router as reader_router
from app.reader.operations import AmbiguousChannel
from app.scheduler import Scheduler, scheduler_enabled
from app.sources_routes import router as sources_router
from app.subscriptions_routes import router as subscriptions_router
from app.system_routes import router as system_router
from app.tenancy_check import report_other_tenants
from app.work_routes import router as work_router

LOGGER = logging.getLogger(__name__)


DEFAULT_DATABASE_URL = "sqlite+pysqlite:///./data/pearss.db"


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


def create_engine_for(url: str) -> Any:
    """Build an engine with the pragmas this application depends on.

    Exists as its own function because the pragmas are not optional and are
    easy to forget: `sa.create_engine(url)` looks like it does the same thing
    and silently skips every one of them. Anything that needs an engine --
    the app, the migration tests, a test fixture -- should come through here so
    the connection setup has a single definition.
    """
    engine = create_engine(
        url,
        # WAL lets readers (NNW pulls, MCP queries) proceed while the
        # enrichment worker or the scheduler writes - the default journal
        # (delete) makes every writer take an exclusive lock and was the
        # main concurrency hazard for parallel collection.
        connect_args={"timeout": 30.0},
    )

    if not url_is_sqlite(url):
        return engine

    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # SQLite ignores every ON DELETE clause unless this is set, and its
        # default is off. Without it the schema's own declarations -- the
        # CASCADEs from item to read_state and item_enrichment, and from
        # source to item and raw_fetch -- were inert, so deleting a parent
        # left children pointing at a row that no longer existed. Deletion
        # (ADR 0021) relies on those cascades to carry a deleted item's
        # consumption state and processor products away with it.
        #
        # Per connection, not per database, which is why it runs in the
        # connect hook: setting it once at startup would leave every later
        # connection unenforced.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_session_factory(url: str | None = None) -> sessionmaker[Session]:
    """Build the session factory, creating the database directory if needed."""
    resolved = url or database_url()
    _ensure_parent_directory(resolved)
    return sessionmaker(create_engine_for(resolved), expire_on_commit=False)


def url_is_sqlite(url: str) -> bool:
    """Whether the database URL addresses a SQLite file."""
    return url.startswith("sqlite")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the shared HTTP client for the process lifetime.

    A client per request would re-do the TLS handshake and discard the
    connection pool on every fetch.

    Sync rather than async: the collection handlers are `def` so that
    SQLAlchemy work runs in a threadpool instead of blocking the loop, and a
    threadpool task cannot await on an AsyncClient. Matching the client to the
    handler's execution model is what keeps both honest.
    """
    configure_logging()
    log_startup_banner()

    # connect covers the TLS handshake too, so it needs headroom; 5s dropped
    # about a third of requests to slow origins. pool is the wait for a free
    # connection, not the time to establish one, so it gets the total budget.
    timeout = httpx.Timeout(connect=15.0, read=15.0, write=15.0, pool=60.0)
    client = httpx.Client(
        transport=DeadlineTransport(),
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
    )
    app.state.http_client = client

    # Entered before the yield so the session manager is running by the time
    # the port accepts traffic.

    # The MCP surface is a Starlette sub-app mounted into this one. Mounting
    # does not run the child's lifespan — Starlette only runs the top app's —
    # and the StreamableHTTP session manager must be initialised there or every
    # MCP request fails. So the child lifespan is entered explicitly here and
    # its exit is tied to ours, which keeps shutdown ordering correct.
    # The inner Starlette app owns the session-manager lifespan; the auth
    # wrapper only fronts requests. Collapsing the two would look tidier and
    # break `router.lifespan_context` below, which is what makes MCP sessions
    # work at all.
    #
    # Built by create_app (it needs the app to read routes off) and read back
    # here by name: the session manager must be entered inside this lifespan,
    # which is the only one Starlette runs.
    mcp_inner = app.state.mcp_app

    # The application's own collector registry: the scheduler, the routes and
    # the lifespan all use this one, so two applications in one process do not
    # share collectors, sandboxed children or installed plugins (issue #84).
    registry = app.state.collector_registry

    scheduler = Scheduler(
        app.state.session_factory,
        client=client,
        tick_seconds=float(os.environ.get("PEARSS_TICK_SECONDS", "60")),
        registry=registry,
    )
    app.state.scheduler = scheduler

    # The enrichment worker runs beside the scheduler: queued processor jobs
    # (P3 chains) and gate promotion. Started/stopped with the app; the
    # PEARSS_SCHEDULER_ENABLED switch governs both, one flag per deployment.
    from app.enrichment_worker import EnrichmentWorker, default_processors

    worker = EnrichmentWorker(
        app.state.session_factory,
        processors=default_processors(),
        watch=scheduler.liveness,
    )
    app.state.enrichment_worker = worker
    # Each loop watches the other: a loop cannot report its own death, so
    # whichever is still ticking is the one that says the other stopped
    # (issue #61).
    scheduler.watch(worker.liveness)

    # The work loop owns requested batches (ADR 0046): a submitted work is a job,
    # and a job needs an owner. It shares the deployment's egress client (the
    # timeouts, size caps and SSRF guard live there) and the same
    # PEARSS_SCHEDULER_ENABLED switch as the other two loops.
    from app.work_worker import WorkWorker

    work_worker = WorkWorker(
        app.state.session_factory,
        client=client,
        registry=registry,
        watch=scheduler.liveness,
    )
    app.state.work_worker = work_worker

    async with mcp_inner.router.lifespan_context(mcp_inner):
        try:
            # Restore approved installations before egress wiring or starting jobs.
            registry.restore_installed_plugins(app.state.session_factory)
            # Names the assumption and checks it against the data (issue
            # #89): the column exists on every table, so a second tenant is
            # representable, and every read would silently stop returning it.
            report_other_tenants(app.state.session_factory)

            # The sandboxed collectors' egress is host-proxied (ADR 0017): the
            # child's reverse http calls ride back here and run through the same
            # shared client the scheduler uses - which is where the SSRF
            # private-address guard (ADR 0016) and the fetch-timeout/size bounds
            # live. Without this injection every proxied request is refused, so
            # the wiring is load-bearing, not cosmetic.
            from app.plugin_runtime import HostClientTransport

            transport = HostClientTransport(client)
            # Registered, not just applied: an installed plugin's child is built
            # later (ADR 0023), and `_installed_registration` reads this back to
            # give it the same egress path. Applying it to the current children is
            # the other half.
            registry.for_each_sandboxed_collector(
                lambda name, child: registry.set_http_transport(name, transport)
            )
            registry.sync_plugin_table(app.state.session_factory)
            if scheduler_enabled():
                await scheduler.start()
                await worker.start()
                await work_worker.start()
            else:
                LOGGER.info("scheduler disabled by PEARSS_SCHEDULER_ENABLED")
            yield
        finally:
            await scheduler.stop()
            await worker.stop()
            await work_worker.stop()
            # Sandboxed children are spawned lazily and hold real processes
            # with pipes; without this they outlive a graceful shutdown and
            # are only reaped when the OS tears the host down. Stopped before
            # the client closes because a live child may still be mid-call.
            registry.for_each_sandboxed_collector(lambda name, child: child.stop())
            client.close()


# Keys that carry non-serialisable or client-useless values.
_DROPPED_ERROR_KEYS = frozenset({"ctx", "url", "input"})


def _safe_errors(errors: object) -> list[dict[str, object]]:
    """Reduce pydantic validation errors to JSON-safe, client-useful entries.

    `loc` and `msg` are what a caller acts on. `ctx` holds the raised exception
    object, `url` a documentation link, and `input` the rejected value — which
    for a credential-shaped field would echo user data into a response body.
    """
    safe: list[dict[str, object]] = []
    if not isinstance(errors, (list, tuple)):
        return safe
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        reduced: dict[str, object] = {}
        for key, value in entry.items():
            if key in _DROPPED_ERROR_KEYS:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                reduced[key] = value
            elif isinstance(value, (list, tuple)):
                reduced[key] = [
                    item
                    for item in value
                    if isinstance(item, (str, int, float, bool)) or item is None
                ]
        safe.append(reduced)
    return safe


def docs_enabled() -> bool:
    """Whether Swagger/redoc/openapi.json are served.

    Swagger is the documented fallback admin UI (spec 1.2), so it stays on by
    default; on a public deployment the tunnel routes only the Reader API and
    this flag turns the fallback off entirely (ADR 0016), because an
    unauthenticated /openapi.json hands an attacker the full API surface.
    """
    return os.environ.get("PEARSS_DOCS_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


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
        docs_url="/docs" if docs_enabled() else None,
        redoc_url="/redoc" if docs_enabled() else None,
        openapi_url="/openapi.json" if docs_enabled() else None,
    )
    app.state.session_factory = session_factory or create_session_factory()

    # One collector registry per application (issue #84). Built before the MCP
    # sub-app, whose core client reads it off `app.state`. Constructing it is
    # cheap: the registrations (and the sandboxed children) are built lazily on
    # first use.
    from app.collector_registry import CollectorRegistry

    app.state.collector_registry = CollectorRegistry()

    # The Reader API is a compatibility surface with its own authentication,
    # mounted separately per docs/api/style-guide.md rather than mixed into
    # /v1. Credentials come from the environment, so this is a no-op when they
    # are unset and the endpoints simply reject every login.
    app.state.reader_auth = ReaderAuth(app.state.session_factory)
    configure_reader_credentials(app.state.session_factory)
    app.include_router(reader_router)
    warn_if_unconfigured()
    app.include_router(sources_router)
    app.include_router(subscriptions_router)
    app.include_router(origins_router)
    app.include_router(ingest_router)
    app.include_router(items_router)
    app.include_router(system_router)
    app.include_router(plugins_router)
    app.include_router(work_router)

    @app.exception_handler(AmbiguousChannel)
    def on_ambiguous_channel(request: Request, exc: AmbiguousChannel) -> JSONResponse:
        """A legacy channel id that names an origin with several subscriptions.

        Refused rather than guessed (issue #370, AC-344): the split made a
        channel a subscription, so an origin id stops being a channel id the
        moment that origin has more than one -- and answering with one of them
        would show the client a different channel's items under the id it named.
        """
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "ambiguous_channel",
                    "message": str(exc),
                    "error_class": None,
                    "suggestion": (
                        "Name the subscription id; subscription/list gives the "
                        "current ones."
                    ),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    def on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Render request-validation failures in the documented envelope.

        Without this, a malformed body returns Starlette's default shape while
        every other failure returns the envelope — two error formats on one
        API, which clients end up branching on.
        """
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request body did not match the schema.",
                    "error_class": None,
                    "suggestion": "Correct the listed fields and retry.",
                    # Filtered to JSON-safe scalars rather than passed through:
                    # pydantic puts the raised ValueError object in `ctx` and a
                    # source reference in `url`, and serialising either raises.
                    # Handled wrong, the error handler turns every validation
                    # failure into a 500 — a worse bug than the one it reports.
                    "details": _safe_errors(exc.errors()),
                    "correlation_id": current_correlation_id(),
                }
            },
        )

    # Added after the routes so the audit record can name the matched route
    # rather than only the path. Correlation ids must still be set before any
    # endpoint runs, which is why this is middleware and not a dependency.
    app.add_middleware(
        CorrelationAndAuditMiddleware, session_factory=app.state.session_factory
    )
    # Public-exposure hardening (ADR 0016). Starlette wraps in reverse
    # registration order, so the rate limiter ends up outermost: a blocked
    # address is refused before the body limit, the early auth check, the
    # audit write, or any route runs, and 413s never reach the application
    # either. EarlyAuth sits just inside the rate limiter so an unauthenticated
    # request is refused before FastAPI reads or validates the body - body
    # parsing is endpoint work, and endpoint work is for authenticated
    # callers only.
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=env_int("PEARSS_MAX_BODY_BYTES", 1024 * 1024)
    )
    app.add_middleware(EarlyAuthMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        AuthFailureRateLimitMiddleware,
        max_failures=env_int("PEARSS_AUTH_RATELIMIT_MAX", 50),
        window_seconds=env_int("PEARSS_AUTH_RATELIMIT_WINDOW_SECONDS", 300),
        block_seconds=env_int("PEARSS_AUTH_RATELIMIT_BLOCK_SECONDS", 900),
    )

    if not auth_configured():
        # Fail closed: with no token every Core API request is refused. Saying
        # so at startup is the difference between a confusing 401 and a
        # configuration step that is simply missing.
        LOGGER.warning("PEARSS_API_TOKEN unset; the Core API will reject every request")

    @app.get("/healthz", tags=["ops"])
    def healthz() -> Any:
        """Liveness probe: no dependency on the database.

        The HTTP process being alive says nothing about the background
        loops (A07): a scheduler killed by an escaped error used to leave
        this returning ok forever. Started workers report their task
        liveness here; a dead loop degrades the answer to 503 with the
        offender named, so monitoring sees the degradation instead of
        quietly stopped collection.
        """
        degraded: list[str] = []
        workers = (
            getattr(app.state, "scheduler", None),
            getattr(app.state, "enrichment_worker", None),
            getattr(app.state, "work_worker", None),
        )
        for worker in workers:
            if worker is None:
                continue
            alive, who = worker.liveness()
            if not alive:
                degraded.append(who)
        if degraded:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "details": degraded},
            )
        return {"status": "ok"}

    # The MCP sub-app. Mounted at the root because the SDK app routes on
    # `/mcp` itself: mounting it under `/mcp` nested the paths into
    # `/mcp/mcp`, leaving the advertised `/mcp` as a 307 that clients which
    # POST without following redirects reported as a connection failure.
    #
    # MCPPathGuard is what makes a root mount safe -- it hands every non-MCP
    # path back as 404, so unknown URLs keep their own answer instead of
    # inheriting this layer's (an unconfigured token used to turn them all
    # into 503, and it swallowed the health probe).
    #
    # Order: guard first (cheapest, and it owns path membership), then auth so
    # an unauthenticated peer never reaches the body-reading layer, then
    # header/body agreement.
    mcp_inner = build_mcp_app(app)
    app.state.mcp_app = mcp_inner
    app.mount(
        "/",
        MCPPathGuard(
            MCPAuthMiddleware(MCPHeaderValidationMiddleware(mcp_inner)),
        ),
    )

    return app


_APPLICATION_KEY = "app"


def __getattr__(name: str) -> Any:
    """Build the ASGI application on first attribute access (PEP 562).

    The module used to end with `app = create_app()`, so *importing* it
    built the engine, created the database directory, and wired the full
    middleware stack (issue #21) -- a cost every importer paid, and one
    that multiplies with multi-process or per-tenant deployment. Uvicorn's
    `app.main:app` string form only needs the attribute to exist when it
    is looked up, so the build moves to that lookup: `import app.main` is
    now free, and `app.main.app` builds exactly once per process and is
    cached in the module namespace (subsequent reads hit the attribute,
    not this hook).
    """
    if name == _APPLICATION_KEY:
        application = create_app()
        globals()[_APPLICATION_KEY] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
