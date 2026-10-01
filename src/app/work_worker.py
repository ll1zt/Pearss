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
import logging
import time
from collections.abc import Callable
from typing import Final

import httpx
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import (
    append_unit,
    chain_for_source,
    claim_next_work,
    counts_for_work,
    finish_work,
    get_source,
    units_for_work,
)
from app import collector_registry
from app.collector_registry import CollectorRegistry
from app.liveness_watch import LoopWatch, always_alive
from app.pipeline import fetch_one, fetch_pacing, outcome_for
from core.enums import ErrorClass, UnitOutcome
from core.work import WorkRecord, terminal_status

LOGGER = logging.getLogger(__name__)

# How often the loop looks for work. A work is not on a schedule, so this is
# only "how long may a freshly submitted batch wait"; the work itself takes as
# long as its targets take.
DEFAULT_WORK_SWEEP_SECONDS: Final = 30.0

# The kinds this host can execute. Adding one is a deliberate act: the claim
# filters on this set, so an unimplemented kind waits instead of failing.
EXECUTABLE_KINDS: Final[frozenset[str]] = frozenset({"fetch"})


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
        except Exception as exc:  # a failed task must not block shutdown
            LOGGER.error("work task failed (%s)", type(exc).__name__)
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
        with self._session_factory() as session:
            work = claim_next_work(session, kinds=EXECUTABLE_KINDS)
            session.commit()
        if work is None:
            return {"works": 0, "units": 0}
        units = self._run_work(work, registry)
        return {"works": 1, "units": units}

    def _run_work(self, work: WorkRecord, registry: CollectorRegistry) -> int:
        """Execute the work's remaining targets, then close it. Returns the count.

        Re-entrant by design: targets that already have a ledger row are skipped,
        which is what makes a crashed pass resumable rather than a restart.
        """
        written = 0
        with self._session_factory() as session:
            source_id = str(work.params.get("source_id") or "")
            source = get_source(session, source_id) if source_id else None
            if source is None:
                # The work names a source this deployment does not have. Every
                # target is accounted for rather than left queued forever.
                for target in work.targets:
                    append_unit(
                        session,
                        work_id=work.id,
                        target=target,
                        outcome=UnitOutcome.FAILED,
                        error_class=ErrorClass.STRUCTURE_CHANGED,
                        detail=f"no source {source_id!r}",
                    )
                session.commit()
                self._close(session, work.id)
                return len(work.targets)

            registration = registry.select_for(
                source.type, source.url or source.canonical_url or ""
            )
            chain = chain_for_source(session, source_id=source.id)
            done = {row.target for row in units_for_work(session, work.id)}
            batch_size, interval = fetch_pacing(work.params)

            for index, target in enumerate(work.targets):
                if target in done:
                    continue
                if index and index % batch_size == 0:
                    time.sleep(interval)
                unit = fetch_one(
                    session,
                    source=source,
                    registration=registration,
                    client=self._client,
                    url=target,
                    chain=chain,
                )
                append_unit(
                    session,
                    work_id=work.id,
                    target=target,
                    outcome=outcome_for(unit.action),
                    error_class=unit.error_class,
                    detail=unit.detail,
                    ref=unit.item_id,
                )
                session.commit()
                written += 1
                if unit.error_class is ErrorClass.BLOCKED:
                    for rest in work.targets[index + 1 :]:
                        if rest in done:
                            continue
                        append_unit(
                            session,
                            work_id=work.id,
                            target=rest,
                            outcome=UnitOutcome.REJECTED,
                            error_class=ErrorClass.BLOCKED,
                            detail="not attempted: the origin refused this address",
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
