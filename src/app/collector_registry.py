"""Collector selection.

P1 has two collectors, registered here explicitly rather than discovered: the
guide defers dynamic import, manifest and lifecycle to P2, and P2 in turn waits
for three collectors before freezing a hookspec (Rule of Three).

Selection is by URL only, which is what `match_url` is for: it runs before any
fetch is scheduled, so it must not touch the network or the database.
"""

from __future__ import annotations

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
    "REGISTRY",
    "Manifest",
    "Registration",
    "by_name",
    "plugins_for_surface",
    "select",
    "select_for",
    "sync_plugin_table",
]
from plugins.collectors.rss import collector as rss
from plugins.collectors.telegram import collector as telegram
from plugins.collectors.twitter import collector as twitter
from plugins.collectors.zhihu import collector as zhihu
from plugins.collectors.zsxq import collector as zsxq


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
    name: str
    collector: Collector
    # A paged collector is walked a page at a time against stored cursor state,
    # which is a different collection path, not a loop around the same one.
    # Declared here rather than probed from the module: the presence of a
    # `page_ids` attribute says a collector happens to define a function, not
    # that collection should persist a frontier.
    paged: bool = False
    # The collector fetches its own pages (signed or cookie-authenticated
    # origins). Same reasoning as `paged`: declared here rather than probed.
    fetches: bool = False
    # The verified manifest (checksum computed by the registry at startup).
    manifest: Manifest


_PM = pluggy.PluginManager("pearss")
_PM.add_hookspecs(spec)

# The shipped set. Registration records which optional capabilities each
# collector declared — the registry verifies at import time that a collector
# declaring a capability actually implements it (ADR 0017).
_BUILTIN_MODULES: tuple[tuple[str, Any, frozenset[str]], ...] = (
    ("telegram", telegram, frozenset()),
    ("twitter", twitter, frozenset({"paged"})),
    ("zhihu", zhihu, frozenset({"fetches"})),
    ("zsxq", zsxq, frozenset({"fetches"})),
    # rss fetches too: its hook upgrades WordPress summary feeds to full text
    # via the origin's REST API, falling back to the plain payload on anything
    # it cannot upgrade.
    ("rss", rss, frozenset({"fetches"})),
)


def _build_registry() -> tuple[Registration, ...]:
    """Register every builtin module with pluggy and derive registrations.

    Capability verification is the point of this pass, in both directions:
    a manifest declaring `paged` without a `collect_pages` implementation
    (or an implementation without the declaration) is a contract violation,
    and a violated contract must fail at startup rather than at the first
    collection that needs the missing half. The verified manifest leaves
    with its checksum computed.
    """
    registrations: list[Registration] = []
    for name, module, declared in _BUILTIN_MODULES:
        _PM.register(module, name=f"collector-{name}")
        impls = {
            "fetch": bool(getattr(module, "fetch", None)),
            "collect_pages": bool(getattr(module, "collect_pages", None)),
            "backfill": bool(getattr(module, "backfill", None)),
        }
        manifest: Manifest = module.source_config()
        if manifest.id != name:
            raise RuntimeError(
                f"collector {name!r} declares manifest id {manifest.id!r}"
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
        for feature in manifest.features:
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
        registrations.append(
            Registration(
                name=name,
                collector=cast("Collector", module),
                paged="paged" in declared,
                fetches="fetches" in declared,
                manifest=manifest.with_checksum(),
            )
        )
    return tuple(registrations)


REGISTRY: tuple[Registration, ...] = _build_registry()


def select(url: str) -> Registration | None:
    """The collector whose matcher claims `url`, or None.

    URL matching alone is a heuristic: it looks for path fragments that a feed
    often but not always carries. Use `select_for` when a source type is known.
    """
    for registration in REGISTRY:
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
        (registration for registration in REGISTRY if registration.name == name),
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
        for registration in REGISTRY
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
        for registration in REGISTRY:
            manifest = registration.manifest
            upsert_plugin(
                session,
                plugin_id=manifest.id,
                manifest_json=json.dumps(
                    manifest.as_dict(), sort_keys=True, ensure_ascii=False
                ),
                checksum=manifest.checksum,
                trust="builtin",
                tenant_id=tenant_id,
                at=stamp,
            )
        session.commit()


def plugins_for_surface() -> list[dict[str, object]]:
    """Manifest summaries for the MCP/API surface (list_plugins)."""
    return [{**r.manifest.as_dict(), "checksum": r.manifest.checksum} for r in REGISTRY]
