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
import random
import secrets
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text
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
    last_failure_error_class,
    prune_audit_log,
    prune_work_ledger,
    reap_orphaned_runs,
    update_gaps_for_source,
)

# The fixed-width format has one definition (adapters.timestamps); the
# alias keeps the call sites in this module short.
from adapters.repository import timestamp as _iso
from adapters.repository.search_index import index_missing
from app.collector_registry import CollectorRegistry
from app.liveness_watch import LoopWatch, always_alive
from app.pipeline import CollectionResult, collect_source
from core.alerting import AlertState
from core.enums import ErrorClass, JobRunStatus
from core.job_state import JobRunEvent
from core.models import schedule_policy_from_json
from core.scheduling import ScheduleDecision, decide_schedule
from core.tenancy import SINGLE_TENANT

LOGGER = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """The completion clock for run bookkeeping.

    A module-level seam rather than a bare datetime.now(UTC) call so tests
    can drive run durations deterministically (issue #17): the tick's
    decision clock is injected per call, but the moment a run *ends* is
    whatever the wall says when the work actually finishes -- and that is
    what finished_at must record, or every stored run duration reads as
    zero and a 20-minute fetch looks instantaneous.
    """
    return datetime.now(UTC)


DEFAULT_TICK_SECONDS = 60.0
# How many due sources one tick collects in parallel. Each source runs
# collect_now with its own session and its own active-run claim, so sources
# are independent by construction; the parallelism only shortens the wall
# clock of a pass when several slow origins come due at once.
DEFAULT_TICK_CONCURRENCY = 3
DEFAULT_TICKS = 60.0
PRUNE_INTERVAL_SECONDS = 3600.0
# How often to let SQLite refresh its own statistics (issue #77). `PRAGMA
# optimize` decides what, if anything, is worth analysing from the queries it
# has actually seen, so a pass that finds nothing costs a no-op; an hour is the
# order SQLite's own documentation suggests, and matching the prune interval
# keeps one maintenance cadence rather than two.
OPTIMIZE_INTERVAL_SECONDS = 3600.0
# How often to index items the search index is missing (issue #79). The write
# path indexes an item when its text changes, so this is a catch-up for the
# corpus that predates the index and for a write that failed after its item was
# committed. Minutes rather than hours because the symptom is "search cannot
# find something I can see", and the pass costs one scan.
INDEX_CATCHUP_INTERVAL_SECONDS = 300.0
# How many items one pass will index. Bounded so a first run over a large
# corpus does not hold a transaction open; the next pass takes the rest.
INDEX_CATCHUP_BATCH = 200
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


def work_retention_days() -> int:
    """How long finished ledger rows are kept (ADR 0046 D-F). `0` disables it.

    The same shape as the audit window on purpose: an operator who wants an
    append-only trail sets one variable, and one that is malformed means the
    default rather than "keep nothing" or "keep forever" -- a typo must not
    change what is deleted.
    """
    raw = os.environ.get("PEARSS_WORK_RETENTION_DAYS", "").strip()
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
    # The earliest next-due time among the sources this pass skipped, so an
    # idle tick can say when it will next have something to do (issue #293).
    # None when every considered source was collected (or there were none).
    next_due_at: datetime | None = None

    @property
    def ran(self) -> int:
        return len(self.collected) + len(self.failed)


def evaluate_source(
    session: Session, *, source_id: str, policy_json: str, now: datetime
) -> ScheduleDecision:
    """Derive whether one source is due, from stored facts only."""
    job_id = ensure_collect_job(
        session, source_id=source_id, tenant_id=SINGLE_TENANT, at=_iso(now)
    )
    policy = schedule_policy_from_json(policy_json)
    return decide_schedule(
        policy,
        last_fetch_at=last_collect_at(session, job_id=job_id),
        observed_update_gaps=update_gaps_for_source(session, source_id=source_id),
        consecutive_failures=consecutive_failures(session, job_id=job_id),
        # Issue #5: the retry table keys on the latest failure's class --
        # non-retryable ones (blocked/auth/structure_changed) probe at the
        # normal pace instead of backing off. Jitter spreads same-tick
        # retries; per-decision rng, no persistence needed. S311 is for
        # crypto contexts; schedule jitter is the opposite of a secret.
        error_class=last_failure_error_class(session, job_id=job_id),
        rng=random.Random(),  # noqa: S311 - jitter, not cryptography
        now=now,
    )


