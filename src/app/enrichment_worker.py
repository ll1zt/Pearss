"""The enrichment worker: runs queued processor jobs, opens the gate.

Second loop beside the collection scheduler, same shape (asyncio task over
a sync worker in a thread). It claims `queued` rows one at a time, calls the
matching processor, and on completion of an item's whole chain promotes the
item to ready — the publish gate's `processing_complete` contract finally
having a producer.

Failure semantics (ADR 0018): a failed required enrichment stays pending
with its error_class recorded; the worker does not spin on it. Rows are
retried only when the item's content changes (new input_hash) or the
operator re-triggers, because a failing provider charges real money per
attempt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import CursorResult, and_, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from adapters import filestore
from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment
from adapters.repository import timestamp
from app.fetch_pacing import PACED_PROCESSORS, host_of, pacer
from app.processor_runtime import LEASE_GRACE_SECONDS, run_processor
from core.enrichment import EnrichmentStatus
from core.enums import ErrorClass, ItemEvent, PublishStatus
from core.item_state import TransitionContext, transition
from core.plugins.hooks import ProcessorError

LOGGER = logging.getLogger(__name__)

TENANT: Final = "default"
# Poll cadence: queued rows appear at collection time, so a minute of lag is
# invisible next to the fetch interval that produced them.
DEFAULT_SWEEP_SECONDS: Final = 60.0
# Generous: an LLM completion regularly takes minutes through a relayed
# endpoint (observed 126s for one gemini-flash reply). The whole attempt,
# including imports, chunks and fallback, shares this budget; leases add grace.
DEFAULT_CALL_TIMEOUT: Final = 360.0
# How many rows one sweep processes in parallel. Each worker runs in its own
# thread with its own session; SQLite handles short writes from a few threads
# fine, and the provider call (the slow part) holds no lock.
DEFAULT_CONCURRENCY: Final = 3
# Retry policy for transient classes (network, blocked/rate-limit): the row
# goes back to queued with attempt_count incremented, retried on the next
# sweep after a backoff. Non-transient classes (auth, parse) fail the row
# immediately - retrying would repeat the same deterministic failure. Once
# MAX_ATTEMPTS is reached the row stops in `failed`, awaiting manual/agent
# re-trigger (reset attempt_count or re-queue).
MAX_ATTEMPTS: Final = 3
TRANSIENT_CLASSES: Final[frozenset[str]] = frozenset({"network", "blocked"})
BACKOFF_BASE_SECONDS: Final = 120.0


def _open_gate_if_complete(session: Session, *, item_id: str, input_hash: str) -> bool:
    """Whether the item's whole chain succeeded — and if so, open the gate."""
    from adapters.repository.enrichment import (
        chain_for_source,
        is_source_chain_complete,
    )

    item = session.get(ItemRow, item_id)
    if item is None:
        return False
    if not is_source_chain_complete(
        session,
        item_id=item_id,
        content_hash=input_hash,
        chain=chain_for_source(session, source_id=item.source_id),
    ):
        return False
    if item.publish_status != PublishStatus.PENDING.value:
        return True  # already through; nothing to do
    result = transition(
        PublishStatus.PENDING,
        ItemEvent.MARK_READY,
        TransitionContext(processing_complete=True),
    )
    item.publish_status = result.status.value
    item.updated_at = timestamp(datetime.now(UTC))
    return True


_row_session_factory: Callable[[], Session] | None = None


def _row_session() -> Session:
    """A session for one parallel row-run. Set once by EnrichmentWorker; the
    sweep-queue standalone caller (tests) sets it explicitly."""
    factory = _row_session_factory
    if factory is None:
        raise RuntimeError("row session factory not configured")
    return factory()


