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
import logging
import os
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
from core.tenancy import SINGLE_TENANT

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
    would make the common case implement two methods that raise. Capability
    is declared via the collector's `backfill()` returning a hooks object
    (core.plugins.hooks.BackfillHooks), which is also what `backfiller()`
    returns narrowed to.
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
# implementations execute in sandboxed child processes, so a crash or memory
# leak in one costs the host nothing. The sandboxed/in-process split is
# declared once, in _DECLARATIONS below — derive from it, never re-list ids.

# Declaration records: (id, sandboxed?, declared capabilities). The manifest
# itself comes from the implementation (`source_config`), verified against
# these declared capabilities at startup (ADR 0017).
_DECLARATIONS: tuple[tuple[str, bool, frozenset[str]], ...] = (
    # telegram fetches: the source URL names the channel, the readable page
    # lives at /s/<name> — a rewrite only the collector knows.
    ("telegram", True, frozenset({"fetches"})),
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

    `overrides` swaps the in-process rss implementation for a test double;
    the verification runs against the override exactly as it would against
    the real module. (The sandboxed collectors are process boundaries, not
    importable modules, so they take doubles through the injected egress
    transport instead.)
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

    # Installed plugins last, so a shipped collector that already claimed the
    # id wins: `install_plugin` refuses such an id at import time, and this is
    # the second line of that defence.
    for plugin_id, manifest in sorted(_INSTALLED_PLUGINS.items()):
        if plugin_id in {name for name, _, _ in _DECLARATIONS}:
            continue
        registrations.append(_installed_registration(plugin_id, manifest))
    return tuple(registrations)


def _installed_registration(plugin_id: str, manifest: dict[str, Any]) -> Registration:
    """A registration for a plugin installed at runtime (ADR 0023).

    The child is built with the plugin directory and its bundled venv on
    `PYTHONPATH` (that is the "bring your own venv" decision), and with the
    credential prefixes the plugin declared rather than the shipped map --
    an installed plugin gets exactly what its manifest asked for.
    """
    from app import plugin_loader
    from app.plugin_runtime import PluginChild, SandboxedCollector, SandboxLimits

    directory = plugin_loader.plugin_directory(plugin_id)
    prefixes = plugin_loader.required_credential_prefixes(manifest)
    features = frozenset(manifest.get("features") or [])
    child = PluginChild(
        plugin_id,
        limits=SandboxLimits(),
        pythonpath=plugin_loader.child_pythonpath(directory),
        env=_plugin_env(prefixes),
    )
    _SANDBOX_CHILDREN[plugin_id] = child
    # Inherit the egress wiring the app installed. The lifespan wires
    # transports once at startup; a plugin installed later was not there for
    # it, and a child with no transport has its egress refused -- which would
    # surface as a collection failure that looked like the origin's fault.
    if _SANDBOX_TRANSPORT is not None:
        child.set_http_transport(_SANDBOX_TRANSPORT)
    return Registration(
        name=plugin_id,
        collector=cast("Collector", SandboxedCollector(child)),
        paged="paged" in features,
        fetches="fetch" in features,
        manifest=manifest,
        checksum=str(manifest.get("checksum", "")) or _installed_checksum(manifest),
        trust="third-party",
    )


def _plugin_env(prefixes: tuple[str, ...]) -> dict[str, str]:
    """The credential values a plugin declared, taken from the host env.

    Empty for a plugin that declared nothing, which is the common case: a
    collector with no credentials needs no environment beyond the base
    allowlist, and gets none.
    """
    if not prefixes:
        return {}
    return {
        key: value
        for key, value in os.environ.items()
        if any(key.startswith(prefix) for prefix in prefixes)
    }


def _installed_checksum(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


_SANDBOX_CHILDREN: dict[str, Any] = {}

# The egress transport the app wired into the sandboxed children, held so a
# plugin installed later can receive the same one. None until `set_http_transport`
# runs, which is the app's lifespan in production and the fixture in tests.
_SANDBOX_TRANSPORT: Any = None


def set_http_transport(collector_id: str, transport: Any) -> None:
    """Inject the egress transport for one sandboxed collector.

    The app's lifespan passes the shared host client here so the child's
    reverse http calls run through the SSRF guard; tests pass a
    MockTransport so fixtures flow to the child.
    """
    global _SANDBOX_TRANSPORT
    from app.plugin_runtime import PluginChild

    # Recorded so a plugin installed later inherits the same egress wiring.
    # Set only through this function (the app's lifespan and the tests' fixture
    # are its callers), so it always describes a transport the running app
    # actually chose rather than a leftover from a previous scenario.
    _SANDBOX_TRANSPORT = transport
    child = _SANDBOX_CHILDREN.get(collector_id)
    if not isinstance(child, PluginChild):
        raise ValueError(f"no sandboxed collector {collector_id!r}")
    child.set_http_transport(transport)


# The transport appliers handed to `for_each_sandboxed_collector`, replayed
# for children built later. The lifespan wires egress once at startup, but an
# installed plugin's child does not exist yet at that point (ADR 0023) -- and
# a child with no transport has its egress refused, which surfaces as a
# collection failure that looks like the origin's fault. Keeping the appliers
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

# Plugin ids installed at runtime from the plugin directory (ADR 0023). The
# loader registers them here after a successful `:import`, and clears them on
# `:uninstall`. Kept as module state beside the built registry for the same
# reason the registry itself is: both are rebuilt together, and a plugin that
# is installed but not in this set is one the operator removed.
_INSTALLED_PLUGINS: dict[str, dict[str, Any]] = {}


def restore_installed_plugins(
    session_factory: Any, *, tenant_id: str = SINGLE_TENANT
) -> None:
    """Recover explicit installations before startup wires egress or starts jobs.

    The database is the consent record, not a directory scan. A changed manifest
    (including credential prefixes) requires a new explicit import. Check it
    *before* running plugin code or giving a verification child any credentials.
    Missing/broken installations remain recorded, but unavailable, so repairing
    their files permits a later restart or explicit import to recover them.
    Startup does not change the operator's enabled/disabled decision.
    """
    from adapters.repository import list_plugin_rows
    from app import plugin_loader

    shipped_ids = {name for name, _, _ in _DECLARATIONS}
    with session_factory() as session:
        records = [
            (row.id, row.checksum)
            for row in list_plugin_rows(session, tenant_id=tenant_id)
            if row.id not in shipped_ids
        ]

    recovered: dict[str, dict[str, Any]] = {}
    for plugin_id, checksum in records:
        try:
            on_disk = plugin_loader.read_manifest(plugin_id)
            if on_disk.checksum != checksum:
                raise plugin_loader.PluginLoadError("manifest changed; import again")
            impls = plugin_loader.verify_in_child(on_disk)
            plugin_loader.verify_declarations(on_disk, impls)
        except (plugin_loader.PluginLoadError, OSError, ValueError, TypeError):
            # Never log exception text: child errors and manifests can contain
            # credentials or private URLs. The id identifies the recovery action.
            logging.getLogger(__name__).warning(
                "Installed plugin %r unavailable; check files and explicitly import "
                "again if its manifest changed",
                plugin_id,
            )
            continue
        recovered[plugin_id] = {**on_disk.manifest, "checksum": on_disk.checksum}

    # A lifespan may be entered again by an embedding application. Drop stale
    # in-memory installations too, without touching shipped children or DB rows.
    for plugin_id in tuple(_INSTALLED_PLUGINS):
        uninstall_plugin(plugin_id)
    _INSTALLED_PLUGINS.update(recovered)
    _rebuild_installed()


def install_plugin(plugin_id: str, manifest: dict[str, Any]) -> None:
    """Make an installed plugin part of the registry.

    The caller (the `:import` route) has already validated the plugin on
    disk and verified its declarations in a child; this records the decision
    and adds its registration.

    Rebuilds only the installed registrations rather than the whole registry:
    a full rebuild would stop and respawn every shipped collector's child,
    which an install has no reason to disturb -- a running collection would
    be cut off mid-flight.
    """
    _INSTALLED_PLUGINS[plugin_id] = manifest
    _rebuild_installed()


def uninstall_plugin(plugin_id: str) -> None:
    """Remove an installed plugin from the registry, keeping its files."""
    global _REGISTRY
    _INSTALLED_PLUGINS.pop(plugin_id, None)
    child = _SANDBOX_CHILDREN.pop(plugin_id, None)
    stop = getattr(child, "stop", None)
    if callable(stop):
        stop()
    if _REGISTRY is not None:
        _REGISTRY = tuple(r for r in _REGISTRY if r.name != plugin_id)


def _rebuild_installed() -> None:
    """Replace the installed registrations, leaving the shipped ones alone.

    Any child for an id being replaced is stopped first: a reload mints a new
    child, and leaving the old one running would hold a subprocess (and its
    environment) for a plugin that is no longer registered.
    """
    global _REGISTRY
    if _REGISTRY is None:
        # Nothing built yet; the next `_registry()` will include them.
        return
    for plugin_id in list(_INSTALLED_PLUGINS):
        previous = _SANDBOX_CHILDREN.pop(plugin_id, None)
        stop = getattr(previous, "stop", None)
        if callable(stop):
            stop()
    # Provenance comes from the shipped declarations, not trust: shipped
    # sandboxed collectors also carry trust="third-party". Keep their existing
    # registrations and children, including the running app's egress wiring.
    shipped_ids = {name for name, _, _ in _DECLARATIONS}
    shipped = tuple(r for r in _REGISTRY if r.name in shipped_ids)
    installed = tuple(
        _installed_registration(plugin_id, manifest)
        for plugin_id, manifest in sorted(_INSTALLED_PLUGINS.items())
        if plugin_id not in {name for name, _, _ in _DECLARATIONS}
    )
    _REGISTRY = shipped + installed


def installed_plugin_ids() -> tuple[str, ...]:
    """The ids currently installed at runtime."""
    return tuple(sorted(_INSTALLED_PLUGINS))


def forget_installed_plugins() -> None:
    """Drop every installed plugin from the registry and stop its child.

    Exists for teardown, not for the operator: `:uninstall` removes one plugin
    and leaves the rest alone. This is what a test suite calls between cases,
    and what a clean shutdown would call, where "the registry keeps a plugin
    from a previous scenario" is a leak rather than a decision.

    Deliberately does not `refresh_registry()`: that rebuilds the *shipped*
    children too, leaving the previous generation of processes running. The
    only registrations this removes are its own.
    """
    if not _INSTALLED_PLUGINS:
        # Nothing installed: leave the registry alone. This is the common case
        # for a teardown that runs after every test, and rebuilding when there
        # is nothing to remove would invalidate a registry the current test is
        # still using.
        return
    _INSTALLED_PLUGINS.clear()
    # A full rebuild, because an installed plugin's child carries the plugin's
    # code and its `PYTHONPATH`: stopping just that child would leave the
    # registry describing a plugin that no longer exists. The shipped children
    # are respawned as part of the same rebuild, which is what a caller
    # resetting registry state wants.
    refresh_registry()
    # The remembered transport belonged to the generation just torn down; a
    # test suite that ran one scenario would otherwise replay its egress onto
    # the next scenario's plugin. Production re-wires it on the next lifespan.
    global _SANDBOX_TRANSPORT
    _SANDBOX_TRANSPORT = None


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

    Installed plugins are tried before the shipped set, and before rss in
    particular: rss matches nearly any http URL, so anything ordered after it
    can only be reached by a URL rss declines. An installed plugin states a
    specific claim -- it was installed *for* particular sites -- and the
    shipping order exists to let a specific matcher beat a general one, which
    is the same reason telegram precedes rss.
    """
    for registration in _installed_first():
        if registration.collector.match_url(url):
            return registration
    return None


def _installed_first() -> tuple[Registration, ...]:
    """The registry with installed plugins ahead of the shipped collectors."""
    registry = _registry()
    installed = tuple(r for r in registry if r.trust != "builtin")
    shipped = tuple(r for r in registry if r.trust == "builtin")
    return installed + shipped


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
    """The collector's backward-walk hooks, or None when it has no cursor.

    The hooks object `backfill()` returns is the declaration of record
    (core.plugins.hooks.BackfillHooks): an in-process collector binds its
    module functions into one, and a sandboxed proxy returns an RPC-backed
    one, so the same narrowing serves both without probing module attributes
    that a proxy structurally cannot have.
    """
    if not hasattr(registration.collector, "backfill"):
        return None
    hooks = registration.collector.backfill()
    if hooks is None:
        return None
    return cast("BackfillingCollector", hooks)


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


def sync_plugin_table(session_factory: Any, *, tenant_id: str = SINGLE_TENANT) -> None:
    """Record every verified manifest in the plugin table at startup.

    Startup discovers; it does not decide. A collector the operator disabled
    stays disabled — only the manifest and checksum are refreshed, so an
    edited manifest is visibly different from the one the status decision
    was made on.
    """
    import json
    from datetime import UTC, datetime

    from adapters.repository import upsert_plugin
    from adapters.timestamps import fixed_width_utc

    stamp = fixed_width_utc(datetime.now(UTC))
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
