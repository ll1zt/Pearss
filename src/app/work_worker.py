"""The work loop: claim a requested batch, execute it, record every unit.

A work is a job (ADR 0046), so something has to own it. This loop claims the
oldest executable work, runs one unit per target, appends a ledger row for each,
and closes the work with a terminal status. It mirrors `EnrichmentWorker`'s
shape (a task, a lock, a liveness watch) because the same questions apply: who
notices it died, and who stops it at shutdown.

Three decisions worth naming:

- **A ledger row is written and committed per target.** A crash therefore leaves
  the work in `running` with the rows it managed to write, and the next pass
  resumes exactly the targets it has no row for. Progress is a fact in the
  database rather than a variable in this process.
- **`blocked` ends the pass.** An origin answering 403/429 is asking this
  deployment to stop; the remaining targets are recorded as `rejected` without
  being attempted. Continuing is the burst the pacing exists to prevent.
- **Only kinds with an executor are claimed.** A `delete` work submitted before
  its executor ships stays queued rather than being claimed and failed, so
  accepting a kind and implementing it can be two separate changes.

Pacing reuses the batch entry point's rules (`pipeline.fetch_pacing`): the
work's own `batch_size`/`min_interval_seconds` shape the bursts, and the
deployment's interval is a floor under them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from adapters import correlation
from adapters.db_models import Source as SourceRow
from adapters.log_safety import escape_control_characters
from adapters.redaction import redact_url_for_log
from adapters.repository import (
    append_unit,
    chain_for_source,
    claim_next_work,
    counts_for_work,
    create_source,
    delete_items,
    derive_source_id,
    enqueue_chain,
    enrichment_status,
    finish_work,
    get_item,
    get_source,
    parse_chain,
    requeue_enrichment,
    timestamp,
    units_for_work,
)
from app import collector_registry
from app.collector_registry import CollectorRegistry, infer_source_type
from app.collector_registry import Registration as CollectorRegistration
from app.ingest import IngestItem, ingest_draft
from app.liveness_watch import LoopWatch, always_alive
from app.pipeline import fetch_one, fetch_pacing, normalise_and_store, outcome_for
from core.enrichment import EnrichmentSpec, EnrichmentStatus
from core.enums import ErrorClass, SourceType, UnitOutcome
from core.folders import DEFAULT_FOLDER
from core.models import schedule_policy_from_json, schedule_policy_to_json
from core.normalize import DedupeAction, origin_canonical_key
from core.tenancy import SINGLE_TENANT
from core.work import WorkRecord, terminal_status

LOGGER = logging.getLogger(__name__)

# Kinds whose target is the payload itself rather than an address for it.
# Printing one would put a whole article in the journal (AC-298).
_PAYLOAD_TARGET_KINDS: Final = frozenset({"ingest"})


def _target_for_log(kind: str, target: str) -> str:
    """One work unit's target, as it should appear in a DEBUG line.

    A target is a string the caller chose, so it is untrusted: it can carry a
    token in its query and a newline anywhere, and either one would leak a
    credential or forge a journal line (AC-298). An ``ingest`` target is the
    item itself -- canonical JSON, body included -- so it is summarised rather
    than printed. The ledger row still holds the full target, so nothing is
    lost; it is only kept out of the journal.
    """
    if kind in _PAYLOAD_TARGET_KINDS:
        return f"<{kind} payload {len(target)} bytes>"
    return escape_control_characters(redact_url_for_log(target))


# How often the loop looks for work. A work is not on a schedule, so this is
# only "how long may a freshly submitted batch wait"; the work itself takes as
# long as its targets take.
DEFAULT_WORK_SWEEP_SECONDS: Final = 30.0

# How long a `running` work may stay silent before another executor may take it
# (issue #212). A pass that is fetching writes a ledger row per target, so a
# silent work is either dead or between two targets -- and the gap between two
# targets is bounded by the egress client's per-request timeout plus the pacing
# interval, i.e. minutes, not seconds. Fifteen minutes leaves a slow origin room
# and still reclaims a pass whose process died the same afternoon. Zero disables
# the lease (every `running` work is reclaimable), which is the pre-#212
# behaviour and the escape hatch for a work somebody needs resumed now.
WORK_LEASE_ENV: Final = "PEARSS_WORK_LEASE_SECONDS"
DEFAULT_WORK_LEASE_SECONDS: Final = 900.0


def work_lease_seconds() -> float:
    """The lease window, in seconds. `0` means "take any running work".

    A malformed value means the default, never "no lease": a typo must not
    quietly remove the protection (the same reading the retention settings have).
    """
    raw = os.environ.get(WORK_LEASE_ENV, "").strip()
    if not raw:
        return DEFAULT_WORK_LEASE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_WORK_LEASE_SECONDS


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitResult:
    """What one target's execution produced, in the ledger's own terms.

    One vocabulary, and it is the ledger's: an executor says what happened to
    its target, not what a response should look like. `rejected` and `failed`
    stay apart for the reason they do everywhere else -- only one of them is
    worth re-sending.
    """

    outcome: UnitOutcome
    error_class: ErrorClass | None = None
    detail: str = ""
    ref: str | None = None
    # "Do not attempt what is left of this list." Only an origin refusing this
    # deployment sets it, and `stop_detail` says why in the rows that follow.
    stop: bool = False
    stop_detail: str = ""
    # Run only once this target's ledger row is committed, never before:
    # deletion's file reclaim is exactly this (files removed inside a
    # transaction cannot come back).
    after_commit: Callable[[], int] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkRuntime:
    """Whatever a kind resolved once, before its first target.

    The base carries nothing on purpose: a kind that needs something defines a
    subclass and returns it from `prepare`, so `run` receives values that cannot
    be missing and cannot disagree with what was resolved. Typed `Any` at the
    call site (the base signature), concrete in each subclass -- the worker's
    loop is generic over kinds, and the type lives where the knowledge does.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRuntime(WorkRuntime):
    """A work about one source: the source, and whatever the kind needs of it."""

    source: SourceRow
    registration: CollectorRegistration | None = None
    chain: tuple[EnrichmentSpec, ...] = ()


