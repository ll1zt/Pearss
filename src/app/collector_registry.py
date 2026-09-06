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
from typing import Protocol, cast

import httpx

from adapters.http import fetch as plain_fetch
from adapters.raw_payload import ConditionalState, RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
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
        self, url: str, *, client: httpx.Client, source_id: str
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


# Order matters: the first match wins, so the more specific matcher goes first.
# Telegram is checked before RSS because a t.me URL is unambiguous, whereas the
# RSS matcher looks for path fragments that a channel URL could theoretically
# contain. Zhihu is host-scoped and unambiguous for the same reason.
REGISTRY: tuple[Registration, ...] = (
    Registration(name="telegram", collector=telegram),
    Registration(name="twitter", collector=twitter, paged=True),
    Registration(name="zhihu", collector=zhihu, fetches=True),
    Registration(name="zsxq", collector=zsxq, fetches=True),
    Registration(name="rss", collector=rss),
)


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
    try:
        return registration.collector.parse(raw)
    except (
        rss.CollectorError,
        telegram.CollectorError,
        zsxq.CollectorError,
        zhihu.CollectorError,
    ) as exc:
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

    The conditional state is only meaningful to the plain path: a signed API
    fetch replays no ETag, so the hook's signature omits it rather than
    accepting an argument it would have to ignore.
    """
    if registration.fetches:
        fetcher = cast("FetchingCollector", registration.collector)
        return fetcher.fetch(url, client=client, source_id=source_id)
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
    """Whether this collector can walk backwards through its origin's history.

    Probed rather than declared beside each registry entry, because a flag
    there would have to be kept in sync with the module it describes, and the
    one place that must agree is the module itself.

    Both helpers are required: a cursor reader without a URL builder would
    stall on the first page, and a URL builder with nothing to build from can
    only repeat the page it started on.
    """
    return hasattr(registration.collector, "before_url") and hasattr(
        registration.collector, "oldest_cursor"
    )


def matchers() -> tuple[tuple[str, Callable[[str], bool]], ...]:
    """Every registered matcher, for diagnostics and tests."""
    return tuple(
        (registration.name, registration.collector.match_url)
        for registration in REGISTRY
    )
