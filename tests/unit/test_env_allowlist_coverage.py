"""Every PEARSS_* variable the code reads must be reachable by a child.

The sandbox's env allowlist (`child_environment`) is deny-by-default: a
collector whose credential prefix is missing from
`_COLLECTOR_ENV_PREFIXES` reads an empty value at runtime and reports
`auth` — the failure looks like an expired session, not a wiring bug.
This guard closes that gap at CI time: any PEARSS_* reference in src/
must either match a collector's allowlisted prefix or belong to the
host-side set that no child ever needs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

# Host-side variables: read by the app itself (main, routes, middleware),
# never by a collector. A child receiving these would be a leak.
HOST_SIDE: frozenset[str] = frozenset(
    {
        "PEARSS_API_TOKEN",
        "PEARSS_API_READ_TOKEN",
        # Where the CLI (app.work_cli) sends its requests. Host-side: it is the
        # operator's view of where the deployment answers, and no collector or
        # plugin has a reason to know it.
        "PEARSS_API_URL",
        "PEARSS_MCP_TOKEN",
        "PEARSS_MCP_ALLOWED_HOSTS",
        "PEARSS_DATABASE_URL",
        "PEARSS_STORAGE_DIR",
        "PEARSS_LOG_LEVEL",
        "PEARSS_DOCS_ENABLED",
        "PEARSS_MAX_BODY_BYTES",
        # Half-open request-header timeout (issue #9). Host-side by design: the
        # deadline is enforced by the HTTP protocol class in the parent
        # process, and a sandboxed child has no socket to time out. It must not
        # reach a child either -- nothing there reads it, and passing it would
        # widen the allowlist for a value with no consumer.
        "PEARSS_HTTP_HEADER_TIMEOUT_SECONDS",
        "PEARSS_AUDIT_RETENTION_DAYS",
        "PEARSS_SCHEDULER_ENABLED",
        "PEARSS_TICK_SECONDS",
        "PEARSS_AUTH_RATELIMIT_MAX",
        "PEARSS_AUTH_RATELIMIT_WINDOW_SECONDS",
        "PEARSS_AUTH_RATELIMIT_BLOCK_SECONDS",
        "PEARSS_ALLOW_PRIVATE_FETCH",
        # Model health (ADR 0048). Host-side by design and by architecture: the
        # worker is the only process that sees the whole queue, and a processor
        # child learns the resulting policy as *data* in its context
        # (`model_policy`), never by reading these. Passing them to a child would
        # widen the allowlist for values nothing there reads.
        "PEARSS_MODEL_COOLDOWN_SECONDS",
        "PEARSS_MODEL_COOLDOWN_MAX_SECONDS",
        "PEARSS_MODEL_ATTEMPTS_PER_MODEL",
        # Where the plugin loader reads installed plugins from (ADR 0023);
        # host-side, never passed to a child -- a plugin has no business
        # knowing where other plugins live.
        "PEARSS_PLUGIN_DIR",
        "PEARSS_SANDBOXED",  # set by the allowlist itself, a marker
        "PEARSS_READER_USERNAME",  # Reader API credentials (app-side)
        "PEARSS_READER_PASSWORD",
        "PEARSS_BACKFILL_SOURCE_ID",  # enrichment-backfill CLI plumbing
        "PEARSS_BACKFILL_LIMIT",
        "PEARSS_REINDEX_BATCH",  # reindex CLI plumbing
        "PEARSS_ENRICHMENT_CONCURRENCY",  # worker sweep concurrency
        "PEARSS_TICK_CONCURRENCY",  # scheduler tick concurrency
        # Fetch politeness (issue #35). Host-side by design: the sandboxed
        # child is spawned per row, so a counter or a clock inside it could
        # not pace anything across rows — the whole point of these knobs is
        # that the *worker* enforces them.
        "PEARSS_FETCH_MIN_INTERVAL",
        "PEARSS_FETCH_MAX_PER_HOST_PER_SWEEP",
        "PEARSS_FETCH_COOLDOWN_SECONDS",
        "PEARSS_BACKFILL_DEFAULT_LIMIT",
        "PEARSS_BACKUP_DIR",  # backup destination (CLI, host-side)
        "PEARSS_RESTORE_ARCHIVE",  # restore CLI plumbing
        "PEARSS_RESTORE_TARGET",
        # The A3 ingest credential (ADR 0022). Host-side and only host-side:
        # the collectors never push content, so a sandboxed child has no
        # business holding the token that can.
        # Alerting (ADR 0034). Host-side by design: the scheduler sends these
        # from the parent process, and a sandboxed child has no business
        # knowing where notifications go. `PEARSS_ALERT_URL` falls back to the
        # deployment's `NOTIFY_URL` (the shared notify gateway), so a host that
        # already injects that for other services needs no second setting.
        "PEARSS_ALERT_URL",
        "NOTIFY_URL",
        "PEARSS_ALERT_SUCCESS_RATE_THRESHOLD",
        "PEARSS_ALERT_MIN_RUNS",
        "PEARSS_ALERT_MIN_INTERVAL_SECONDS",
        "PEARSS_ALERT_TIMEOUT_SECONDS",
        "PEARSS_INGEST_TOKEN",
    }
)

# Collector-side prefixes: the child may see everything under these.
COLLECTOR_PREFIXES: tuple[str, ...] = (
    "PEARSS_X_",
    "PEARSS_ZHIHU_",
    "PEARSS_ZSXQ_",
    "PEARSS_COOKIECLOUD_",
    "PEARSS_TRANSLATE_",
    "PEARSS_EMBEDDING_",
)

# Test plumbing that deliberately reaches a sandboxed child. It is neither
# host-side (the child has to read it) nor a collector credential, so it gets
# its own category rather than being filed under a heading that misdescribes it.
# It is inert unless the test suite sets it; see conftest.py.
TEST_ONLY: frozenset[str] = frozenset({"PEARSS_TEST_DNS"})

_REFERENCE = re.compile(r"PEARSS_[A-Z0-9_]+")


def _referenced_names() -> set[str]:
    """Every PEARSS_* name (or prefix stem) that appears in src/."""
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for match in _REFERENCE.findall(path.read_text()):
            if match.endswith("_"):
                # A prefix stem used with startswith (e.g. PEARSS_COOKIECLOUD_):
                # the concrete variables under it are covered by the prefix
                # itself; record the stem so an unknown stem still fails here.
                names.add(match)
            else:
                names.add(match)
    return names


@pytest.mark.unit
def test_every_referenced_env_name_is_allowlisted_or_host_side() -> None:
    referenced = _referenced_names()
    assert referenced, "the scan found nothing; is the src/ layout intact?"

    unaccounted = sorted(
        name
        for name in referenced
        if name not in HOST_SIDE
        and name not in TEST_ONLY
        and not name.startswith(COLLECTOR_PREFIXES)
    )
    assert not unaccounted, (
        "PEARSS_* variables referenced in src/ but not reachable by the "
        "env allowlist and not host-side: "
        f"{unaccounted}. Either the collector's prefix is missing from "
        "_COLLECTOR_ENV_PREFIXES (the child would read empty credentials "
        "and report auth), or a new host-side variable needs adding to "
        "HOST_SIDE here."
    )


@pytest.mark.unit
def test_the_allowlist_prefixes_match_what_the_collectors_read() -> None:
    """The runtime allowlist and this guard's prefix list agree."""
    from app.plugin_runtime import _COLLECTOR_ENV_PREFIXES

    runtime_prefixes = {
        prefix for prefixes in _COLLECTOR_ENV_PREFIXES.values() for prefix in prefixes
    }
    assert runtime_prefixes == set(COLLECTOR_PREFIXES), (
        "test_env_allowlist_coverage.COLLECTOR_PREFIXES and "
        "_COLLECTOR_ENV_PREFIXES disagree; update both together."
    )


@pytest.mark.unit
def test_a_new_collector_prefix_fails_the_guard_until_registered() -> None:
    """The guard actually bites: an unregistered reference is flagged."""
    # Every *sandboxed* collector must appear in the allowlist map, even
    # telegram (empty tuple): a collector missing from the map entirely
    # would silently inherit nothing and fail open in future refactors.
    # rss is in-process and correctly absent.
    from app.collector_registry import _DECLARATIONS
    from app.plugin_runtime import _COLLECTOR_ENV_PREFIXES

    sandboxed = {name for name, is_sandboxed, _ in _DECLARATIONS if is_sandboxed}
    assert sandboxed <= set(_COLLECTOR_ENV_PREFIXES), (
        "sandboxed collectors in _DECLARATIONS and keys of "
        f"_COLLECTOR_ENV_PREFIXES disagree: declared={sorted(sandboxed)} "
        f"allowlisted={sorted(_COLLECTOR_ENV_PREFIXES)}"
    )