class KindExecutor:
    """One work kind: a preflight for the whole work, then one target at a time.

    Splitting the two is what lets a kind resolve what it needs once (a source,
    its collector, its chain) while still recording a row per target -- and what
    keeps the worker's loop free of anything kind-specific.
    """

    def prepare(
        self, session: Session, work: WorkRecord, registry: CollectorRegistry
    ) -> str | WorkRuntime:
        """A reason *every* target will fail, or the runtime the targets need.

        A work naming something this deployment does not have is refused here
        rather than per target: nothing about a target could have made it work.
        """
        return WorkRuntime()

    def run(
        self,
        session: Session,
        work: WorkRecord,
        target: str,
        *,
        runtime: Any,
        client: httpx.Client,
    ) -> UnitResult:
        """Execute one target. A classified failure is a return value, not a raise."""
        raise NotImplementedError

    def pacing(self, work: WorkRecord) -> tuple[int, float]:
        """(batch size, interval). The default paces nothing: only a kind that
        makes real outbound requests has a reason to wait."""
        return (1, 0.0)


def _source_runtime(
    session: Session, work: WorkRecord, registry: CollectorRegistry
) -> str | SourceRuntime:
    """The source a work names, or the reason there is none.

    One reader for both `prepare` and `run`, so the two cannot disagree about
    which source a work is about.
    """
    source_id = str(work.params.get("source_id") or "")
    source = get_source(session, source_id) if source_id else None
    if source is None:
        return f"no source {source_id!r}"
    registration = registry.select_for(
        source.type, source.url or source.canonical_url or ""
    )
    return SourceRuntime(
        source=source,
        registration=registration,
        chain=chain_for_source(session, source_id=source.id),
    )


class FetchExecutor(KindExecutor):
    """Addresses through the source's collector (issue #190).

    The only kind that makes outbound requests, so the only one that paces.
    """

    def prepare(
        self, session: Session, work: WorkRecord, registry: CollectorRegistry
    ) -> str | SourceRuntime:
        resolved = _source_runtime(session, work, registry)
        if isinstance(resolved, str):
            return resolved
        if resolved.registration is None or not resolved.registration.item:
            return "the collector for this source cannot fetch a single address"
        return resolved

    def pacing(self, work: WorkRecord) -> tuple[int, float]:
        return fetch_pacing(work.params)

    def run(
        self,
        session: Session,
        work: WorkRecord,
        target: str,
        *,
        runtime: SourceRuntime,
        client: httpx.Client,
    ) -> UnitResult:
        unit = fetch_one(
            session,
            source=runtime.source,
            registration=runtime.registration,
            client=client,
            url=target,
        )
        blocked = unit.error_class is ErrorClass.BLOCKED
        return UnitResult(
            outcome=outcome_for(unit.action),
            error_class=unit.error_class,
            detail=unit.detail,
            ref=unit.item_id,
            stop=blocked,
            stop_detail=(
                "not attempted: the origin refused this address" if blocked else ""
            ),
        )


