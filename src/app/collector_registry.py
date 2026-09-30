"""Collector selection.

P1 has two collectors, registered here explicitly rather than discovered: the
guide defers dynamic import, manifest and lifecycle to P2, and P2 in turn waits
for three collectors before freezing a hookspec (Rule of Three).

Selection is by URL only, which is what `match_url` is for: it runs before any
fetch is scheduled, so it must not touch the network or the database.

`CollectorRegistry` owns every piece of mutable registry state -- the built
registrations, the sandboxed children, the egress transport they inherit, and
the plugins installed at runtime (ADR 0023). An application builds one
(`create_app`) and its lifespan, routes, pipeline, scheduler and MCP core
client all read that application's own object, so two applications in one
process do not share collectors, children or installations. The module-level
functions below delegate to a process default instance: it is what direct
callers (the CLI, collector suites that drive `collect_pipeline` without an
application) keep using, and it is deliberately *not* the application path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
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
    "CollectorRegistry",
    "Manifest",
    "Registration",
    "backfiller",
    "by_name",
    "collect_item_with",
    "default_registry",
    "fetch_page_with",
    "for_each_sandboxed_collector",
    "forget_installed_plugins",
    "install_plugin",
    "installed_plugin_ids",
    "matchers",
    "parse_with",
    "plugins_for_surface",
    "refresh_registry",
    "reset_registries",
    "restore_installed_plugins",
    "select",
    "select_for",
    "set_http_transport",
    "shipped_collector_ids",
    "supports_backfill",
    "sync_plugin_table",
    "uninstall_plugin",
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


class ItemCollectingCollector(Protocol):
    """The optional item hook: one already-known address in, drafts out.

    Separate from `Collector` because it is a capability rather than a
    requirement: a collector that only knows how to read its own listing
    cannot answer an address the caller found elsewhere. Declared via the
    manifest's `item` feature, so the registry's gate -- not an attribute
    probe on a module that may not have it -- decides whether a call is
    allowed.
    """

    def collect_item(
        self,
        *,
        source_id: str,
        url: str,
        client: httpx.Client,
    ) -> list[ItemDraft]: ...


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
    # The collector can turn one address the caller discovered into drafts
    # (content-level discovery). Declared, not probed: an implementation the
    # manifest does not name is inert on purpose.
    item: bool = False
    # The verified manifest as its surface dict; `checksum` is the registry
    # computed sha256 of its canonical JSON (ADR 0017).
    manifest: dict[str, Any]
    checksum: str
    # The trust level the loader asserted: `builtin` for the shipped set.
    trust: str = "builtin"


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
    # text via the origin's REST API, falling back to the plain payload; its
    # item hook fetches one address the caller discovered (WordPress REST
    # first, the shared article extractor as the fallback).
    ("rss", False, frozenset({"fetches", "item"})),
)

# Every registry built in this process, for teardown only. A suite that
# constructs applications without entering their lifespan would otherwise
# leave each application's sandboxed children running; `reset_registries()`
# is the hook that stops them. Production builds one application, whose
# lifespan is the shutdown path, so this holds the module default and that
# one application. Strong references on purpose: teardown must reach a
# registry whose application the test has already dropped.
_REGISTRIES: list[CollectorRegistry] = []


def shipped_collector_ids() -> frozenset[str]:
    """The collector ids this deployment ships.

    The one fact a caller outside this module needs from the declaration
    records, published so it does not have to unpack them: the tuple's shape
    (id, sandboxed, capability set) is an implementation detail, and a caller
    that unpacks it freezes that shape in place. The plugin `:import` route
    refuses an id colliding with a shipped collector, and `plugin_loader`
    refuses one on disk; both ask this rather than reading `_DECLARATIONS`.
    """
    return frozenset(name for name, _, _ in _DECLARATIONS)


class CollectorRegistry:
    """The collectors and their lifecycle, owned by one application.

    The four mutable facts live here as fields rather than as module globals:
    the built registrations, the sandboxed children, the egress transport they
    inherit, and the plugins installed at runtime. An application constructs
    one (`create_app` stores it on `app.state.collector_registry`); nothing on
    an application path reads the module default, so restart, reload, test
    isolation and "two applications in one process" reason about one object
    instead of shared state.
    """

    def __init__(self) -> None:
        self._registry: tuple[Registration, ...] | None = None
        self._sandbox_children: dict[str, Any] = {}
        # The egress transport the app wired into the sandboxed children, held
        # so a plugin installed later can receive the same one. None until
        # `set_http_transport` runs, which is the app's lifespan in production
        # and the fixture in tests.
        self._sandbox_transport: Any = None
        # Plugin ids installed at runtime from the plugin directory (ADR 0023).
        # The loader registers them here after a successful `:import`, and
        # clears them on `:uninstall`. Kept beside the built registry for the
        # same reason the registry itself is: both are rebuilt together, and a
        # plugin that is installed but not in this set is one the operator
        # removed.
        self._installed_plugins: dict[str, dict[str, Any]] = {}
        # The pluggy manager is per-registry too: a shared one would let one
        # application's rebuild unregister another's collector plugin.
        self._pm = pluggy.PluginManager("pearss")
        self._pm.add_hookspecs(spec)
        # The registry is read by parallel tick threads (`run_tick` collects
        # sources concurrently) and mutated by install/uninstall/reload. The
        # lock makes the lazy build atomic: without it two threads can build at
        # once, and the loser's sandboxed children are overwritten in
        # `_sandbox_children` and dropped while still running -- a real
        # subprocess leak. Re-entrant because a lifecycle method may call
        # another (install -> rebuild).
        self._lock = threading.RLock()
        _REGISTRIES.append(self)

    def _build_registry(
        self,
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
                "collect_item": bool(getattr(executor, "collect_item", None)),
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
                self._sandbox_children[name] = child
                executor: Any = SandboxedCollector(child)
            else:
                executor = overrides.get(name, rss)
                existing = self._pm.get_plugin(f"collector-{name}")
                if existing is not None:
                    self._pm.unregister(existing)
                self._pm.register(executor, name=f"collector-{name}")

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
                "item": impls["collect_item"],
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
                    item="item" in declared,
                    manifest=manifest_data,
                    checksum=checksum,
                    trust="third-party" if sandboxed else "builtin",
                )
            )

        # Installed plugins last, so a shipped collector that already claimed
        # the id wins: `install_plugin` refuses such an id at import time, and
        # this is the second line of that defence.
        for plugin_id, manifest in sorted(self._installed_plugins.items()):
            if plugin_id in {name for name, _, _ in _DECLARATIONS}:
                continue
            registrations.append(self._installed_registration(plugin_id, manifest))
        return tuple(registrations)

    def _installed_registration(
        self, plugin_id: str, manifest: dict[str, Any]
    ) -> Registration:
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
        self._sandbox_children[plugin_id] = child
        # Inherit the egress wiring the app installed. The lifespan wires
        # transports once at startup; a plugin installed later was not there for
        # it, and a child with no transport has its egress refused -- which would
        # surface as a collection failure that looked like the origin's fault.
        if self._sandbox_transport is not None:
            child.set_http_transport(self._sandbox_transport)
        return Registration(
            name=plugin_id,
            collector=cast("Collector", SandboxedCollector(child)),
            paged="paged" in features,
            fetches="fetch" in features,
            item="item" in features,
            manifest=manifest,
            checksum=str(manifest.get("checksum", "")) or _installed_checksum(manifest),
            trust="third-party",
        )

    def set_http_transport(self, collector_id: str, transport: Any) -> None:
        """Inject the egress transport for one sandboxed collector.

        The app's lifespan passes the shared host client here so the child's
        reverse http calls run through the SSRF guard; tests pass a
        MockTransport so fixtures flow to the child.
        """
        from app.plugin_runtime import PluginChild

        # Recorded so a plugin installed later inherits the same egress wiring.
        # Set only through this function (the app's lifespan and the tests'
        # fixture are its callers), so it always describes a transport the
        # running app actually chose rather than a leftover from a previous
        # scenario.
        self._sandbox_transport = transport
        child = self._sandbox_children.get(collector_id)
        if not isinstance(child, PluginChild):
            raise ValueError(f"no sandboxed collector {collector_id!r}")
        child.set_http_transport(transport)

    def for_each_sandboxed_collector(self, apply: Callable[[str, Any], None]) -> None:
        """Run `apply(id, child)` over every sandboxed collector.

        A name-and-child callback rather than exposing the registry's internal
        dict: callers wire egress transports, and the dict's shape is not part
        of any contract. Builds the registry if it is not built yet, matching
        how every other entry point behaves.
        """
        self._registry_for_build()
        for collector_id, child in self._sandbox_children.items():
            apply(collector_id, child)

    def restore_installed_plugins(
        self, session_factory: Any, *, tenant_id: str = SINGLE_TENANT
    ) -> None:
        """Recover explicit installations before startup wires egress or starts jobs.

        The database is the consent record, not a directory scan. A changed
        manifest (including credential prefixes) requires a new explicit import.
        Check it *before* running plugin code or giving a verification child any
        credentials. Missing/broken installations remain recorded, but
        unavailable, so repairing their files permits a later restart or explicit
        import to recover them. Startup does not change the operator's
        enabled/disabled decision.
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
                    raise plugin_loader.PluginLoadError(
                        "manifest changed; import again"
                    )
                impls = plugin_loader.verify_in_child(on_disk)
                plugin_loader.verify_declarations(on_disk, impls)
            except (plugin_loader.PluginLoadError, OSError, ValueError, TypeError):
                # Never log exception text: child errors and manifests can contain
                # credentials or private URLs. The id identifies the recovery action.
                logging.getLogger(__name__).warning(
                    "Installed plugin %r unavailable; check files and explicitly "
                    "import again if its manifest changed",
                    plugin_id,
                )
                continue
            recovered[plugin_id] = {**on_disk.manifest, "checksum": on_disk.checksum}

        # A lifespan may be entered again by an embedding application. Drop stale
        # in-memory installations too, without touching shipped children or DB rows.
        for plugin_id in tuple(self._installed_plugins):
            self.uninstall_plugin(plugin_id)
        self._installed_plugins.update(recovered)
        self._rebuild_installed()

    def install_plugin(self, plugin_id: str, manifest: dict[str, Any]) -> None:
        """Make an installed plugin part of the registry.

        The caller (the `:import` route) has already validated the plugin on
        disk and verified its declarations in a child; this records the decision
        and adds its registration.

        Rebuilds only the installed registrations rather than the whole registry:
        a full rebuild would stop and respawn every shipped collector's child,
        which an install has no reason to disturb -- a running collection would
        be cut off mid-flight.
        """
        self._installed_plugins[plugin_id] = manifest
        self._rebuild_installed()

    def uninstall_plugin(self, plugin_id: str) -> None:
        """Remove an installed plugin from the registry, keeping its files."""
        with self._lock:
            self._installed_plugins.pop(plugin_id, None)
            child = self._sandbox_children.pop(plugin_id, None)
            stop = getattr(child, "stop", None)
            if callable(stop):
                stop()
            if self._registry is not None:
                self._registry = tuple(r for r in self._registry if r.name != plugin_id)

    def _rebuild_installed(self) -> None:
        """Replace the installed registrations, leaving the shipped ones alone.

        Any child for an id being replaced is stopped first: a reload mints a new
        child, and leaving the old one running would hold a subprocess (and its
        environment) for a plugin that is no longer registered.
        """
        with self._lock:
            if self._registry is None:
                # Nothing built yet; the next `_registry_for_build()` will include
                # them.
                return
            for plugin_id in list(self._installed_plugins):
                previous = self._sandbox_children.pop(plugin_id, None)
                stop = getattr(previous, "stop", None)
                if callable(stop):
                    stop()
            # Provenance comes from the shipped declarations, not trust: shipped
            # sandboxed collectors also carry trust="third-party". Keep their
            # existing registrations and children, including the running app's
            # egress wiring.
            shipped_ids = {name for name, _, _ in _DECLARATIONS}
            shipped = tuple(r for r in self._registry if r.name in shipped_ids)
            installed = tuple(
                self._installed_registration(plugin_id, manifest)
                for plugin_id, manifest in sorted(self._installed_plugins.items())
                if plugin_id not in {name for name, _, _ in _DECLARATIONS}
            )
            self._registry = shipped + installed

    def installed_plugin_ids(self) -> tuple[str, ...]:
        """The ids currently installed at runtime."""
        return tuple(sorted(self._installed_plugins))

    def forget_installed_plugins(self) -> None:
        """Drop every installed plugin from the registry and stop its child.

        Exists for teardown, not for the operator: `:uninstall` removes one plugin
        and leaves the rest alone. This is what a test suite calls between cases,
        and what a clean shutdown would call, where "the registry keeps a plugin
        from a previous scenario" is a leak rather than a decision.

        Deliberately does not `refresh_registry()`: that rebuilds the *shipped*
        children too, leaving the previous generation of processes running. The
        only registrations this removes are its own.
        """
        if not self._installed_plugins:
            # Nothing installed: leave the registry alone. This is the common case
            # for a teardown that runs after every test, and rebuilding when there
            # is nothing to remove would invalidate a registry the current test is
            # still using.
            return
        self._installed_plugins.clear()
        # A full rebuild, because an installed plugin's child carries the plugin's
        # code and its `PYTHONPATH`: stopping just that child would leave the
        # registry describing a plugin that no longer exists. The shipped children
        # are respawned as part of the same rebuild, which is what a caller
        # resetting registry state wants.
        self.refresh_registry()
        # The remembered transport belonged to the generation just torn down; a
        # test suite that ran one scenario would otherwise replay its egress onto
        # the next scenario's plugin. Production re-wires it on the next lifespan.
        self._sandbox_transport = None

    def _registry_for_build(self) -> tuple[Registration, ...]:
        """Lazy build: the first access spawns the sandboxed children.

        Double-checked under the registry lock so a parallel tick cannot build
        twice and orphan the first generation of children.
        """
        if self._registry is None:
            with self._lock:
                if self._registry is None:
                    self._registry = self._build_registry()
        return self._registry

    def refresh_registry(self) -> tuple[Registration, ...]:
        """Tear down all children and rebuild (:reload support, P2b)."""
        with self._lock:
            if self._registry is not None:
                for registration in self._registry:
                    stop = getattr(registration.collector, "stop", None)
                    if callable(stop):
                        stop()
            self._registry = self._build_registry()
            return self._registry

    def select(self, url: str) -> Registration | None:
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
        for registration in self._installed_first():
            if registration.collector.match_url(url):
                return registration
        return None

    def _installed_first(self) -> tuple[Registration, ...]:
        """The registry with installed plugins ahead of the shipped collectors."""
        registry = self._registry_for_build()
        installed = tuple(r for r in registry if r.trust != "builtin")
        shipped = tuple(r for r in registry if r.trust == "builtin")
        return installed + shipped

    def select_for(self, source_type: str, url: str) -> Registration | None:
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
            return self.by_name("rss")
        return self.select(url)

    def by_name(self, name: str) -> Registration | None:
        return next(
            (
                registration
                for registration in self._registry_for_build()
                if registration.name == name
            ),
            None,
        )

    def matchers(self) -> tuple[tuple[str, Callable[[str], bool]], ...]:
        """Every registered matcher, for diagnostics and tests."""
        return tuple(
            (registration.name, registration.collector.match_url)
            for registration in self._registry_for_build()
        )

    def sync_plugin_table(
        self, session_factory: Any, *, tenant_id: str = SINGLE_TENANT
    ) -> None:
        """Record every verified manifest in the plugin table at startup.

        Startup discovers; it does not decide. A collector the operator disabled
        stays disabled — only the manifest and checksum are refreshed, so an
        edited manifest is visibly different from the one the status decision
        was made on.
        """
        from datetime import UTC, datetime

        from adapters.repository import upsert_plugin
        from adapters.timestamps import fixed_width_utc

        stamp = fixed_width_utc(datetime.now(UTC))
        with session_factory() as session:
            for registration in self._registry_for_build():
                manifest = registration.manifest  # surface dict
                upsert_plugin(
                    session,
                    plugin_id=str(manifest["id"]),
                    manifest_json=json.dumps(
                        manifest, sort_keys=True, ensure_ascii=False
                    ),
                    checksum=registration.checksum,
                    trust="builtin",
                    tenant_id=tenant_id,
                    at=stamp,
                )
            session.commit()

    def plugins_for_surface(self) -> list[dict[str, object]]:
        """Manifest summaries for the MCP/API surface (list_plugins)."""
        return [
            {**r.manifest, "checksum": r.checksum} for r in self._registry_for_build()
        ]

    def _shutdown(self) -> None:
        """Stop every child and drop installed plugins (teardown only).

        The built shipped registrations are kept, with their children stopped.
        Rebuilding them here would start a fresh generation of sandboxed
        children, and a parallel tick that lazily builds at the same moment
        can leave the loser's children running -- which is the leak this hook
        exists to prevent. A sandboxed collector restarts its own child on
        next use, so the stopped registrations stay valid.
        """
        for child in self._sandbox_children.values():
            stop = getattr(child, "stop", None)
            if callable(stop):
                stop()
        if self._installed_plugins:
            # Drop an installed plugin's registration (and its stopped child)
            # so the next access does not see a plugin a previous scenario
            # installed. Done by filtering, not by a full rebuild: a rebuild
            # would respawn the shipped children this hook just stopped.
            installed_ids = set(self._installed_plugins)
            self._installed_plugins.clear()
            for plugin_id in installed_ids:
                self._sandbox_children.pop(plugin_id, None)
            if self._registry is not None:
                shipped = {name for name, _, _ in _DECLARATIONS}
                self._registry = tuple(r for r in self._registry if r.name in shipped)
        self._sandbox_transport = None


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


