"""The frozen collector hookspec (P2 contract, ADR 0017).

This module is the contract between the host and every collector. It is
frozen: breaking any signature here is a breaking API change and requires a
new ADR. The five shipped collectors (rss, telegram, twitter, zhihu, zsxq)
are the reference implementations the contract was generalised from.

Why this lives in core/: the hookspec is domain contract, not wiring — the
dependency rule runs `app -> plugins -> core`, so plugins import the spec
from here and the host consumes implementations through
`app.collector_registry`. pluggy itself is dependency-free and carries no
I/O, so the core purity rule (no framework, no I/O) holds.

Failure contract: `fetch` implementations may only raise
`FetchError(ErrorClass)`; `parse` implementations may only raise
`CollectorError(ErrorClass)`. A hookimpl that leaks anything else is refused
at registration (checked by the registry, not here — pluggy cannot express
exception contracts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import pluggy

if TYPE_CHECKING:
    from adapters.raw_payload import RawPayload
    from core.models import ItemDraft

HOOKSPEC = pluggy.HookspecMarker("pearss")
HOOKIMPL = pluggy.HookimplMarker("pearss")


class CollectorError(Exception):
    """A parse that failed, carrying its ErrorClass.

    The single error type a `parse` implementation may raise. Living beside
    the spec (not in each collector) removes the five identical definitions
    the pre-pluggy registry had to translate between.
    """

    def __init__(self, error_class: Any, detail: str) -> None:
        from core.enums import ErrorClass

        if not isinstance(error_class, ErrorClass):
            raise TypeError("CollectorError requires an ErrorClass")
        super().__init__(f"{error_class.value}: {detail}")
        self.error_class = error_class
        self.detail = detail


class BackfillHooks:
    """The optional backward-walk capability, declared not probed.

    A collector returns an instance of this from `backfill()`; returning
    None declares the origin has no backward walk.
    """

    def before_url(self, url: str, cursor: str) -> str | None:
        """The URL one page before `cursor`, or None when none exists."""
        ...

    def oldest_cursor(self, drafts: list[ItemDraft]) -> str | None:
        """The cursor a backfill should continue from after these drafts."""
        ...


class CollectorSpec(Protocol):
    """The collector contract. See module docstring for freezing rules.

    A Protocol: declaration-only methods are its native form, and the
    registry verifies hookimpls structurally against it. Implementations
    live in the collectors and are invoked by pluggy, never by calling
    these.
    """

    @HOOKSPEC
    def collector_id(self) -> str:
        """Stable identifier ('rss', 'twitter', ...), the registration key."""
        ...

    @HOOKSPEC
    def match_url(self, url: str) -> bool:
        """Whether this collector handles `url`. Pure: no I/O, no database."""
        ...

    @HOOKSPEC
    def parse(self, raw: RawPayload) -> list[ItemDraft]:
        """Turn a fetched payload into ItemDrafts.

        Raises CollectorError with a classified reason; an empty list means
        the origin genuinely has no entries.
        """
        ...

    @HOOKSPEC
    def source_config(self) -> dict[str, Any]:
        """The collector's manifest (P2b fills the full shape).

        Required so the registry can verify declared capabilities against
        what the implementation actually provides.
        """
        ...

    # ---- optional capabilities ----

    @HOOKSPEC
    def fetch(
        self,
        url: str,
        *,
        client: Any,
        source_id: str,
        conditional: Any = None,
    ) -> RawPayload:
        """Fetch a page through the collector's own transport path.

        Implemented by collectors whose origins need signed or
        cookie-authenticated requests (zhihu, zsxq) or an upgrade pass
        (rss's WordPress fulltext). May only raise FetchError.
        """
        ...

    @HOOKSPEC
    def collect_pages(
        self,
        *,
        source_id: str,
        source_url: str,
        client: Any,
        state: Any,
        max_pages: int = 20,
    ) -> Any:
        """Walk a paged origin (the twitter capability).

        The host owns paging state end to end: it passes `state` in and
        persists the result's frontier/resume values.
        """
        ...

    @HOOKSPEC
    def backfill(self) -> BackfillHooks | None:
        """The backward-walk capability, or None (most origins have none).

        Declared, replacing the old hasattr-probe: a collector that defines
        `before_url` without declaring it here does not gain the capability.
        """
        ...


# The real verification happens at startup: the registry registers five
# implementations against this spec, which the suite exercises on every run.