class DeleteExecutor(KindExecutor):
    """Items by id, deleted with everything that only existed because of them.

    A target that is already gone is `skipped`, not `failed`: the end state the
    caller asked for is true, and a client retrying a lost response must see the
    same success the first attempt produced (which is why the endpoint has been
    idempotent all along).
    """

    def run(
        self,
        session: Session,
        work: WorkRecord,
        target: str,
        *,
        runtime: WorkRuntime,
        client: httpx.Client,
    ) -> UnitResult:
        result = delete_items(session, [target])
        if result.counts["items"] == 0:
            return UnitResult(outcome=UnitOutcome.SKIPPED, detail="already gone")
        return UnitResult(
            outcome=UnitOutcome.OK,
            detail=(
                f"deleted {result.counts['items']} item(s), "
                f"{result.counts['enrichments']} product(s), "
                f"{result.counts['read_states']} state row(s)"
            ),
            after_commit=result.reclaim.reclaim,
        )


class EnrichExecutor(KindExecutor):
    """One source's chain queued for each named item (issue #197).

    The per-item half of `backfill_chain`: that function selects the items and
    queues the same rows for each, so a work target and a backfilled item go
    through one rule -- idempotent on the (item, plugin, version, kind, lang,
    input_hash) identity, which is why "already queued" is `skipped` and not a
    failure.
    """

    def prepare(
        self, session: Session, work: WorkRecord, registry: CollectorRegistry
    ) -> str | SourceRuntime:
        resolved = _source_runtime(session, work, registry)
        if isinstance(resolved, str):
            return resolved
        if not resolved.chain:
            return "the source has no pipeline_chain; there is nothing to queue"
        return resolved

    def run(
        self,
        session: Session,
        work: WorkRecord,
        target: str,
        *,
        runtime: SourceRuntime,
        client: httpx.Client,
    ) -> UnitResult:
        item = get_item(session, target)
        if item is None:
            return UnitResult(
                outcome=UnitOutcome.FAILED,
                error_class=ErrorClass.STRUCTURE_CHANGED,
                detail=f"no item {target!r}",
            )
        queued = enqueue_chain(
            session,
            item_id=item.id,
            content_hash=item.content_hash,
            chain=runtime.chain,
        )
        if queued == 0:
            return UnitResult(
                outcome=UnitOutcome.SKIPPED, detail="its products are already queued"
            )
        return UnitResult(outcome=UnitOutcome.OK, detail=f"queued {queued} product(s)")


class RetryExecutor(KindExecutor):
    """Failed chain rows, requeued one at a time (issue #195's rule, per target)."""

    def run(
        self,
        session: Session,
        work: WorkRecord,
        target: str,
        *,
        runtime: WorkRuntime,
        client: httpx.Client,
    ) -> UnitResult:
        status = enrichment_status(session, target)
        if status is None:
            return UnitResult(
                outcome=UnitOutcome.FAILED,
                error_class=ErrorClass.STRUCTURE_CHANGED,
                detail=f"no enrichment row {target!r}",
            )
        if status == EnrichmentStatus.SUCCEEDED.value:
            # The product exists, so the end state the caller wanted is already
            # true; producing it again is a different command.
            return UnitResult(
                outcome=UnitOutcome.SKIPPED,
                detail="already succeeded: its product exists",
            )
        if not requeue_enrichment(session, target):
            return UnitResult(outcome=UnitOutcome.SKIPPED, detail=f"already {status}")
        return UnitResult(outcome=UnitOutcome.OK, detail="requeued")


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscribeRuntime(WorkRuntime):
    """A subscription batch: the shared options, resolved and validated once.

    "Shared" is the point. A policy or a chain this deployment cannot use is not
    a per-URL accident, so it refuses the whole work in `prepare` rather than
    failing two hundred targets one at a time.
    """

    policy_json: str
    requested_type: str
    folder: str
    trust_level: str
    parser_plugin_id: str
    chain_json: str | None
    target_lang: str
    at: str
    registry: CollectorRegistry