def sweep_queue(
    session: Session,
    *,
    processors: dict[str, Any],
    now: datetime | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_attempts: int = MAX_ATTEMPTS,
    row_session_factory: Callable[[], Session] | None = None,
    cancel_event: threading.Event | None = None,
    call_timeout: float | None = None,
) -> dict[str, int]:
    """Process queued enrichments, up to `concurrency` in parallel.

    Claiming is a Core-language conditional UPDATE (no ORM instances of the
    row are retained here): the row sessions own every further write, so
    there is no cross-session identity conflict. The claim commits before
    dispatch. Only immediately available slots are claimed. One absolute
    deadline includes dispatch/startup and execution; a persisted lease adds
    termination/write grace. Older rows without leases use the legacy cutoff.
    """
    global _row_session_factory
    if row_session_factory is not None:
        _row_session_factory = row_session_factory
    if _row_session_factory is None:
        raise RuntimeError(
            "pass row_session_factory (the EnrichmentWorker sets it; tests "
            "pass their sessionmaker)"
        )

    now = now or datetime.now(UTC)
    call_timeout = DEFAULT_CALL_TIMEOUT if call_timeout is None else call_timeout
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    deadline = time.monotonic() + call_timeout
    lease_expires_at = timestamp(
        now + timedelta(seconds=call_timeout + LEASE_GRACE_SECONDS)
    )
    report: dict[str, int] = {
        "ran": 0,
        "succeeded": 0,
        "failed": 0,
        "retried": 0,
        "skipped": 0,
    }

    # Per-host fetch pacing (issue #35), in two stages. The cap is applied
    # here, at claim time, which is single-threaded and race-free: a host
    # whose sweep budget is spent (or that is cooling down) is simply not
    # claimed, so its rows wait for the next sweep. The *interval* cannot be
    # enforced here — it has to hold between the actual fetches, which happen
    # in the pool's threads and then in child processes; it is enforced by
    # `pace()` immediately before each fetch.
    fetch_pacer = pacer()
    fetch_pacer.begin_sweep()

    claimable = _claimable_rows(session, now=now, limit=max(1, concurrency))
    claimed: list[tuple[str, str, str, str]] = []
    for row_id, item_id, plugin_id, item_url in claimable:
        if plugin_id not in processors:
            # Defense in depth: chains naming unregistered processors are
            # refused at config time now, but a processor removed after a
            # chain was configured would land here - visible, not silent.
            LOGGER.error(
                "enrichment %s names unregistered processor %r; the item "
                "cannot be processed until the chain is reconfigured",
                row_id,
                plugin_id,
            )
            report["skipped"] += 1
            continue
        host = host_of(item_url) if plugin_id in PACED_PROCESSORS else ""
        if not fetch_pacer.reserve(host):
            # Deferred, not dropped: the row stays queued and a later sweep
            # picks it up once the cooldown has passed.
            LOGGER.info(
                "deferring enrichment %s: host %s is paced (%d per sweep, "
                "%.0fs interval)",
                row_id,
                host or "-",
                fetch_pacer.max_per_sweep,
                fetch_pacer.min_interval,
            )
            report["skipped"] += 1
            continue
        # Eligibility must still hold when the claim is written, not just
        # when candidates were enumerated.
        owner = secrets.token_hex(16)
        claim = (
            update(ItemEnrichment)
            .where(
                ItemEnrichment.id == row_id,
                or_(
                    and_(
                        ItemEnrichment.status == EnrichmentStatus.QUEUED.value,
                        or_(
                            ItemEnrichment.next_attempt_at.is_(None),
                            ItemEnrichment.next_attempt_at <= timestamp(now),
                        ),
                    ),
                    _expired_lease(now),
                ),
            )
            .values(
                status=EnrichmentStatus.RUNNING.value,
                owner_token=owner,
                lease_expires_at=lease_expires_at,
                updated_at=timestamp(now),
            )
        )
        claimed_flag = cast("CursorResult[Any]", session.execute(claim)).rowcount
        if claimed_flag:
            claimed.append((row_id, item_id, owner, host))
    session.commit()
    if not claimed:
        return report

    concurrency = max(1, min(concurrency, len(claimed)))

    report_lock = threading.Lock()

    def run_one(row_id: str, item_id: str, owner: str, host: str) -> None:
        nonlocal report
        outcome = _execute_row(
            row_id,
            item_id,
            owner_token=owner,
            processors=processors,
            max_attempts=max_attempts,
            cancel_event=cancel_event,
            deadline=deadline,
            host=host,
        )
        with report_lock:
            for key, value in outcome.items():
                report[key] = report.get(key, 0) + value

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        try:
            futures = [
                pool.submit(run_one, row_id, item_id, owner, host)
                for row_id, item_id, owner, host in claimed
            ]
            for future in futures:
                future.result()
        finally:
            cancel_event.set()
    return report