# The process default registry. Direct callers -- the CLI, and the collector
# suites that drive the pipeline without an application -- keep using the
# module functions below, which delegate here. Application paths deliberately
# do not: `create_app` builds a registry per application so two of them do not
# share collectors or installations (issue #84).
_DEFAULT_REGISTRY = CollectorRegistry()


def default_registry() -> CollectorRegistry:
    """The process default registry (non-application callers)."""
    return _DEFAULT_REGISTRY


def reset_registries() -> None:
    """Stop every registry's sandboxed children and drop installed plugins.

    Teardown for suites that build applications without entering their
    lifespan: each application owns a registry now, so stopping only the
    module default would leave every application's children running. The
    registries stay tracked so a later access rebuilds cleanly; only children
    and installations are cleared. Not a runtime path -- production stops its
    one application's children in the lifespan.
    """
    for registry in _REGISTRIES:
        registry._shutdown()


# --- Module-level delegates to the process default -------------------------
#
# Kept so the existing call sites (tests, CLI, one-off scripts) do not have to
# know about the class. An application path must NOT use these; it takes the
# registry off `app.state` and calls the methods above.


def set_http_transport(collector_id: str, transport: Any) -> None:
    _DEFAULT_REGISTRY.set_http_transport(collector_id, transport)


def for_each_sandboxed_collector(apply: Callable[[str, Any], None]) -> None:
    _DEFAULT_REGISTRY.for_each_sandboxed_collector(apply)