def collect_now(
    session: Session,
    *,
    source_id: str,
    policy_json: str,
    client: httpx.Client,
    now: datetime | None = None,
    registry: CollectorRegistry | None = None,
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
        session, source_id=source_id, tenant_id=SINGLE_TENANT, at=_iso(moment)
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
        result = collect_source(
            session,
            source=source,
            client=client,
            tenant_id=SINGLE_TENANT,
            registry=registry,
        )
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
            at=_iso(_utcnow()),
        )
        session.commit()
        return status, CollectionResult(
            succeeded=False, error_class="parse", detail=detail
        )

    # The completion moment, not the tick moment: started_at carries the
    # caller's clock (the tick's, so a run is never stamped later than the
    # decision that produced it), but finished_at is when the work actually
    # ended. Stamping both with the tick moment made every stored run
    # duration zero and reaping-grace reasoning unobservable (issue #17).
    # The escaped-exception path above reads the same seam, so all three
    # endings agree.
    finished = _iso(_utcnow())
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
    registry: CollectorRegistry | None = None,
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
        sources = collectable_sources(session, tenant_id=SINGLE_TENANT)

    collected: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    due_count = 0

    def evaluate_and_collect(
        source_id: str, policy_json: str
    ) -> tuple[str, datetime | None]:
        """One source's due-check + collection. Returns outcome and next due."""
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
                # A source in backoff used to be indistinguishable from a
                # healthy one in the journal: the decision was discarded here,
                # so "waiting because it keeps failing" and "nothing to do yet"
                # rendered identically (issue #4, mode 2). Log only the failing
                # case -- a healthy source that is simply not due yet is the
                # normal, silent state, and logging it would put one line per
                # source per tick into the journal.
                if decision.consecutive_failures > 0:
                    LOGGER.info(
                        "source %s failing: %d consecutive failure(s),"
                        " last class %s, next probe at %s",
                        source_id,
                        decision.consecutive_failures,
                        decision.error_class or "-",
                        decision.next_due_at.isoformat()
                        if decision.next_due_at
                        else "-",
                    )
                return "skipped", decision.next_due_at

            with session_factory() as session:
                status, _ = collect_now(
                    session,
                    source_id=source_id,
                    policy_json=policy_json,
                    client=client,
                    now=moment,
                    registry=registry,
                )
                session.commit()
            return ("collected" if status is JobRunStatus.SUCCEEDED else "failed"), None
        except Exception:
            # One broken source must not end the pass: the remaining sources
            # would silently stop being collected, which looks like a quiet
            # scheduler rather than a failing one.
            LOGGER.exception("collection failed for source %s", source_id)
            return "failed", None

    nearest_next_due: datetime | None = None
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(evaluate_and_collect, source_id, policy_json): source_id
            for source_id, policy_json in sources
        }
        for future in as_completed(futures):
            source_id = futures[future]
            outcome, next_due = future.result()
            if next_due is not None and (
                nearest_next_due is None or next_due < nearest_next_due
            ):
                nearest_next_due = next_due
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
        next_due_at=nearest_next_due,
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
    else:
        # "Idling" and "dead" used to look identical in the journal (issue
        # #293): neither wrote a line. The heartbeat is DEBUG so it is
        # available when someone is diagnosing and costs nothing at the
        # default level -- at 60s/tick a line per tick at INFO would be 1440
        # rows a day of "nothing happened".
        LOGGER.debug(
            "tick: %d considered, 0 due, next due %s",
            report.considered,
            report.next_due_at.isoformat() if report.next_due_at else "-",
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
        optimize_every: float = OPTIMIZE_INTERVAL_SECONDS,
        index_every: float = INDEX_CATCHUP_INTERVAL_SECONDS,
        concurrency: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        # The application's collector registry, threaded into every collection
        # this loop runs. None means the process default, which is what a test
        # constructing a bare Scheduler gets (issue #84).
        self._registry = registry
        self._tick_seconds = tick_seconds
        # Injected so the maintenance schedules can be driven deterministically
        # in tests: reaping orphans and pruning the audit log are both gated on
        # elapsed monotonic time, and waiting an hour of wall clock to observe
        # a decision is not a test. Production passes the default.
        self._monotonic = monotonic
        self._on_tick = on_tick
        self._prune_every = prune_every
        self._optimize_every = optimize_every
        self._index_every = index_every
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
        self._next_work_prune_at = 0.0
        self._next_reap_at = 0.0
        self._next_optimize_at = 0.0
        self._next_index_at = 0.0
        self._task: asyncio.Task[None] | None = None
        # Edge detection for alerts (ADR 0034): in memory by design, since what
        # it tracks is "have we already said this" -- a property of this
        # process's alerting, not a fact worth coupling to the database whose
        # failure is one of the things worth alerting about.
        self._alert_state = AlertState()
        # Watches nothing until `watch` is called: a scheduler that was never
        # handed another loop reports on no loop, rather than on a name it
        # guessed.
        self._watch = LoopWatch(name="scheduler", watch=always_alive)
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
        except Exception:
            LOGGER.exception("scheduler task failed")
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
                await self._check_health_and_alert()
                await self._reap_orphaned_runs_if_due()
                await self._prune_audit_if_due()
                await self._prune_work_ledger_if_due()
                await self._optimize_if_due()
                await self._index_if_due()
            except OperationalError:
                LOGGER.warning("scheduler storage operation failed; retrying next poll")

    def watch(self, liveness: Callable[[], tuple[bool, str]]) -> None:
        """Register another loop whose death this one should report (issue #61).

        Set after construction because the two are built in order, and a loop
        cannot report its own death: whichever loop is still ticking is the one
        that can say the other stopped. `LoopWatch` is shared with the worker's
        direction so the two cannot drift apart in what they say.
        """
        self._watch = LoopWatch(name="scheduler", watch=liveness)

    async def _check_health_and_alert(self) -> None:
        """Speak up when collection needs a person (ADR 0034, issue #4).

        Runs after the tick's collection work, on the scheduler's own loop,
        reading the same `system_snapshot` an operator would fetch by hand --
        one source of truth for the numbers rather than a second computation
        that can disagree with the endpoint.

        Off the event loop (`to_thread`): the snapshot is a few queries and the
        send is an HTTP POST to another host, and neither belongs on the loop
        that also serves requests. Failures here cannot reach the caller: the
        whole body catches, because an alerting problem becoming a collection
        outage is a strictly worse outcome than not being told (ADR 0034
        decision 6).
        """
        try:
            sent = await asyncio.to_thread(self._alert_if_needed)
        except Exception as exc:
            LOGGER.warning(
                "health check failed (%s); retrying next tick", type(exc).__name__
            )
            return
        if sent:
            LOGGER.info("health alert sent")

    def _alert_if_needed(self) -> bool:
        """Read the snapshot, decide, and maybe send. Returns whether it sent.

        Synchronous on purpose so it can be handed to a thread; every decision
        is `core.alerting`'s, and this method only supplies the clock and the
        sender.
        """
        from adapters.alerting import send_alert
        from adapters.repository.inspection import (
            exhausted_enrichment_sources,
            system_snapshot,
        )
        from core.alerting import ALERT_DEFAULTS, alert_body, decide_health

        with self._session_factory() as session:
            snapshot = system_snapshot(session, tenant_id=SINGLE_TENANT)
            exhausted = exhausted_enrichment_sources(session, tenant_id=SINGLE_TENANT)
        # The other loop's liveness travels through its own watch rather than
        # through this verdict: two conditions, two edges, so a stuck chain
        # clearing cannot suppress the recovery notice for a loop that came back.
        self._watch.check()
        verdict = decide_health(
            snapshot,
            success_rate_threshold=float(
                os.environ.get(
                    "PEARSS_ALERT_SUCCESS_RATE_THRESHOLD",
                    str(ALERT_DEFAULTS.success_rate_threshold),
                )
            ),
            min_runs=int(
                os.environ.get("PEARSS_ALERT_MIN_RUNS", str(ALERT_DEFAULTS.min_runs))
            ),
            min_interval_seconds=ALERT_DEFAULTS.min_interval_seconds,
            exhausted_sources=exhausted,
        )
        min_interval = float(
            os.environ.get(
                "PEARSS_ALERT_MIN_INTERVAL_SECONDS",
                str(ALERT_DEFAULTS.min_interval_seconds),
            )
        )
        if not self._alert_state.should_send(
            verdict, now=time.monotonic(), min_interval_seconds=min_interval
        ):
            return False
        return send_alert(
            alert_body(verdict),
            title="Pearss collection degraded"
            if verdict.degraded
            else "Pearss recovered",
            source=f"pearss@{socket.gethostname()}",
            severity="error" if verdict.degraded else "info",
        )

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

    async def _prune_work_ledger_if_due(self) -> None:
        """Enforce the ledger's retention window (ADR 0046 D-F, issue #203).

        Separate from the audit window even though both default to 30 days: they
        are different records with different reasons to be kept, and an operator
        tuning one must not silently move the other. The *interval* is shared
        (`prune_every`), which is what keeps this from being a second schedule.
        """
        now = self._monotonic()
        if now < self._next_work_prune_at:
            return
        days = work_retention_days()
        if days <= 0:
            return

        def prune() -> int:
            with self._session_factory() as session:
                pruned = prune_work_ledger(
                    session, older_than=datetime.now(UTC) - timedelta(days=days)
                )
                session.commit()
            return pruned

        # Same rule as reaping and the audit window: spend the interval only on
        # a committed pass, so a locked database retries on the next poll rather
        # than an interval later.
        pruned = await asyncio.to_thread(prune)
        self._next_work_prune_at = self._monotonic() + self._prune_every
        if pruned:
            LOGGER.info(
                "ledger retention pruned %d work(s) older than %dd", pruned, days
            )

    async def _optimize_if_due(self) -> None:
        """Let SQLite refresh the planner's statistics (issue #77).

        The feed's reads are served by indexes the planner only chooses if it
        knows how selective they are, and those statistics age: a table that
        grows from a thousand rows to a million changes which plan is right,
        while the planner keeps the estimate it computed at the index's
        creation. `PRAGMA optimize` is the supported periodic form -- it runs
        the ANALYZE passes it judges worthwhile from the queries this
        connection has seen, and does nothing when there is nothing to do, so
        the interval is a ceiling rather than a cost.

        Same shape and the same rule as pruning: the interval advances only
        after the pass returns, so a locked database retries on the next poll
        rather than an hour later.
        """
        now = self._monotonic()
        if now < self._next_optimize_at:
            return

        def optimize() -> None:
            with self._session_factory() as session:
                session.execute(text("PRAGMA optimize"))
                session.commit()

        await asyncio.to_thread(optimize)
        self._next_optimize_at = self._monotonic() + self._optimize_every

    async def _index_if_due(self) -> None:
        """Index items the search index does not have yet (issue #79).

        The write path indexes an item when its text changes, so this is not the
        path that keeps the index current -- it is the one that keeps it from
        staying *incomplete*. A corpus that predates migration 0023, or a write
        that failed after its item was committed, is otherwise invisible to
        search until someone remembers `just reindex`.

        Bounded per pass: an unbounded catch-up over a large corpus would hold
        one transaction open for as long as it takes, and the next pass picks up
        where this one stopped.
        """
        now = self._monotonic()
        if now < self._next_index_at:
            return

        def catch_up() -> int:
            with self._session_factory() as session:
                indexed = index_missing(session, limit=INDEX_CATCHUP_BATCH)
                session.commit()
            return indexed

        indexed = await asyncio.to_thread(catch_up)
        self._next_index_at = self._monotonic() + self._index_every
        if indexed:
            LOGGER.info("indexed %d item(s) into the search index", indexed)

    async def tick_once(self) -> TickReport:
        """Run one pass off the event loop, at most one at a time."""
        async with self._lock:
            report = await asyncio.to_thread(
                run_tick,
                self._session_factory,
                client=self._client,
                concurrency=self._concurrency,
                registry=self._registry,
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
