"""Collector selection.

P1 has two collectors, registered here explicitly rather than discovered: the
guide defers dynamic import, manifest and lifecycle to P2, and P2 in turn waits
for three collectors before freezing a hookspec (Rule of Three).

Selection is by URL only, which is what `match_url` is for: it runs before any
fetch is scheduled, so it must not touch the network or the database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
import pluggy

from adapters.http import fetch as plain_fetch
from adapters.raw_payload import ConditionalState, RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
from core.plugins.hooks import CollectorError as SpecCollectorError
from core.plugins.hooks import CollectorSpec as spec
from core.plugins.hooks import Manifest

__all__ = [
    "Manifest",
    "Registration",
    "by_name",
    "plugins_for_surface",
    "select",
    "select_for",
    "sync_plugin_table",
]
from plugins.collectors.rss import collector as rss


class CollectorError(Exception):
    """A parse that failed, carrying its ErrorClass.

    The class is typed as `ErrorClass` rather than a generic object because
    the taxonomy is supposed to have one source of truth: letting a collector
    attach an arbitrary value here is how a sixth error class appears without
    anyone deciding it should exist.
    """

    def __init__(self, error_class: ErrorClass, detail: str) -> None:
        super().__init__(f"{error_class.value}: {detail}")
        self.error_class = error_class
        self.detail = detail


class Collector(Protocol):
    """The shape a P1 quasi-plugin satisfies.

    A Protocol rather than an ABC: the collectors predate this file and are
    plain modules, and structural typing lets them stay that way. P2 replaces
    this with a frozen pluggy hookspec once there are three to generalise from.
    """

    def match_url(self, url: str) -> bool: ...

    def parse(self, raw: RawPayload) -> list[ItemDraft]: ...


class FetchingCollector(Protocol):
    """The optional fetch hook from the hookspec draft (spec section 3).

    A collector whose origin needs signed or cookie-authenticated requests
    fetches its own page: the plain adapter cannot know how to sign one. The
    module exposes `fetch(url, *, client, source_id) -> RawPayload` and the
    registration declares `fetches=True` — declared, not probed, so a module
    that happens to define the name does not silently change collection.
    """

    def fetch(
        self,
        url: str,
        *,
        client: httpx.Client,
        source_id: str,
        conditional: ConditionalState | None = None,
    ) -> RawPayload: ...


class BackfillingCollector(Protocol):
    """The optional half: an origin that can be walked backwards.

    Separate from `Collector` because most cannot. An RSS feed has no cursor
    beyond what it happens to include, so demanding these of every collector
    would make the common case implement two methods that raise. Capability is
    probed with `supports_backfill` instead of declared, so the module that has
    to agree with the check is the module itself.
    """

    def before_url(self, url: str, cursor: str) -> str | None: ...

    def oldest_cursor(self, drafts: list[ItemDraft]) -> str | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Registration:
    """One registered collector and how the host reaches it.

    `collector` is either the in-process module (builtin rss) or a
    `SandboxedCollector` proxy (third-party): both satisfy the same
    Protocol surface, so the pipeline and routes never branch on trust.
    """

    name: str
    collector: Collector
    # A paged collector is walked a page at a time against stored cursor
    # state, which is a different collection path, not a loop around the
    # same one. Declared, not probed (ADR 0017).
    paged: bool = False
    # The collector fetches its own pages (signed or cookie-authenticated
    # origins). Declared, not probed.
    fetches: bool = False
    # The verified manifest as its surface dict; `checksum` is the registry
    # computed sha256 of its canonical JSON (ADR 0017).
    manifest: dict[str, Any]
    checksum: str
    # The trust level the loader asserted: `builtin` for the shipped set.
    trust: str = "builtin"


_PM = pluggy.PluginManager("pearss")
_PM.add_hookspecs(spec)

# The shipped set, in routing priority order (first match wins). Third-party
# collectors (everything but rss) are *not imported by the host*: their
# hookimpls are declared on lightweight shims and executed in sandboxed
# child processes, so a crash or memory leak in one costs the host nothing.
_SANDBOXED: tuple[str, ...] = ("telegram", "twitter", "zhihu", "zsxq")

# Declaration records: (id, sandboxed?, declared capabilities). The manifest
# itself comes from the implementation (`source_config`), verified against
# these declared capabilities at startup (ADR 0017).
_DECLARATIONS: tuple[tuple[str, bool, frozenset[str]], ...] = (
    ("telegram", True, frozenset()),
    ("twitter", True, frozenset({"paged"})),
    ("zhihu", True, frozenset({"fetches"})),
    ("zsxq", True, frozenset({"fetches"})),
    # rss stays in-process: it handles the host's own protocol and needs no
    # credentials. Its fetch hook upgrades WordPress summary feeds to full
    # text via the origin's REST API, falling back to the plain payload.
    ("rss", False, frozenset({"fetches"})),
)


def _build_registry(
    *,
    overrides: dict[str, Any] | None = None,
) -> tuple[Registration, ...]:
    """Build the registration set: sandboxed proxies + the in-process rss.

    Capability verification is the point of this pass, in both directions:
    a manifest declaring `paged` without a `collect_pages` implementation
    (or an implementation without the declaration) is a contract violation,
    and a violated contract must fail at startup rather than at the first
    collection that needs the missing half. The verified manifest leaves
    with its checksum computed.

    `overrides` swaps a collector's implementation for a test double keyed
    by id; the verification runs against the override exactly as it would
    against the real module.
    """
    overrides = overrides or {}

    def _impls(executor: Any) -> dict[str, bool]:
        return {
            "fetch": bool(getattr(executor, "fetch", None)),
            "collect_pages": bool(getattr(executor, "collect_pages", None)),
            "backfill": bool(getattr(executor, "backfill", None)),
        }

    registrations: list[Registration] = []
    for name, sandboxed, declared in _DECLARATIONS:
        if sandboxed:
            from app.plugin_runtime import (
                PluginChild,
                SandboxedCollector,
                SandboxLimits,
            )

            # The child spawns lazily on first use: importing this module
            # (which every test and CLI entry does) must not fork processes.
            # No env override: the child environment is the runtime's own
            # allowlist (base vars + this collector's credential prefixes).
            child = PluginChild(name, limits=SandboxLimits())
            _SANDBOX_CHILDREN[name] = child
            executor: Any = SandboxedCollector(child)
        else:
            executor = overrides.get(name, rss)
            existing = _PM.get_plugin(f"collector-{name}")
            if existing is not None:
                _PM.unregister(existing)
            _PM.register(executor, name=f"collector-{name}")

        impls = _impls(executor)
        manifest_data: dict[str, Any] = (
            executor.source_config().as_dict()
            if hasattr(executor.source_config(), "as_dict")
            else dict(executor.source_config())
        )
        if manifest_data["id"] != name:
            raise RuntimeError(
                f"collector {name!r} declares manifest id {manifest_data['id']!r}"
            )
        # Two feature kinds: structural ones map to a hookimpl that must
        # exist; descriptive ones (signed, cookie, wordpress-upgrade)
        # characterise *how* the fetch hook works and require it.
        structural = {
            "fetch": impls["fetch"],
            "paged": impls["collect_pages"],
            "backfill": impls["backfill"],
        }
        descriptive = {"signed", "cookie", "wordpress-upgrade"}
        for feature in manifest_data["features"]:
            if feature in structural:
                if not structural[feature]:
                    raise RuntimeError(
                        f"collector {name!r} declares feature {feature!r} "
                        "without implementing it"
                    )
            elif feature not in descriptive:
                raise RuntimeError(
                    f"collector {name!r} declares unknown feature {feature!r}"
                )
            elif feature in descriptive and not impls["fetch"]:
                raise RuntimeError(
                    f"collector {name!r} declares descriptive feature "
                    f"{feature!r} without a fetch hook"
                )
        checksum = hashlib.sha256(
            json.dumps(manifest_data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        registrations.append(
            Registration(
                name=name,
                collector=cast("Collector", executor),
                paged="paged" in declared,
                fetches="fetches" in declared,
                manifest=manifest_data,
                checksum=checksum,
                trust="third-party" if sandboxed else "builtin",
            )
        )
    return tuple(registrations)


_SANDBOX_CHILDREN: dict[str, Any] = {}


def set_http_transport(collector_id: str, transport: Any) -> None:
    """Inject the egress transport for one sandboxed collector.

    The app's lifespan passes the shared host client here so the child's
    reverse http calls run through the SSRF guard; tests pass a
    MockTransport so fixtures flow to the child.
    """
    from app.plugin_runtime import PluginChild

    child = _SANDBOX_CHILDREN.get(collector_id)
    if not isinstance(child, PluginChild):
        raise ValueError(f"no sandboxed collector {collector_id!r}")
    child.set_http_transport(transport)


def for_each_sandboxed_collector(apply: Callable[[str, Any], None]) -> None:
    """Run `apply(id, child)` over every sandboxed collector.

    A name-and-child callback rather than exposing the registry's internal
    dict: callers wire egress transports, and the dict's shape is not part
    of any contract. Builds the registry if it is not built yet, matching
    how every other entry point behaves.
    """
    _registry()
    for collector_id, child in _SANDBOX_CHILDREN.items():
        apply(collector_id, child)


_REGISTRY: tuple[Registration, ...] | None = None


def _registry() -> tuple[Registration, ...]:
    """Lazy build: the first access spawns the sandboxed children."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def refresh_registry() -> tuple[Registration, ...]:
    """Tear down all children and rebuild (:reload support, P2b)."""
    global _REGISTRY
    if _REGISTRY is not None:
        for registration in _REGISTRY:
            stop = getattr(registration.collector, "stop", None)
            if callable(stop):
                stop()
    _REGISTRY = _build_registry()
    return _REGISTRY