def restore_installed_plugins(
    session_factory: Any, *, tenant_id: str = SINGLE_TENANT
) -> None:
    _DEFAULT_REGISTRY.restore_installed_plugins(session_factory, tenant_id=tenant_id)


def install_plugin(plugin_id: str, manifest: dict[str, Any]) -> None:
    _DEFAULT_REGISTRY.install_plugin(plugin_id, manifest)


def uninstall_plugin(plugin_id: str) -> None:
    _DEFAULT_REGISTRY.uninstall_plugin(plugin_id)


def installed_plugin_ids() -> tuple[str, ...]:
    return _DEFAULT_REGISTRY.installed_plugin_ids()


def forget_installed_plugins() -> None:
    _DEFAULT_REGISTRY.forget_installed_plugins()


def refresh_registry() -> tuple[Registration, ...]:
    return _DEFAULT_REGISTRY.refresh_registry()


def _registry() -> tuple[Registration, ...]:
    return _DEFAULT_REGISTRY._registry_for_build()


def select(url: str) -> Registration | None:
    return _DEFAULT_REGISTRY.select(url)


def select_for(source_type: str, url: str) -> Registration | None:
    return _DEFAULT_REGISTRY.select_for(source_type, url)


def by_name(name: str) -> Registration | None:
    return _DEFAULT_REGISTRY.by_name(name)


