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
import time
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
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
# Generous: an LLM completion regularly takes tens of seconds.
DEFAULT_CALL_TIMEOUT: Final = 150.0


def _mark(
    session: Session,
    row: ItemEnrichment,
    *,
    status: EnrichmentStatus,
    payload_ref: str | None = None,
    error_class: ErrorClass | None = None,
    detail: str | None = None,
) -> None:
    row.status = status.value
    row.payload_ref = payload_ref
    row.error_class = error_class.value if error_class else None
    row.detail = (detail or "")[:500]
    row.updated_at = timestamp(datetime.now(UTC))


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


def sweep_queue(
    session: Session,
    *,
    processors: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, int]:
    """Process every queued enrichment whose processor is registered.

    One row at a time, claiming by status transition (queued -> running in
    this transaction) so two workers cannot double-run the same row. Returns
    counts for the tick report.
    """
    now = now or datetime.now(UTC)
    rows = (
        session.execute(
            select(ItemEnrichment)
            .where(ItemEnrichment.status == EnrichmentStatus.QUEUED.value)
            .order_by(ItemEnrichment.created_at)
            .limit(20)
        )
        .scalars()
        .all()
    )
    report = {"ran": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    for row in rows:
        if row.plugin_id not in processors:
            report["skipped"] += 1
            continue
        item = session.get(ItemRow, row.item_id)
        if item is None:
            _mark(
                session,
                row,
                status=EnrichmentStatus.FAILED,
                error_class=ErrorClass.PARSE,
                detail="item vanished",
            )
            report["failed"] += 1
            continue
        # Claim visible to other workers BEFORE the provider call: the claim
        # commits here, so a second sweep sees `running` and stands down, and
        # the SQLite write lock is not held across a 150s LLM round trip
        # (NNW's read-state writes would hit `database is locked`). A crashed
        # worker leaves the row running; the sweep re-claims rows that have
        # been running past a generous budget.
        if row.status == EnrichmentStatus.QUEUED.value:
            row.status = EnrichmentStatus.RUNNING.value
            row.updated_at = timestamp(now)
            session.commit()
        elif (
            datetime.now(UTC) - _parse(row.updated_at)
        ).total_seconds() < DEFAULT_CALL_TIMEOUT * 2:
            report["skipped"] += 1
            continue

        processor = processors[row.plugin_id]
        try:
            payload_ref = processor.process(
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
        except Exception as exc:  # classified below, never fatal to the sweep
            error_class = getattr(exc, "error_class", None)
            _mark(
                session,
                row,
                status=EnrichmentStatus.FAILED,
                error_class=error_class or ErrorClass.NETWORK,
                detail=str(exc),
            )
            LOGGER.warning(
                "enrichment %s failed (%s): %s",
                row.id,
                error_class.value if error_class else "network",
                str(exc)[:120],
            )
            report["failed"] += 1
        else:
            _mark(
                session,
                row,
                status=EnrichmentStatus.SUCCEEDED,
                payload_ref=payload_ref,
            )
            if _open_gate_if_complete(
                session, item_id=row.item_id, input_hash=row.input_hash
            ):
                LOGGER.info("gate opened for %s (chain complete)", row.item_id)
            report["succeeded"] += 1
        report["ran"] += 1
    session.commit()
    return report


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(UTC)


class EnrichmentWorker:
    """The tick loop, modelled on Scheduler: task in, report callback out."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        processors: dict[str, Any],
        sweep_seconds: float = DEFAULT_SWEEP_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._processors = processors
        self._sweep_seconds = sweep_seconds
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
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        LOGGER.info("enrichment worker stopped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_seconds)
            await self.sweep_once()

    async def sweep_once(self) -> dict[str, int]:
        async with self._lock:
            started = time.monotonic()
            report = await asyncio.to_thread(
                self._sweep, self._session_factory, self._processors
            )
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

    @staticmethod
    def _sweep(
        session_factory: sessionmaker[Session], processors: dict[str, Any]
    ) -> dict[str, int]:
        with session_factory() as session:
            return sweep_queue(session, processors=processors)


def default_processors() -> dict[str, Any]:
    """The shipped processors, resolved lazily so import stays cheap."""

    import sys

    from plugins.processors.translate import processor as translate

    return {"translate": sys.modules[translate.__name__]}


__all__ = ["EnrichmentWorker", "default_processors", "sweep_queue"]
