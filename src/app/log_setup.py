"""Logging bootstrap: one idempotent entry point, correlation id on every line.

Two problems this module closes (issue #290). First, the bootstrap used to live
in ``app.main`` and call ``logging.basicConfig``, which does nothing when the
root logger already has a handler -- so under uvicorn, or in a second entry
point, the format silently did not apply, and repeated calls stacked handlers.
Second, ``adapters.correlation`` promised that an id "ties one caller-visible
request to the log lines, audit rows and job runs", but no log line ever carried
it: the journal and the audit trail could not be joined. A ``Filter`` on the
handler reads the active id and stamps every record, so the promise is kept at
the one place all lines pass through.

The format string is the contract: the ``%(correlation_id)s`` placeholder is
filled by :class:`CorrelationIdFilter`, and a filter that stopped running would
fail rendering rather than quietly drop the field.

Lives in ``app`` rather than ``adapters``: the banner reports app-level
configuration (tick, pacing) whose defaults belong to ``app.fetch_pacing``, and
``app`` is the layer allowed to know about both.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO
from urllib.parse import urlsplit

from adapters import correlation

LOGGER = logging.getLogger(__name__)

# Unbuffered stderr is what the service journal reads; the fields are ordered
# so a human scanning the journal sees time, severity, the request it belongs
# to, and the logger that spoke.
LOG_FORMAT = "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"

# Marks the handler this module owns, so a second call replaces it instead of
# adding another. An attribute rather than a module global because the root
# logger is process state that outlives any import.
_HANDLER_MARKER = "_pearss_log_handler"


class CorrelationIdFilter(logging.Filter):
    """Stamp every record with the correlation id active when it was emitted.

    ``correlation.current()`` generates an id when none is set, deliberately:
    a scheduler tick or a CLI run outside any request still produces traceable
    lines rather than a blank field.

    An id already on the record wins. A forwarded processor line carries the
    id captured when its child was spawned (issue #298); stamping it with
    ``current()`` would replace that with whatever context the host happens to
    be re-logging from, which may be none at all.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "correlation_id", None) is None:
            record.correlation_id = correlation.current()
        return True


def _writes_to_the_journal(handler: logging.Handler) -> bool:
    """Whether `handler` is another writer to the stream this module logs to.

    Identity, not equality: `sys.stderr` is the journal, and a handler holding
    it is a second writer to it.
    """
    return (
        isinstance(handler, logging.StreamHandler)
        and getattr(handler, "stream", None) is sys.stderr
    )


def configure_logging(
    *, level: str | None = None, stream: TextIO | None = None
) -> None:
    """Configure the root logger; safe to call more than once.

    Idempotent by construction: an earlier handler installed by this function
    is removed before the new one is added, so repeated calls (uvicorn reload,
    a CLI that imports the app, a test) converge on one handler and one line
    per record. Foreign handlers are left alone -- pytest's capture, another
    library's -- which ``logging.basicConfig(force=True)`` would not do; the
    exception is another writer to *this* stream, which would print every
    record a second time in a second format (AC-308).
    """
    resolved_level = (level or os.environ.get("PEARSS_LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger()
    root.setLevel(resolved_level)

    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False) or _writes_to_the_journal(
            existing
        ):
            # A second writer to the journal is the double line this function
            # exists to prevent: a library that called
            # `logging.config.fileConfig` (alembic's env.py does) leaves a
            # stderr handler behind, and it prints every record again, in its
            # own format. Handlers writing somewhere else -- pytest's capture,
            # a file -- are the embedder's business and stay (AC-308).
            root.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(CorrelationIdFilter())
    root.addHandler(handler)

    # uvicorn's access log is replaced by the application's own line (issue
    # #294): it carries no correlation id and no route template, and two lines
    # per request would double the journal. `uvicorn.access` has its own
    # handler and `propagate=False`, so its level is the only lever; the app
    # middleware emits the line instead.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def describe_database_url(url: str) -> str:
    """The database location without anything that could be a credential.

    For SQLite the path *is* the location, and there is no authority to hide.
    For a network URL the authority (``user:password@host:port``) and the query
    string (``?sslmode=...``, sometimes a token) are dropped: only the scheme
    and the database path are safe to print.

    Parsed with :func:`urllib.parse.urlsplit` rather than splitting on ``/``:
    a URL with no path has no path segment, and the old fallback took the whole
    authority -- password included -- and printed it as the path.
    """
    parts = urlsplit(url)
    if not parts.scheme:
        return "<unparseable>"
    if parts.scheme.startswith("sqlite"):
        return url.split("?", 1)[0]
    return f"{parts.scheme}:///{parts.path.lstrip('/')}"


def log_startup_banner() -> None:
    """Print the effective configuration once, so startup is observable.

    Without this the log level, database location, tick and pacing are only
    discoverable by reading ``.env`` and the environment. Secrets are excluded
    by construction: the database URL goes through :func:`describe_database_url`
    and nothing else printed here is a credential.
    """
    # Imported inside the function: ``app.main`` imports this module, and
    # ``app.fetch_pacing`` builds a pacer from the environment on demand.
    from app.fetch_pacing import HostPacer
    from app.main import database_url
    from app.scheduler import scheduler_enabled

    pacer = HostPacer.from_env()
    LOGGER.info(
        "pearss starting: level=%s db=%s tick=%ss scheduler=%s "
        "fetch_min_interval=%ss fetch_max_per_host=%s",
        os.environ.get("PEARSS_LOG_LEVEL", "INFO").upper(),
        describe_database_url(database_url()),
        os.environ.get("PEARSS_TICK_SECONDS", "60"),
        "on" if scheduler_enabled() else "off",
        pacer.min_interval,
        pacer.max_per_sweep,
    )