def _expired_lease(now: datetime) -> Any:
    """Legacy NULL leases retain their old crash-recovery cutoff."""
    return and_(
        ItemEnrichment.status == EnrichmentStatus.RUNNING.value,
        or_(
            ItemEnrichment.lease_expires_at <= timestamp(now),
            and_(
                ItemEnrichment.lease_expires_at.is_(None),
                ItemEnrichment.updated_at
                < timestamp(now - timedelta(seconds=DEFAULT_CALL_TIMEOUT * 2)),
            ),
        ),
    )


def _claimable_rows(
    session: Session, *, now: datetime, limit: int
) -> list[tuple[str, str, str, str | None]]:
    """(id, item_id, plugin_id, item_url) of queued rows plus `running` rows
    past the reclaim budget. Core columns only - the outer sweep never holds
    ORM instances of rows that row sessions will write. The item URL rides
    along because fetch pacing is per host, and the host is the item's."""
    rows = session.execute(
        select(
            ItemEnrichment.id,
            ItemEnrichment.item_id,
            ItemEnrichment.plugin_id,
            ItemRow.url,
        )
        .join(ItemRow, ItemRow.id == ItemEnrichment.item_id)
        .where(
            or_(
                and_(
                    ItemEnrichment.status == EnrichmentStatus.QUEUED.value,
                    # Rows requeued after a transient failure wait out their
                    # backoff before being claimed again.
                    or_(
                        ItemEnrichment.next_attempt_at.is_(None),
                        ItemEnrichment.next_attempt_at <= timestamp(now),
                    ),
                ),
                _expired_lease(now),
            )
        )
        .order_by(ItemEnrichment.created_at)
        .limit(limit)
    ).all()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


