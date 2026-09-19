"""The scheduler.

Answers "which sources should be collected now?" by deriving the answer from
each source's policy and its recorded history, rather than from a stored
next-run time. See docs/adr/0009 for why that choice was made over the
APScheduler-with-jobstore shape named in architecture section 9.

Two structural notes:

- `run_tick` is separated from `run_forever` so the interesting logic is
  callable from a test. A loop that only exists as `while True: sleep()` cannot
  be observed, and an unobservable scheduler is one that gets shipped without
  being checked.
- Collection work is synchronous (SQLAlchemy, lxml, a sync httpx client), so it
  runs through `asyncio.to_thread`. Awaiting it directly would block the event
  loop and stall the API for the duration of every fetch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Source as SourceRow
from adapters.repository import (
    claim_job_run,
    collectable_sources,
    consecutive_failures,
    create_job_run,
    ensure_collect_job,
    finish_job_run,
    last_collect_at,
    prune_audit_log,
    reap_orphaned_runs,
    update_gaps_for_source,
)

# The fixed-width format has one definition (adapters.timestamps); the
# alias keeps the call sites in this module short.
from adapters.repository import timestamp as _iso
from app.pipeline import CollectionResult, collect_source
from core.enums import ErrorClass, JobRunStatus
from core.job_state import JobRunEvent
from core.models import schedule_policy_from_json
from core.scheduling import ScheduleDecision, decide_schedule

LOGGER = logging.getLogger(__name__)

TENANT = "default"
DEFAULT_TICK_SECONDS = 60.0
# How many due sources one tick collects in parallel. Each source runs
# collect_now with its own session and its own active-run claim, so sources
# are independent by construction; the parallelism only shortens the wall
# clock of a pass when several slow origins come due at once.
DEFAULT_TICK_CONCURRENCY = 3
DEFAULT_TICKS = 60.0
PRUNE_INTERVAL_SECONDS = 3600.0
# Per-tick reaping grace. A live run of *this* process must never be reaped:
# timing out a run that is actually still collecting loses its result and
# frees the active-run slot for a duplicate. Routine collection is one page;
# even a full 20-page backfill against the slowest policy finishes far inside
# an hour, so anything running longer than that is genuinely orphaned.
REAP_GRACE_SECONDS = 3600.0


def audit_retention_days() -> int:
    """How long audit rows are kept (ADR 0016). `0` disables pruning."""
    raw = os.environ.get("PEARSS_AUDIT_RETENTION_DAYS", "").strip()
    if not raw:
        return 30
    try:
        return max(0, int(raw))
    except ValueError:
        return 30


@dataclass(frozen=True, slots=True, kw_only=True)
class TickReport:
    """What one scheduler pass did. Returned so a caller, test or log line can
    see the outcome without scraping the log."""

    considered: int
    due: int
    collected: list[str]
    failed: list[str]
    skipped: list[str]

    @property
    def ran(self) -> int:
        return len(self.collected) + len(self.failed)


def evaluate_source(
    session: Session, *, source_id: str, policy_json: str, now: datetime
) -> ScheduleDecision:
    """Derive whether one source is due, from stored facts only."""
    job_id = ensure_collect_job(
        session, source_id=source_id, tenant_id=TENANT, at=_iso(now)
    )
    policy = schedule_policy_from_json(policy_json)
    return decide_schedule(
        policy,
        last_fetch_at=last_collect_at(session, job_id=job_id),
        observed_update_gaps=update_gaps_for_source(session, source_id=source_id),
        consecutive_failures=consecutive_failures(session, job_id=job_id),
        now=now,
    )


def collect_now(
    session: Session,
    *,
    source_id: str,
    policy_json: str,
    client: httpx.Client,
    now: datetime | None = None,
) -> tuple[JobRunStatus, CollectionResult | None]:
    """Run one source's collection, recording the job run around it.

    Mutual exclusion is enforced by the schema: a unique partial index on
    `job_run(job_id)` over the active statuses makes a second concurrent run
    for the same job impossible, so a manual trigger and a scheduler tick (or
    two processes against one database) cannot both collect the same source.
    The loser's insert raises IntegrityError and it stands down with 409
    semantics already in place at the call sites.
    """
    moment = now or datetime.now(UTC)
    job_id = ensure_collect_job(
        session, source_id=source_id, tenant_id=TENANT, at=_iso(moment)
    )
    run_id = f"run-{secrets.token_hex(8)}"
    correlation_id = secrets.token_hex(8)
    try:
        # Insert and claim share one transaction. Claiming inside it means a
        # crash can leave neither a queued row (which would hold the job's
        # only active-run slot forever) nor an unclaimed one to reap.
        create_job_run(
            session,
            run_id=run_id,
            job_id=job_id,
            correlation_id=correlation_id,
            at=_iso(moment),
        )
        claimed = claim_job_run(session, run_id=run_id, at=_iso(moment))
        session.commit()
    except IntegrityError:
        session.rollback()
        LOGGER.info("job %s already has an active run; standing down", job_id)
        return JobRunStatus.CANCELLED, None

    if not claimed:
        # Unreachable while the partial index exists: the row was created and
        # claimed inside one transaction. Kept so a schema without the index
        # degrades to standing down rather than double-collecting.
        LOGGER.info("run %s was claimed elsewhere; standing down", run_id)
        return JobRunStatus.CANCELLED, None

    source = session.get(SourceRow, source_id)
    if source is None:
        # The listing came from the same database, so this is a race with a
        # delete rather than a normal path. It still has to close the run, or
        # the row would sit in `running` forever and block the source.
        finish_job_run(
            session,
            run_id=run_id,
            event=JobRunEvent.FAIL,
            correlation_id=correlation_id,
            error_class=ErrorClass.PARSE,
            at=_iso(moment),
        )
        session.commit()
        return JobRunStatus.FAILED, None

    try:
        result = collect_source(session, source=source, client=client, tenant_id=TENANT)
    except Exception as exc:
        # Failures are normally values, but an exception can still escape the
        # collection path (a plugin child answering unintelligibly, a bug in
        # the pipeline itself). The run must not survive it open: the schema's
        # unique partial index over active runs would then block every future
        # collection of this source until a reaper noticed. An escaped
        # exception is not any one origin's fault, so it classifies as parse
        # and carries the exception type and message for the operator — the
        # traceback itself is in the journal via the logger.
        LOGGER.exception("collection raised for source %s", source_id)
        # A failed result, not a bare status: callers read None as "stood
        # down / another run in progress", which a finished run is not —
        # trigger turned it into a misleading 409 (issue #2).
        detail = f"collection raised: {type(exc).__name__}: {exc}"[:500]
        status = finish_job_run(
            session,
            run_id=run_id,
            event=JobRunEvent.FAIL,
            correlation_id=correlation_id,
            error_class=ErrorClass.PARSE,
            metrics={"detail": detail},
            at=_iso(datetime.now(UTC)),
        )
        session.commit()
        return status, CollectionResult(
            succeeded=False, error_class="parse", detail=detail
        )

    # The caller's clock throughout. Stamping the run with the wall clock while
    # deciding with an injected one makes the two disagree, and a source whose
    # run is stamped "later than now" reads as clock skew and is then treated
    # as due forever.
    finished = _iso(moment)
    if result.succeeded:
        status = finish_job_run(
            session,
            run_id=run_id,
            event=JobRunEvent.SUCCEED,
            correlation_id=correlation_id,
            metrics={
                "drafts": result.drafts,
                "inserted": result.inserted,
                "skipped": result.skipped,
                "not_modified": result.not_modified,
                "skipped_plugin": result.skipped_plugin,
            },
            at=finished,
        )
    else:
        status = finish_job_run(
            session,
            run_id=run_id,
            event=JobRunEvent.FAIL,
            correlation_id=correlation_id,
            error_class=ErrorClass(result.error_class or "network"),
            # Bounded at 500, not 200: the tail of an origin-side detail is
            # where "which call, what did it say" lived, and the old cap cut
            # exactly there (issue #2).
            metrics={"detail": (result.detail or "")[:500]},
            at=finished,
        )
    session.commit()
    return status, result


def run_tick(
    session_factory: sessionmaker[Session],
    *,
    client: httpx.Client,
    now: datetime | None = None,
    concurrency: int | None = None,
) -> TickReport:
    """One scheduler pass: evaluate every active source, collect those due.

    Due sources are collected in parallel (bounded by `concurrency`, from
    PEARSS_TICK_CONCURRENCY, default 3): each runs `collect_now` with its
    own session, and the schema's unique partial index over active runs is
    what makes a source internally exclusive - sources are independent by
    construction, so parallelism here only shortens the wall clock of a
    pass. Requires WAL mode (set at engine creation) so the parallel row
    writes from collection do not block readers.
    """
    moment = now or datetime.now(UTC)
    raw = str(os.environ.get("PEARSS_TICK_CONCURRENCY", "")).strip()
    if concurrency is None:
        try:
            concurrency = max(1, int(raw)) if raw else DEFAULT_TICK_CONCURRENCY
        except ValueError:
            concurrency = DEFAULT_TICK_CONCURRENCY
    concurrency = max(1, concurrency)

    with session_factory() as session:
        sources = collectable_sources(session, tenant_id=TENANT)

    collected: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    due_count = 0

    def evaluate_and_collect(source_id: str, policy_json: str) -> str | None:
        """One source's due-check + collection. Returns outcome category."""
        try:
            with session_factory() as session:
                decision = evaluate_source(
                    session,
                    source_id=source_id,
                    policy_json=policy_json,
                    now=moment,
                )
                session.commit()

            if not decision.due:
                return "skipped"

            with session_factory() as session:
                status, _ = collect_now(
                    session,
                    source_id=source_id,
                    policy_json=policy_json,
                    client=client,
                    now=moment,
                )
                session.commit()
            return "collected" if status is JobRunStatus.SUCCEEDED else "failed"
        except Exception:
            # One broken source must not end the pass: the remaining sources
            # would silently stop being collected, which looks like a quiet
            # scheduler rather than a failing one.
            LOGGER.exception("collection failed for source %s", source_id)
            return "failed"

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(evaluate_and_collect, source_id, policy_json): source_id
            for source_id, policy_json in sources
        }
        for future in as_completed(futures):
            source_id = futures[future]
            outcome = future.result()
            if outcome == "collected":
                due_count += 1
                collected.append(source_id)
            elif outcome == "failed":
                due_count += 1
                failed.append(source_id)
            else:
                skipped.append(source_id)

    report = TickReport(
        considered=len(sources),
        due=due_count,
        collected=collected,
        failed=failed,
        skipped=skipped,
    )
    if report.ran:
        LOGGER.info(
            "tick: %d considered, %d ran (%d ok, %d failed), %d not due",
            report.considered,
            report.ran,
            len(report.collected),
            len(report.failed),
            len(report.skipped),
        )
    return report


