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
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import CursorResult, and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from adapters import filestore
from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment
from adapters.repository import timestamp
from core.enrichment import EnrichmentStatus
from core.enums import ErrorClass, ItemEvent, PublishStatus
from core.item_state import TransitionContext, transition

LOGGER = logging.getLogger(__name__)

TENANT: Final = "default"
# Poll cadence: queued rows appear at collection time, so a minute of lag is
# invisible next to the fetch interval that produced them.
DEFAULT_SWEEP_SECONDS: Final = 60.0
# Generous: an LLM completion regularly takes minutes through a relayed
# endpoint (observed 126s for one gemini-flash reply). Matches the
# processor's read budget; the reclaim window is twice this.
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
) -> dict[str, int]:
    """Process queued enrichments, up to `concurrency` in parallel.

    Claiming is a Core-language conditional UPDATE (no ORM instances of the
    row are retained here): the row sessions own every further write, so
    there is no cross-session identity conflict. The claim commits before
    dispatch - the provider call must not hold the write lock, and a second
    sweep seeing `running` stands down. A `running` row past twice the call
    budget is reclaimed (crashed worker).
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
    report: dict[str, int] = {
        "ran": 0,
        "succeeded": 0,
        "failed": 0,
        "retried": 0,
        "skipped": 0,
    }

    claimable = _claimable_rows(session, now=now, limit=concurrency * 2)
    claimed: list[tuple[str, str]] = []
    for row_id, item_id, plugin_id in claimable:
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
        claim = (
            update(ItemEnrichment)
            .where(
                ItemEnrichment.id == row_id,
                ItemEnrichment.status.in_(
                    [EnrichmentStatus.QUEUED.value, EnrichmentStatus.RUNNING.value]
                ),
            )
            .values(status=EnrichmentStatus.RUNNING.value, updated_at=timestamp(now))
        )
        claimed_flag = cast("CursorResult[Any]", session.execute(claim)).rowcount
        if claimed_flag:
            claimed.append((row_id, item_id))
    session.commit()

    if not claimed:
        return report

    concurrency = max(1, min(concurrency, len(claimed)))

    report_lock = threading.Lock()

    def run_one(row_id: str, item_id: str) -> None:
        nonlocal report
        outcome = _execute_row(
            row_id,
            item_id,
            processors=processors,
            max_attempts=max_attempts,
        )
        with report_lock:
            for key, value in outcome.items():
                report[key] = report.get(key, 0) + value

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(run_one, row_id, item_id) for row_id, item_id in claimed]
        for future in futures:
            future.result()
    return report


def _claimable_rows(
    session: Session, *, now: datetime, limit: int
) -> list[tuple[str, str, str]]:
    """(id, item_id, plugin_id) of queued rows plus `running` rows past the
    reclaim budget. Core columns only - the outer sweep never holds ORM
    instances of rows that row sessions will write."""
    stale_before = timestamp(now - timedelta(seconds=DEFAULT_CALL_TIMEOUT * 2))
    rows = session.execute(
        select(
            ItemEnrichment.id,
            ItemEnrichment.item_id,
            ItemEnrichment.plugin_id,
        )
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
                and_(
                    ItemEnrichment.status == EnrichmentStatus.RUNNING.value,
                    ItemEnrichment.updated_at < stale_before,
                ),
            )
        )
        .order_by(ItemEnrichment.created_at)
        .limit(limit)
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


def _execute_row(
    row_id: str,
    item_id: str,
    *,
    processors: dict[str, Any],
    max_attempts: int,
) -> dict[str, int]:
    """Run one claimed row entirely inside its own session.

    The row and item are re-fetched here: this session owns them from claim
    to commit, and no other session touches them meanwhile.
    """
    outcome = {"ran": 1, "succeeded": 0, "failed": 0, "retried": 0, "skipped": 0}
    session = _row_session()
    try:
        row = session.get(ItemEnrichment, row_id)
        item = session.get(ItemRow, item_id)
        if row is None or item is None:
            outcome["skipped"] += 1
            return outcome
        processor = processors[row.plugin_id]
        try:
            result = processor.process(
                {
                    "item_id": item.id,
                    "title": item.title or "",
                    "content": filestore.read_body(item.content_ref),
                    "url": item.url,
                    "lang": row.lang,
                },
                target_lang=row.lang or "en",
                store=filestore.store_body,
            )
        except Exception as exc:
            error_class = getattr(exc, "error_class", None) or ErrorClass.NETWORK
            attempts = row.attempt_count + 1
            transient = error_class.value in TRANSIENT_CLASSES
            if transient and attempts < max_attempts:
                backoff = BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
                row.status = EnrichmentStatus.QUEUED.value
                row.attempt_count = attempts
                row.next_attempt_at = timestamp(
                    datetime.now(UTC) + timedelta(seconds=backoff)
                )
                row.error_class = error_class.value
                row.detail = str(exc)[:500]
                row.updated_at = timestamp(datetime.now(UTC))
                outcome["retried"] += 1
                LOGGER.warning(
                    "enrichment %s transient failure (%s), requeue attempt %d/%d "
                    "after %ds: %s",
                    row.id,
                    error_class.value,
                    attempts,
                    max_attempts,
                    int(backoff),
                    str(exc)[:100],
                )
            else:
                row.status = EnrichmentStatus.FAILED.value
                row.error_class = error_class.value
                row.detail = str(exc)[:500]
                row.updated_at = timestamp(datetime.now(UTC))
                LOGGER.warning(
                    "enrichment %s failed (%s)%s: %s",
                    row.id,
                    error_class.value,
                    f" attempt {attempts}/{max_attempts}" if transient else "",
                    str(exc)[:120],
                )
                outcome["failed"] += 1
            session.commit()
            return outcome

        row.status = EnrichmentStatus.SUCCEEDED.value
        row.payload_ref = result if isinstance(result, str) else None
        row.updated_at = timestamp(datetime.now(UTC))
        _open_gate_if_complete(session, item_id=item.id, input_hash=row.input_hash)
        session.commit()
        LOGGER.info("enrichment %s succeeded", row.id)
        outcome["succeeded"] += 1
        return outcome
    finally:
        session.close()


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

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="pearss-enrichment")
        LOGGER.info(
            "enrichment worker started, sweeping every %.0fs", self._sweep_seconds
        )

    async def stop(self) -> None:
        if self._task is None:
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

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_seconds)
            await self.sweep_once()

    async def sweep_once(self) -> dict[str, int]:
        async with self._lock:
            started = time.monotonic()
            report = await asyncio.to_thread(self._sweep, self._session_factory)
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
            )


def default_processors() -> dict[str, Any]:
    """The shipped processors, resolved lazily so import stays cheap."""

    import sys

    from plugins.processors.translate import processor as translate

    return {"translate": sys.modules[translate.__name__]}


def registered_processor_ids() -> frozenset[str]:
    """Ids of the processors this deployment can actually run."""
    return frozenset(default_processors())


__all__ = [
    "EnrichmentWorker",
    "default_processors",
    "registered_processor_ids",
    "sweep_queue",
]