def select(url: str) -> Registration | None:
    """The collector whose matcher claims `url`, or None.

    URL matching alone is a heuristic: it looks for path fragments that a feed
    often but not always carries. Use `select_for` when a source type is known.
    """
    for registration in _registry():
        if registration.collector.match_url(url):
            return registration
    return None


def select_for(source_type: str, url: str) -> Registration | None:
    """The collector for a source, honouring the type the operator declared.

    Declared type wins over the URL heuristic. `hnrss.org/frontpage` is a real
    RSS feed that contains none of the fragments the RSS matcher looks for, so
    routing purely by URL refused to collect a source someone had explicitly
    created as `rss` -- and the schema already records that intent in
    `source.type`, which ignoring it threw away.

    The URL still decides for `web` sources, where the type says only that
    something must be scraped rather than which scraper fits.
    """
    if source_type == "rss":
        return by_name("rss")
    return select(url)


def by_name(name: str) -> Registration | None:
    return next(
        (registration for registration in _registry() if registration.name == name),
        None,
    )


def parse_with(registration: Registration, raw: RawPayload) -> list[ItemDraft]:
    """Parse `raw`, normalising the two collectors' error types into one.

    Each collector defines its own `CollectorError` with the same shape, so a
    caller would otherwise need to catch both. Translating here keeps that
    duplication out of the pipeline, and is the seam a P2 hookspec replaces.
    """
    # The frozen spec's single CollectorError is what parse impls raise;
    # re-raised as the registry's own type so callers keep one import.
    try:
        return registration.collector.parse(raw)
    except SpecCollectorError as exc:
        raise CollectorError(exc.error_class, exc.detail) from exc