def _execute_row(
    row_id: str,
    item_id: str,
    *,
    owner_token: str,
    processors: dict[str, Any],
    max_attempts: int,
    cancel_event: threading.Event,
    deadline: float,
    host: str = "",
) -> dict[str, int]:
    """Run one claimed row entirely inside its own session.

    The row and item are re-fetched here: this session owns them from claim
    to commit. Every result write is fenced by the claim's owner token (A10):
    a late completion from a former owner - e.g. after stale-claim recovery
    handed the row to a new sweeper - must not overwrite the current owner's
    work. The fence is the UPDATE's WHERE clause itself, so the check and the
    write land in one statement; the Python-side re-read is only a fast path.
    """
    outcome = {"ran": 1, "succeeded": 0, "failed": 0, "retried": 0, "skipped": 0}
    session = _row_session()
    try:
        row = session.get(ItemEnrichment, row_id)
        item = session.get(ItemRow, item_id)
        if row is None or item is None:
            outcome["skipped"] += 1
            return outcome
        if row.owner_token != owner_token:
            # Our lease was reclaimed while the processor was running. The new
            # owner's attempt is authoritative; ours is discarded unrecorded.
            LOGGER.warning(
                "enrichment %s claim lost to a newer owner; discarding result",
                row.id,
            )
            outcome["skipped"] += 1
            return outcome
        processor = processors[row.plugin_id]
        if row.plugin_id in PACED_PROCESSORS and not pacer().pace(host):
            # Nothing was attempted: this row lost its turn because a sibling
            # row on the same host was refused (cooldown) or the wait budget
            # ran out. Hand it back to the queue rather than burning an
            # attempt, so the next sweep retries it for free.
            fenced = _fenced_update(
                session,
                row_id=row_id,
                owner_token=owner_token,
                values={
                    "status": EnrichmentStatus.QUEUED.value,
                    "owner_token": None,
                    "lease_expires_at": None,
                    "updated_at": timestamp(datetime.now(UTC)),
                },
            )
            outcome["skipped"] += 1
            if fenced:
                LOGGER.info(
                    "enrichment %s deferred: host %s is cooling down or paced",
                    row_id,
                    host or "-",
                )
            session.commit()
            return outcome
        try:
            result = run_processor(
                processor,
                plugin_id=row.plugin_id,
                item_ctx={
                    "item_id": item.id,
                    "title": item.title or "",
                    "content": filestore.read_body(item.content_ref),
                    "url": item.url,
                    "lang": row.lang,
                },
                target_lang=row.lang or "en",
                deadline=deadline,
                cancelled=cancel_event,
            )
        except ProcessorError as exc:
            error_class = exc.error_class
            detail = exc.detail
        except Exception as exc:
            error_class = ErrorClass.NETWORK
            detail = f"{type(exc).__name__}: {exc}"
        else:
            _record_success(
                session,
                row_id=row_id,
                owner_token=owner_token,
                result=filestore.store_body(result),
            )
            if _fence_holds(session, row_id=row_id, owner_token=owner_token):
                _open_gate_if_complete(
                    session, item_id=item.id, input_hash=row.input_hash
                )
                session.commit()
                LOGGER.info("enrichment %s succeeded", row.id)
                outcome["succeeded"] += 1
            else:
                session.rollback()
                LOGGER.warning(
                    "enrichment %s claim lost before result; discarding", row_id
                )
                outcome["skipped"] += 1
            return outcome
        attempts = row.attempt_count + 1
        # A refusal is the origin asking us to stop (403/429): honour it for
        # every row on that host, not just this one. The classification is the
        # taxonomy's own blocked class, so the pacer cannot disagree with what
        # the operator sees in the run history.
        if error_class is ErrorClass.BLOCKED:
            pacer().record_blocked(host)
        transient = error_class.value in TRANSIENT_CLASSES
        if transient and attempts < max_attempts:
            backoff = BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
            fenced = _fenced_update(
                session,
                row_id=row_id,
                owner_token=owner_token,
                values={
                    "status": EnrichmentStatus.QUEUED.value,
                    "attempt_count": attempts,
                    "next_attempt_at": timestamp(
                        datetime.now(UTC) + timedelta(seconds=backoff)
                    ),
                    "error_class": error_class.value,
                    "detail": detail[:500],
                    "updated_at": timestamp(datetime.now(UTC)),
                },
            )
            if fenced:
                outcome["retried"] += 1
                LOGGER.warning(
                    "enrichment %s transient failure (%s), requeue attempt "
                    "%d/%d after %ds: %s",
                    row.id,
                    error_class.value,
                    attempts,
                    max_attempts,
                    int(backoff),
                    detail[:100],
                )
            else:
                outcome["skipped"] += 1
            session.commit()
            return outcome
        fenced = _fenced_update(
            session,
            row_id=row_id,
            owner_token=owner_token,
            values={
                "status": EnrichmentStatus.FAILED.value,
                "error_class": error_class.value,
                "detail": detail[:500],
                "updated_at": timestamp(datetime.now(UTC)),
            },
        )
        if fenced:
            LOGGER.warning(
                "enrichment %s failed (%s)%s: %s",
                row.id,
                error_class.value,
                f" attempt {attempts}/{max_attempts}" if transient else "",
                detail[:120],
            )
            outcome["failed"] += 1
        else:
            outcome["skipped"] += 1
        session.commit()
        return outcome
    finally:
        session.close()