def matchers() -> tuple[tuple[str, Callable[[str], bool]], ...]:
    return _DEFAULT_REGISTRY.matchers()


def sync_plugin_table(session_factory: Any, *, tenant_id: str = SINGLE_TENANT) -> None:
    _DEFAULT_REGISTRY.sync_plugin_table(session_factory, tenant_id=tenant_id)


def plugins_for_surface() -> list[dict[str, object]]:
    return _DEFAULT_REGISTRY.plugins_for_surface()


# --- Pure helpers over a Registration (no registry state) ------------------


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


def collect_item_with(
    registration: Registration,
    *,
    source_id: str,
    url: str,
    client: httpx.Client,
) -> list[ItemDraft]:
    """One address of this source, or a clear refusal.

    The capability is checked *here*, before the collector is called: a source
    whose collector cannot read a single address must not have the caller's
    address list turned into requests against it (AC-168). The refusal is
    classified `structure_changed` -- this source cannot serve this shape of
    request, which is what the class means elsewhere -- so the pipeline
    records it like any other source-level failure and a route can answer
    without inventing a second vocabulary.
    """
    if not registration.item:
        raise CollectorError(
            ErrorClass.STRUCTURE_CHANGED,
            f"collector {registration.name!r} does not declare the item "
            "capability, so it cannot fetch an address on its own",
        )
    collector = cast("ItemCollectingCollector", registration.collector)
    return collector.collect_item(source_id=source_id, url=url, client=client)


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


