"""Item, RawFetch and job persistence, split by aggregate.

The repository applies the rules that core decides: core is pure and holds
no database, so it returns decisions and these modules execute them. Each
module owns one aggregate (items, reader state, sources, plugins,
idempotency, jobs, scheduling inputs, inspection); the package __init__
re-exports every public name so callers keep importing from
`adapters.repository` unchanged.

Shared row conversion lives in `_shared`; `timestamp` is the fixed-width
formatter whose single definition is `adapters.timestamps`.
"""

from __future__ import annotations

from adapters.repository._shared import (
    EPOCH_FLOOR,
    StoreOutcome,
    dumps,
    row_to_item,
    timestamp,
)
from adapters.repository.deletion import (
    DeletionResult,
    ReclaimPlan,
    delete_items,
    delete_source,
    delete_source_items,
)
from adapters.repository.enrichment import (
    backfill_chain,
    backlog_for_source,
    chain_for_source,
    enqueue_chain,
    failed_enrichments_for_source,
    parse_chain,
    statuses_for_item,
)
from adapters.repository.feed import (
    FEED_WINDOW,
    items_ready_for_feed,
    mark_published,
    promote_to_ready,
)
from adapters.repository.idempotency import (
    find_idempotent,
    last_conditional_state,
    store_idempotent,
)
from adapters.repository.inspection import (
    count_items,
    list_job_runs,
    list_jobs,
    system_snapshot,
)
from adapters.repository.items import (
    existing_items_for_source,
    find_by_content_hash,
    find_by_identity,
    store_item,
)
from adapters.repository.items_query import (
    ItemFilter,
    ItemPage,
    count_matching,
    escape_like,
    get_item,
    query_items,
    update_item_content_ref,
)
from adapters.repository.jobs import (
    COLLECT_JOB_TYPE,
    claim_job_run,
    create_job_run,
    ensure_collect_job,
    finish_job_run,
    job_for_source,
)
from adapters.repository.plugins import (
    list_plugin_rows,
    plugin_status,
    set_plugin_status,
    upsert_plugin,
)
from adapters.repository.rawfetch import record_raw_fetch
from adapters.repository.reader import (
    mark_all_read,
    read_states_for,
    reader_credential,
    resolve_reader_ids,
    upsert_read_state,
    upsert_reader_credential,
    visible_ids_for_stream,
)
from adapters.repository.scheduling import (
    PagingState,
    collectable_sources,
    consecutive_failures,
    last_collect_at,
    last_failure_error_class,
    paging_state,
    prune_audit_log,
    reap_orphaned_runs,
    save_paging_state,
    set_source_title_if_absent,
    source_display_labels,
    update_gaps_for_source,
)
from adapters.repository.sources import (
    create_source,
    derive_source_id,
    get_source,
    list_sources,
)
from adapters.repository.work import (
    append_unit,
    claim_work,
    counts_for_work,
    create_work,
    finish_work,
    units_for_work,
    work_by_id,
)

__all__ = [
    "COLLECT_JOB_TYPE",
    "EPOCH_FLOOR",
    "FEED_WINDOW",
    "DeletionResult",
    "ItemFilter",
    "ItemPage",
    "PagingState",
    "ReclaimPlan",
    "StoreOutcome",
    "append_unit",
    "backfill_chain",
    "backlog_for_source",
    "chain_for_source",
    "claim_job_run",
    "claim_work",
    "collectable_sources",
    "consecutive_failures",
    "count_items",
    "count_matching",
    "counts_for_work",
    "create_job_run",
    "create_source",
    "create_work",
    "delete_items",
    "delete_source",
    "delete_source_items",
    "derive_source_id",
    "dumps",
    "enqueue_chain",
    "ensure_collect_job",
    "escape_like",
    "existing_items_for_source",
    "failed_enrichments_for_source",
    "find_by_content_hash",
    "find_by_identity",
    "find_idempotent",
    "finish_job_run",
    "finish_work",
    "get_item",
    "get_source",
    "items_ready_for_feed",
    "job_for_source",
    "last_collect_at",
    "last_conditional_state",
    "last_failure_error_class",
    "list_job_runs",
    "list_jobs",
    "list_plugin_rows",
    "list_sources",
    "mark_all_read",
    "mark_published",
    "paging_state",
    "parse_chain",
    "plugin_status",
    "promote_to_ready",
    "prune_audit_log",
    "query_items",
    "read_states_for",
    "reader_credential",
    "reap_orphaned_runs",
    "record_raw_fetch",
    "resolve_reader_ids",
    "row_to_item",
    "save_paging_state",
    "set_plugin_status",
    "set_source_title_if_absent",
    "source_display_labels",
    "statuses_for_item",
    "store_idempotent",
    "store_item",
    "system_snapshot",
    "timestamp",
    "units_for_work",
    "update_gaps_for_source",
    "update_item_content_ref",
    "upsert_plugin",
    "upsert_read_state",
    "upsert_reader_credential",
    "visible_ids_for_stream",
    "work_by_id",
]
