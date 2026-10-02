"""The frozen collector hookspec (P2 contract, ADR 0017) and the P3
processor contract.

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
`FetchError(ErrorClass)`; `parse` and `collect_item` implementations may only
raise `CollectorError(ErrorClass)` -- `collect_item` both fetches and parses
one address, so it may raise either. A hookimpl that leaks anything else is
refused at registration (checked by the registry, not here — pluggy cannot
express exception contracts).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Protocol

import pluggy

if TYPE_CHECKING:
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


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfigRequirement:
    """One environment setting a collector needs, per its manifest.

    `required=False` marks optional tuning knobs. Names only — values are
    read from the environment by the host, never carried in the manifest
    (a manifest lands in the plugin table and the MCP tool surface).
    """

    env_name: str
    description: str
    required: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class Manifest:
    """The declarative identity of a collector (E2: 声明式清单).

    The registry verifies at startup that a manifest's `features` match the
    collector's actual hookimpls — a declared capability without an
    implementation is a startup failure, not a silent gap. `checksum` is
    computed over the canonical JSON of everything else, so any edit to a
    shipped manifest is visible.
    """

    id: str
    version: str
    name: str
    # What a source of this collector calls itself when the origin carries no
    # title (paged timelines have no channel element). None = derive from id.
    display_title: str | None = None
    description: str
    example_url: str
    features: frozenset[str]
    required_config: tuple[ConfigRequirement, ...]
    # Populated by the registry, not declared by the collector.
    checksum: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The surface form of the manifest (API, MCP, stored JSON)."""
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "display_title": self.display_title,
            "description": self.description,
            "example_url": self.example_url,
            "features": sorted(self.features),
            "required_config": [
                {
                    "env_name": req.env_name,
                    "description": req.description,
                    "required": req.required,
                }
                for req in self.required_config
            ],
        }

    def canonical_json(self) -> str:
        """The checksummed form: `as_dict` serialised deterministically."""
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def with_checksum(self) -> Manifest:
        digest = hashlib.sha256(self.canonical_json().encode()).hexdigest()
        return replace(self, checksum=digest)


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
    def parse(self, raw: Any) -> list[ItemDraft]:
        """Turn a fetched payload into ItemDrafts.

        Raises CollectorError with a classified reason; an empty list means
        the origin genuinely has no entries.
        """
        ...

    @HOOKSPEC
    def source_config(self) -> Manifest:
        """The collector's manifest: identity, capabilities, config needs.

        The registry verifies declared capabilities against what the
        implementation actually provides, computes the checksum, and records
        the manifest in the plugin table at startup.
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
    ) -> Any:
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
    def collect_item(
        self,
        *,
        source_id: str,
        url: str,
        client: Any,
    ) -> list[ItemDraft]:
        """Turn one already-known address into drafts (the item capability).

        The caller supplies the address -- an article URL it discovered
        outside the host -- instead of the source URL every other path starts
        from, so the collector owns both the fetch and the parse of it. A
        collector that can only read its own listing does not implement this,
        and the registry's gate refuses the call rather than letting a
        caller's address list become requests against an origin that never
        agreed to them.
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


# --------------------------------------------------------------------------
# Processor contract (P3): derived artifacts such as translation.
#
# Separate from CollectorSpec on purpose: a processor consumes stored items
# and produces side files, has no URL, and fails on provider/auth terms the
# collector taxonomy already covers. Same freezing rules apply — the registry
# verifies hookimpls against this spec at startup.
# --------------------------------------------------------------------------


# Keys the runtime places in a processor's `item_ctx` besides the item's own
# fields. They are named here because the runtime (`app/processor_runtime`) and
# the processors (`plugins/...`) both read them, and neither layer may import
# the other:
#
# - `DEADLINE_KEY`: the absolute `time.monotonic()` instant by which `process`
#   must return (ADR 0047). Absent for an in-process call.
# - `MODEL_POLICY_KEY`: the worker's model-health policy for this row, e.g.
#   `{"skip": ["m1"], "attempts_per_model": 2}` (ADR 0048).
# - `MODEL_EVENTS_KEY`: the report channel back. The runtime creates the list; a
#   processor appends `(model, error_class | None)` for each model it asked, and
#   the runtime forwards it to the worker's health registry (ADR 0048).
DEADLINE_KEY: Final = "deadline"
MODEL_POLICY_KEY: Final = "model_policy"
MODEL_EVENTS_KEY: Final = "model_events"


class ProcessorError(Exception):
    """A classified processor failure: a value the gate can act on.

    Same shape as CollectorError: the class drives the run record and the
    operator's diagnostics, not the stack trace.
    """

    def __init__(self, error_class: Any, detail: str) -> None:
        from core.enums import ErrorClass

        if not isinstance(error_class, ErrorClass):
            raise TypeError("ProcessorError requires an ErrorClass")
        super().__init__(f"{error_class.value}: {detail}")
        self.error_class = error_class
        self.detail = detail


class ProcessorSpec(Protocol):
    """The processor contract: item context in, artifact path out.

    `process` writes its product to the file store through the injected
    store callable and returns the resulting reference, or raises
    ProcessorError. It never touches the database — the worker owns the
    row's lifecycle (queued -> running -> succeeded/failed) exactly as the
    pipeline owns the item's.
    """

    @HOOKSPEC
    def processor_id(self) -> str:
        """The stable registration key."""
        ...

    @HOOKSPEC
    def processor_version(self) -> str:
        """Semver of the implementation; changes re-process everything."""
        ...

    @HOOKSPEC
    def process(
        self,
        item_ctx: Any,
        *,
        target_lang: str,
        store: Any,
    ) -> str:
        """Produce the artifact; return its storage reference.

        `item_ctx` carries the fields the processor may read (title,
        content HTML, url, lang hints) and, when the caller enforces a
        deadline, `deadline`: the absolute `time.monotonic()` instant by which
        `process` must return. The runtime adds it (ADR 0047); an in-process
        call has none. `store` is the file-store callable.
        """
        ...