def fetch_page_with(
    registration: Registration,
    url: str,
    *,
    client: httpx.Client,
    source_id: str,
    conditional: ConditionalState | None,
) -> RawPayload:
    """Fetch through the collector's own hook, or the plain adapter.

    `conditional` is forwarded into fetching hooks as well: a hook that
    starts from the plain adapter (the rss collector's WordPress upgrade
    does) replays the ETag on its first leg, while hooks that never use it
    — a signed API answers no 304 — simply ignore the argument.
    """
    if registration.fetches:
        fetcher = cast("FetchingCollector", registration.collector)
        return fetcher.fetch(
            url, client=client, source_id=source_id, conditional=conditional
        )
    return plain_fetch(url, client=client, source_id=source_id, conditional=conditional)


def backfiller(registration: Registration) -> BackfillingCollector | None:
    """The collector as a backfiller, or None when its origin has no cursor.

    Returning the narrowed view rather than a boolean means the caller cannot
    forget to check: to use the cursor methods it must first hold this value.
    """
    if not supports_backfill(registration):
        return None
    collector = registration.collector
    before_url = getattr(collector, "before_url", None)
    oldest_cursor = getattr(collector, "oldest_cursor", None)
    if not callable(before_url) or not callable(oldest_cursor):
        return None
    return cast("BackfillingCollector", collector)


def supports_backfill(registration: Registration) -> bool:
    """Whether this collector declared the backward-walk capability.

    Declared via the hookspec's `backfill()` (returning BackfillHooks), not
    probed: the hasattr probe this replaces could not tell "declares the
    capability" from "happens to define the names".
    """
    if not hasattr(registration.collector, "backfill"):
        return False
    return registration.collector.backfill() is not None


def matchers() -> tuple[tuple[str, Callable[[str], bool]], ...]:
    """Every registered matcher, for diagnostics and tests."""
    return tuple(
        (registration.name, registration.collector.match_url)
        for registration in _registry()
    )


def sync_plugin_table(session_factory: Any, *, tenant_id: str = "default") -> None:
    """Record every verified manifest in the plugin table at startup.

    Startup discovers; it does not decide. A collector the operator disabled
    stays disabled — only the manifest and checksum are refreshed, so an
    edited manifest is visibly different from the one the status decision
    was made on.
    """
    import json
    from datetime import UTC, datetime

    from adapters.repository import upsert_plugin

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    with session_factory() as session:
        for registration in _registry():
            manifest = registration.manifest  # surface dict
            upsert_plugin(
                session,
                plugin_id=str(manifest["id"]),
                manifest_json=json.dumps(manifest, sort_keys=True, ensure_ascii=False),
                checksum=registration.checksum,
                trust="builtin",
                tenant_id=tenant_id,
                at=stamp,
            )
        session.commit()


def plugins_for_surface() -> list[dict[str, object]]:
    """Manifest summaries for the MCP/API surface (list_plugins)."""
    return [{**r.manifest, "checksum": r.checksum} for r in _registry()]