class SubscribeExecutor(KindExecutor):
    """URLs become sources (issue #220).

    The per-URL half of `POST /v1/sources:batch`, with the same division of
    blame: the options are judged once for the work, while each URL is judged
    alone -- refusing a whole list over one typo is the loop the batch endpoint
    exists to break. A URL that is already subscribed is `skipped`, because the
    end state the caller asked for is true.
    """

    def prepare(
        self, session: Session, work: WorkRecord, registry: CollectorRegistry
    ) -> str | SubscribeRuntime:
        del session
        params = work.params
        raw_policy = params.get("schedule_policy")
        if not isinstance(raw_policy, (str, dict)):
            return "the work carries no schedule_policy (a subscription needs one)"
        try:
            policy_json = schedule_policy_to_json(schedule_policy_from_json(raw_policy))
        except (ValueError, TypeError) as exc:
            return f"unusable schedule_policy: {exc}"

        requested_type = str(params.get("type") or "auto").strip().lower()
        if requested_type not in {"auto", "rss", "web", "agent"}:
            return f"unsupported source type {requested_type!r}"

        target_lang = str(params.get("target_lang") or "")
        chain = params.get("pipeline_chain") or None
        chain_json: str | None = None
        if chain:
            # Same gate-time guard as the endpoint: a chain naming a processor
            # this deployment cannot run is refused before any source exists.
            from app.enrichment_worker import registered_processor_ids

            try:
                parse_chain(
                    json.dumps(chain),
                    target_lang=target_lang,
                    known_plugins=registered_processor_ids(),
                )
            except (ValueError, KeyError) as exc:
                return f"unusable pipeline_chain: {exc}"
            chain_json = json.dumps(chain)

        raw_trust = str(params.get("trust_level") or "standard")
        return SubscribeRuntime(
            policy_json=policy_json,
            requested_type=requested_type,
            folder=str(params.get("folder") or DEFAULT_FOLDER),
            trust_level=raw_trust,
            parser_plugin_id=str(params.get("parser_plugin_id") or ""),
            chain_json=chain_json,
            target_lang=target_lang,
            at=timestamp(datetime.now(UTC)),
            registry=registry,
        )

    def run(
        self,
        session: Session,
        work: WorkRecord,
        target: str,
        *,
        runtime: SubscribeRuntime,
        client: httpx.Client,
    ) -> UnitResult:
        del work, client
        try:
            source_type = (
                infer_source_type(target, runtime.registry)
                if runtime.requested_type == "auto"
                else runtime.requested_type
            )
            canonical = origin_canonical_key(source_type=source_type, url=target)
        except ValueError as exc:
            # This URL alone: the rest of the list is still subscribed.
            return UnitResult(
                outcome=UnitOutcome.FAILED,
                error_class=ErrorClass.PARSE,
                detail=str(exc),
            )

        source_id = derive_source_id(
            tenant_id=SINGLE_TENANT,
            source_type=source_type,
            canonical_url=canonical,
        )
        existed = session.get(SourceRow, source_id) is not None
        create_source(
            session,
            source_type=source_type,
            url=target,
            canonical_url=canonical,
            schedule_policy=runtime.policy_json,
            tenant_id=SINGLE_TENANT,
            trust_level=runtime.trust_level,
            parser_plugin_id=runtime.parser_plugin_id,
            pipeline_chain=runtime.chain_json,
            target_lang=runtime.target_lang,
            folder=runtime.folder,
            at=runtime.at,
        )
        return UnitResult(
            outcome=UnitOutcome.SKIPPED if existed else UnitOutcome.OK,
            detail="already subscribed" if existed else f"subscribed as {source_type}",
            ref=source_id,
        )


