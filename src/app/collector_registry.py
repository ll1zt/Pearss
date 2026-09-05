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

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
from plugins.collectors.rss import collector as rss
from plugins.collectors.telegram import collector as telegram


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


# Order matters: the first match wins, so the more specific matcher goes first.
# Telegram is checked before RSS because a t.me URL is unambiguous, whereas the
# RSS matcher looks for path fragments that a channel URL could theoretically
# contain.
REGISTRY: tuple[Registration, ...] = (
    Registration(name="telegram", collector=telegram),
    Registration(name="rss", collector=rss),
)


def select(url: str) -> Registration | None:
    """The collector registered for `url`, or None."""
    for registration in REGISTRY:
        if registration.collector.match_url(url):
            return registration
    return None


def parse_with(registration: Registration, raw: RawPayload) -> list[ItemDraft]:
    """Parse `raw`, normalising the two collectors' error types into one.

    Each collector defines its own `CollectorError` with the same shape, so a
    caller would otherwise need to catch both. Translating here keeps that
    duplication out of the pipeline, and is the seam a P2 hookspec replaces.
    """
    try:
        return registration.collector.parse(raw)
    except (rss.CollectorError, telegram.CollectorError) as exc:
        raise CollectorError(exc.error_class, exc.detail) from exc


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