def _fenced_update(
    session: Session, *, row_id: str, owner_token: str, values: dict[str, Any]
) -> bool:
    """One UPDATE that only lands while this owner still holds the row."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(ItemEnrichment)
            .where(
                ItemEnrichment.id == row_id,
                ItemEnrichment.owner_token == owner_token,
            )
            .values(**values)
        ),
    )
    return bool(result.rowcount)


def _record_success(
    session: Session, *, row_id: str, owner_token: str, result: str
) -> None:
    """The fenced success write; the caller checks the fence before opening."""
    session.execute(
        update(ItemEnrichment)
        .where(
            ItemEnrichment.id == row_id,
            ItemEnrichment.owner_token == owner_token,
        )
        .values(
            status=EnrichmentStatus.SUCCEEDED.value,
            payload_ref=result,
            updated_at=timestamp(datetime.now(UTC)),
        )
    )


def _fence_holds(session: Session, *, row_id: str, owner_token: str) -> bool:
    """Whether this owner still owns the row after the success write."""
    return (
        session.scalar(
            select(ItemEnrichment.owner_token).where(ItemEnrichment.id == row_id)
        )
        == owner_token
    )


class EnrichmentWorker:
    """The tick loop, modelled on Scheduler: task in, report callback out."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        processors: dict[str, Any],
        sweep_seconds: float = DEFAULT_SWEEP_SECONDS,
        concurrency: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._processors = processors
        self._sweep_seconds = sweep_seconds
        raw = os.environ.get("PEARSS_ENRICHMENT_CONCURRENCY", "")
        try:
            self._concurrency = (
                max(1, int(raw))
                if raw.strip()
                else (concurrency or DEFAULT_CONCURRENCY)
            )
        except ValueError:
            self._concurrency = concurrency or DEFAULT_CONCURRENCY
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._cancel_event = threading.Event()
        self._inflight: asyncio.Task[dict[str, int]] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # A dead task's reference must not block restart (A07 liveness).
        self._task = None
        self._task = asyncio.create_task(self._loop(), name="pearss-enrichment")
        LOGGER.info(
            "enrichment worker started, sweeping every %.0fs", self._sweep_seconds
        )

    async def stop(self) -> None:
        self._cancel_event.set()
        if self._task is None:
            if self._inflight is not None:
                await asyncio.shield(self._inflight)
            return
        self._task.cancel()
        # A failed task must not prevent shutdown of the remaining resources.
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            LOGGER.error("enrichment task failed (%s)", type(exc).__name__)
        self._task = None
        LOGGER.info("enrichment worker stopped")

    def liveness(self) -> tuple[bool, str]:
        """Whether the background loop is alive, and a name for diagnostics.

        Same contract as Scheduler.liveness: a started task whose coroutine
        died of an escaped error still exists as an object, so `done()` is
        the only honest liveness signal the health endpoint can aggregate.
        """
        if self._task is None:
            return True, ""  # not started (or stopped): not a degradation
        return not self._task.done(), "enrichment"

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_seconds)
            # Same recovery boundary as the scheduler: one transient storage
            # failure must not kill the worker, but a programming error or a
            # cancellation must.
            try:
                await self.sweep_once()
            except OperationalError:
                LOGGER.warning(
                    "enrichment storage operation failed; retrying next sweep"
                )

    async def sweep_once(self) -> dict[str, int]:
        async with self._lock:
            started = time.monotonic()
            self._cancel_event.clear()
            self._inflight = asyncio.create_task(
                asyncio.to_thread(self._sweep, self._session_factory)
            )
            try:
                report = await asyncio.shield(self._inflight)
            except asyncio.CancelledError:
                self._cancel_event.set()
                # Keep the sweep lock and join the actual DB-owning thread;
                # cancelling to_thread alone abandons it, not the processor.
                try:
                    await asyncio.shield(self._inflight)
                except Exception as exc:
                    LOGGER.error("cancelled sweep failed (%s)", type(exc).__name__)
                raise
            finally:
                self._inflight = None
            if report["ran"]:
                LOGGER.info(
                    "enrichment sweep: %d ran (%d ok, %d failed, %d skipped) in %.1fs",
                    report["ran"],
                    report["succeeded"],
                    report["failed"],
                    report["skipped"],
                    time.monotonic() - started,
                )
            return report

    def _sweep(self, session_factory: sessionmaker[Session]) -> dict[str, int]:
        with session_factory() as session:
            return sweep_queue(
                session,
                processors=self._processors,
                concurrency=self._concurrency,
                row_session_factory=session_factory,
                cancel_event=self._cancel_event,
            )


def default_processors() -> dict[str, Any]:
    """The shipped processors, resolved lazily so import stays cheap."""

    import sys

    from plugins.processors.fulltext import processor as fulltext
    from plugins.processors.translate import processor as translate

    return {
        "translate": sys.modules[translate.__name__],
        "fulltext": sys.modules[fulltext.__name__],
    }


def registered_processor_ids() -> frozenset[str]:
    """Ids of the processors this deployment can actually run."""
    return frozenset(default_processors())


__all__ = [
    "EnrichmentWorker",
    "default_processors",
    "registered_processor_ids",
    "sweep_queue",
]