class IngestExecutor(KindExecutor):
    """Agent-submitted content (issue #220), one target at a time.

    A target is one item as canonical JSON. The validation and the identity rule
    are the endpoint's (`app.ingest`, shared on purpose: two copies would drift
    into two identities for the same content), and the write goes through
    `normalise_and_store` -- the same call a collector's parsed output takes,
    with the same sanitising, six-way dedupe, gate and index.
    """

    def prepare(
        self, session: Session, work: WorkRecord, registry: CollectorRegistry
    ) -> str | SourceRuntime:
        resolved = _source_runtime(session, work, registry)
        if isinstance(resolved, str):
            return resolved
        if resolved.source.type != SourceType.AGENT.value:
            return (
                f"source {resolved.source.id!r} is {resolved.source.type!r}; only "
                "agent sources accept pushed items"
            )
        return resolved

    def run(
        self,
        session: Session,
        work: WorkRecord,
        target: str,
        *,
        runtime: SourceRuntime,
        client: httpx.Client,
    ) -> UnitResult:
        del work, client
        try:
            item = IngestItem.model_validate_json(target)
        except ValidationError as exc:
            return UnitResult(
                outcome=UnitOutcome.FAILED,
                error_class=ErrorClass.PARSE,
                detail=(
                    f"not a usable item: {exc.error_count()} validation error(s), "
                    f"first: {exc.errors()[0].get('loc')}"
                ),
            )
        draft = ingest_draft(
            item,
            source_id=runtime.source.id,
            source_url=runtime.source.canonical_url,
        )
        ingested = normalise_and_store(
            session,
            [draft],
            tenant_id=runtime.source.tenant_id,
            chain=runtime.chain,
        )
        [entry] = ingested
        # Written versus "already there": the first is work done, the second is
        # the rule reporting that the corpus already holds this article. Both
        # carry the dedupe's own reason, which is what a caller needs to tell
        # them apart without reading the store.
        written = entry.outcome.action in (
            DedupeAction.INSERT,
            DedupeAction.INSERT_RELATED,
            DedupeAction.UPDATE_PENDING,
        )
        return UnitResult(
            outcome=UnitOutcome.OK if written else UnitOutcome.SKIPPED,
            detail=entry.outcome.reason or ("stored" if written else "already present"),
            ref=entry.result.item.id,
        )


# One entry per kind this host can execute. Adding one is a deliberate act: the
# claim filters on this map's keys, so a kind without an executor waits instead
# of being claimed and failed.
EXECUTORS: Final[dict[str, KindExecutor]] = {
    "fetch": FetchExecutor(),
    "delete": DeleteExecutor(),
    "enrich": EnrichExecutor(),
    "retry": RetryExecutor(),
    "subscribe": SubscribeExecutor(),
    "ingest": IngestExecutor(),
}

EXECUTABLE_KINDS: Final[frozenset[str]] = frozenset(EXECUTORS)