class Scheduler:
    """The tick loop, as a task the application can start and stop."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        client: httpx.Client,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        on_tick: Callable[[TickReport], None] | None = None,
        prune_every: float = PRUNE_INTERVAL_SECONDS,
        concurrency: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._tick_seconds = tick_seconds
        # Injected so the maintenance schedules can be driven deterministically
        # in tests: reaping orphans and pruning the audit log are both gated on
        # elapsed monotonic time, and waiting an hour of wall clock to observe
        # a decision is not a test. Production passes the default.
        self._monotonic = monotonic
        self._on_tick = on_tick
        self._prune_every = prune_every
        raw = os.environ.get("PEARSS_TICK_CONCURRENCY", "").strip()
        try:
            self._concurrency = (
                max(1, int(raw)) if raw else (concurrency or DEFAULT_TICK_CONCURRENCY)
            )
        except ValueError:
            self._concurrency = concurrency or DEFAULT_TICK_CONCURRENCY
        # Zero means the first loop pass prunes immediately: booting after a
        # long outage is exactly when stale rows should go. The same holds for
        # reaping orphaned runs - see _reap_orphaned_runs_if_due.
        self._next_prune_at = 0.0
        self._next_reap_at = 0.0
        self._task: asyncio.Task[None] | None = None
        # Guards against a slow tick overlapping the next one. Without it a
        # fetch longer than the interval would stack up runs indefinitely.
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # A task that already died of an escaped error must not block restart:
        # drop the dead reference and start fresh (A07 liveness recovery).
        self._task = None

        # Reap first: a run left `running` by a crashed process is never closed
        # by anyone, so the history would keep showing work in progress that
        # stopped existing hours ago. At startup no run of *this* process can
        # exist yet, so the tight window (one tick plus slack) is safe here;
        # the per-tick pass uses the wide grace instead, because there a live
        # run of this very process is exactly what must not be reaped.
        def reap() -> int:
            with self._session_factory() as session:
                reaped = reap_orphaned_runs(
                    session,
                    older_than=datetime.now(UTC)
                    - timedelta(seconds=self._tick_seconds * 4),
                )
                session.commit()
            return reaped

        reaped = await asyncio.to_thread(reap)
        if reaped:
            LOGGER.warning(
                "timed out %d run(s) left running by a previous process", reaped
            )
        self._task = asyncio.create_task(self._loop(), name="pearss-scheduler")
        LOGGER.info("scheduler started, ticking every %.0fs", self._tick_seconds)

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
            LOGGER.error("scheduler task failed (%s)", type(exc).__name__)
        self._task = None
        LOGGER.info("scheduler stopped")

    def liveness(self) -> tuple[bool, str]:
        """Whether the background loop is alive, and a name for diagnostics.

        A started task whose coroutine has died of an escaped error still
        exists as an object; `done()` distinguishes the two. This is the
        signal operational health exposes: a 200 from the HTTP process means
        nothing if collection quietly stopped.
        """
        if self._task is None:
            return True, ""  # not started (or stopped): not a degradation
        return not self._task.done(), "scheduler"

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_seconds)
            try:
                await self.tick_once()
                await self._reap_orphaned_runs_if_due()
                await self._prune_audit_if_due()
            except OperationalError:
                LOGGER.warning("scheduler storage operation failed; retrying next poll")

    async def _reap_orphaned_runs_if_due(self) -> None:
        """Close runs left `running`, on every tick (with a wide grace).

        Startup-only reaping (the old behaviour) meant a run abandoned
        *inside* this process - an escaped exception that slipped every
        guard - held the source's only active-run slot until restart. The
        per-tick pass bounds that to REAP_GRACE_SECONDS: long enough that a
        live run of this process can never be reaped, short enough that a
        leaked slot costs the source its next few cycles, not its whole
        week. Startup reaping keeps the tight window - see start().
        """
        now = self._monotonic()
        if now < self._next_reap_at:
            return

        def reap() -> int:
            with self._session_factory() as session:
                reaped = reap_orphaned_runs(
                    session,
                    older_than=datetime.now(UTC)
                    - timedelta(seconds=REAP_GRACE_SECONDS),
                )
                session.commit()
            return reaped

        # Only a committed pass spends the window: a storage failure here must
        # not also consume the retry slot, or one transient lock (backup tool,
        # overloaded disk) would delay orphan reaping by a whole window even
        # though nothing was reaped. Retries land on the next poll instead.
        reaped = await asyncio.to_thread(reap)
        self._next_reap_at = self._monotonic() + self._tick_seconds * 4
        if reaped:
            LOGGER.warning(
                "timed out %d run(s) left running past the %ds grace window",
                reaped,
                REAP_GRACE_SECONDS,
            )

    async def _prune_audit_if_due(self) -> None:
        now = self._monotonic()
        if now < self._next_prune_at:
            return
        days = audit_retention_days()
        if days <= 0:
            return

        def prune() -> int:
            with self._session_factory() as session:
                pruned = prune_audit_log(
                    session,
                    older_than=datetime.now(UTC) - timedelta(days=days),
                )
                session.commit()
            return pruned

        # Same rule as reaping: spend the window only on a committed pass, so
        # a locked database retries on the next poll rather than an hour later.
        pruned = await asyncio.to_thread(prune)
        self._next_prune_at = self._monotonic() + self._prune_every
        if pruned:
            LOGGER.info("audit retention pruned %d row(s) older than %dd", pruned, days)

    async def tick_once(self) -> TickReport:
        """Run one pass off the event loop, at most one at a time."""
        async with self._lock:
            report = await asyncio.to_thread(
                run_tick,
                self._session_factory,
                client=self._client,
                concurrency=self._concurrency,
            )
        if self._on_tick is not None:
            self._on_tick(report)
        return report


def scheduler_enabled() -> bool:
    """Whether the scheduler should run.

    Defaults to on. Disabled by an explicit environment flag so tests and
    one-off commands do not start a background loop, and so a deployment can
    run the API without the scheduler while keeping one config file.
    """
    raw = os.environ.get("PEARSS_SCHEDULER_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}