def infer_source_type(url: str, registry: CollectorRegistry | None = None) -> str:
    """Suggest a source type from the URL, before the operator overrides it.

    A URL a sandboxed collector claims (zhihu, telegram, x.com/home) cannot be
    collected by the rss collector -- routing it as `rss` sends the rss
    collector at an HTML page and the origin answers 403 (observed when
    subscribing a Zhihu profile). So: a collector match means `web`; anything
    else stays `rss`, the default for a feed URL. `parser_plugin_id` exists for
    finer targeting; this inference only picks between rss and web.

    Lives here rather than in the MCP layer because two callers need the same
    answer: `add_source` resolves `auto` before it elicits, and
    `/v1/sources:batch` resolves it per URL (ADR 0044). One implementation, so
    the same list cannot be split two ways depending on the entry point.

    `registry` is the application's own collector registry; `None` means the
    process default, so a direct caller keeps working. An application path
    always passes its own -- otherwise a plugin installed through that
    application would not be seen here (issue #84).
    """
    for name, matcher in (registry or _DEFAULT_REGISTRY).matchers():
        if name != "rss" and _safe_match(matcher, url):
            return "web"
    return "rss"


def _safe_match(matcher: Callable[[str], bool], url: str) -> bool:
    """A matcher that raises (a malformed candidate URL) simply does not match."""
    try:
        return bool(matcher(url))
    except Exception:
        return False