class WorkWorker:
    """The loop that owns requested batches."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        client: httpx.Client,
        registry: CollectorRegistry | None = None,
        sweep_seconds: float = DEFAULT_WORK_SWEEP_SECONDS,
        watch: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        # The caller's client, for the same reason the pipeline takes one: the
        # egress policy (timeouts, size caps, SSRF guard) lives in it, and a
        # worker that built its own would be a second, unaudited way out.
        self._client = client
        self._registry = registry
        self._sweep_seconds = sweep_seconds
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._watch = LoopWatch(name="work-worker", watch=watch or always_alive)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # A dead task's reference must not block a restart (A07 liveness).
        self._task = None
        self._task = asyncio.create_task(self._loop(), name="pearss-work")
        LOGGER.info("work worker started, sweeping every %.0fs", self._sweep_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:  # a failed task must not block shutdown
            LOGGER.exception("work task failed")
        self._task = None
        LOGGER.info("work worker stopped")

    def liveness(self) -> tuple[bool, str]:
        """Whether the loop is alive, with a name for diagnostics.

        Same contract as `Scheduler.liveness`: a coroutine that died of an
        escaped error still exists as an object, so `done()` is the only honest
        signal the health endpoint can aggregate.
        """
        if self._task is None:
            return True, ""  # not started (or stopped): not a degradation
        return not self._task.done(), "work"

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_seconds)
            # One transient storage failure must not kill the loop; a
            # programming error or a cancellation must.
            try:
                await self.sweep_once()
            except Exception:
                LOGGER.exception("work sweep failed; continuing")
            try:
                self._watch.check()
            except Exception:
                LOGGER.exception("liveness watch failed; continuing")

    async def sweep_once(self) -> dict[str, int]:
        """One pass: claim at most one work and execute it to completion.

        One work per pass, not N: the caller is waiting on *its* batch, and a
        work's units are the thing that has to finish. The database work runs in
        a thread so the event loop keeps serving requests.
        """
        async with self._lock:
            return await asyncio.to_thread(self._sweep)

    def _sweep(self) -> dict[str, int]:
        registry = self._registry or collector_registry.default_registry()
        stale_before = datetime.now(UTC) - timedelta(seconds=work_lease_seconds())
        with self._session_factory() as session:
            work = claim_next_work(
                session, kinds=EXECUTABLE_KINDS, stale_before=stale_before
            )
            session.commit()
        if work is None:
            # DEBUG, not INFO: an empty sweep is the normal state, and a line
            # per poll at the default level is exactly the flood issue #297
            # forbids (issue #296).
            LOGGER.debug("no work to claim")
            return {"works": 0, "units": 0}
        # One claimed work is one job: the executors' per-unit lines and this
        # worker's own lines group under a single id (AC-306). Fresh per work
        # because `WorkRecord` carries no correlation id from the request that
        # created it -- a limitation worth its own issue, not a reason to leave
        # every background line with an id of its own.
        correlation.set_correlation_id(correlation.new_correlation_id())
        units = self._run_work(work, registry)
        return {"works": 1, "units": units}

    def _run_work(self, work: WorkRecord, registry: CollectorRegistry) -> int:
        """Execute the work's remaining targets, then close it. Returns the count.

        Re-entrant by design: targets that already have a ledger row are skipped,
        which is what makes a crashed pass resumable rather than a restart.

        Everything kind-specific lives in an executor (see EXECUTORS). What is
        here is what every kind shares: resolve once, record each outcome as it
        happens, and stop when the world says stop.
        """
        executor = EXECUTORS[work.kind]
        written = 0
        with self._session_factory() as session:
            prepared = executor.prepare(session, work, registry)
            if isinstance(prepared, str):
                # The work names something this deployment cannot use, so every
                # target is accounted for rather than left queued forever.
                for target in work.targets:
                    append_unit(
                        session,
                        work_id=work.id,
                        target=target,
                        outcome=UnitOutcome.FAILED,
                        error_class=ErrorClass.STRUCTURE_CHANGED,
                        detail=prepared,
                    )
                    LOGGER.debug(
                        "work %s unit %s -> %s (%s)",
                        work.id,
                        _target_for_log(work.kind, target),
                        UnitOutcome.FAILED.value,
                        escape_control_characters(prepared),
                    )
                session.commit()
                self._close(session, work.id)
                return len(work.targets)

            done = {row.target for row in units_for_work(session, work.id)}
            batch_size, interval = executor.pacing(work)
            for index, target in enumerate(work.targets):
                if target in done:
                    continue
                if index and index % batch_size == 0:
                    time.sleep(interval)
                result = executor.run(
                    session, work, target, runtime=prepared, client=self._client
                )
                append_unit(
                    session,
                    work_id=work.id,
                    target=target,
                    outcome=result.outcome,
                    error_class=result.error_class,
                    detail=result.detail,
                    ref=result.ref,
                )
                session.commit()
                if result.after_commit is not None:
                    result.after_commit()
                written += 1
                # One line per ledger row, at DEBUG: the finish summary reports
                # counts, and "which target failed and why" is per unit
                # (issue #292).
                LOGGER.debug(
                    "work %s unit %s -> %s (%s)",
                    work.id,
                    _target_for_log(work.kind, target),
                    result.outcome.value,
                    escape_control_characters(result.detail or "-"),
                )
                if result.stop:
                    for rest in work.targets[index + 1 :]:
                        if rest in done:
                            continue
                        append_unit(
                            session,
                            work_id=work.id,
                            target=rest,
                            outcome=UnitOutcome.REJECTED,
                            error_class=ErrorClass.BLOCKED,
                            detail=result.stop_detail,
                        )
                        LOGGER.debug(
                            "work %s unit %s -> %s (%s)",
                            work.id,
                            _target_for_log(work.kind, rest),
                            UnitOutcome.REJECTED.value,
                            escape_control_characters(result.stop_detail or "-"),
                        )
                    session.commit()
                    break

            self._close(session, work.id)
        return written

    def _close(self, session: Session, work_id: str) -> None:
        """Finish the work with the status its ledger implies."""
        counts = counts_for_work(session, work_id)
        status = terminal_status(counts)
        finish_work(session, work_id, status=status)
        session.commit()
        LOGGER.info(
            "work %s finished %s (%d ok, %d rejected, %d failed, %d skipped)",
            work_id,
            status.value,
            counts["ok"],
            counts["rejected"],
            counts["failed"],
            counts["skipped"],
        )
