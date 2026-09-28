"""Deletion: removing items and sources for real (ADR 0021).

Deletion here is physical. Rows go, and so does the content-addressed body
each row pointed at. Two things make that more delicate than a DELETE
statement, and both are handled in this module rather than at the call sites,
because every caller needs them and none of them should be able to forget.

**Bodies are shared.** `filestore.store_body` addresses a body by the sha256 of
its bytes, so two items with identical content -- a syndicated article, or the
same post collected twice under different sources -- resolve to one file. The
file may only be unlinked once no surviving row references it. Deleting it per
item would blank a live article silently: `read_body` returns "" for a missing
file and logs one warning, so the user sees an empty page and the log barely
notices. Every unlink is therefore conditional on a count taken after the rows
are gone.

**Products cascade, files do not.** `read_state` and `item_enrichment` rows
follow their item through `ON DELETE CASCADE`. The enrichment's body file has
no such mechanism, so its reference is collected here and tested the same way.

Files are deleted after the rows, and a failure to unlink is logged rather than
raised. The reverse order would leave live rows pointing at missing bodies,
which is the one outcome worth avoiding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from adapters import filestore
from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment as EnrichmentRow
from adapters.db_models import RawFetch as RawFetchRow
from adapters.db_models import ReadState as ReadStateRow
from adapters.db_models import Source as SourceRow
from adapters.repository._shared import row_to_item
from adapters.repository.search_index import deindex_items
from core.models import Item

LOGGER = logging.getLogger(__name__)

_EMPTY_COUNTS: dict[str, int] = {
    "items": 0,
    "read_states": 0,
    "enrichments": 0,
    "bodies": 0,
}


@dataclass(frozen=True, slots=True)
class ReclaimPlan:
    """Files to delete once -- and only once -- the transaction has committed.

    Deletion has two halves with different atomicity. The rows are removed by
    the transaction: roll it back and they come back. The files are not -- an
    unlink cannot be rolled back. So the unlink has to happen *after* the
    commit, or a rolled-back deletion restores rows whose bodies are already
    gone, and `read_body` answers "" for them forever without an error (the
    silent blanking this module exists to avoid).

    The refs are therefore collected inside the transaction, where the
    reference counts are accurate, and handed back for the caller to act on
    after committing. `reclaim()` is safe to call late and safe to call twice:
    removing a file that is already gone is a no-op.
    """

    refs: tuple[str, ...] = ()
    root: Path | None = None

    def reclaim(self) -> int:
        """Delete the planned files. Returns how many were actually removed.

        Deliberately takes no session: by the time this runs the transaction
        is committed, and a reference count read now would be answering a
        question about a different moment.
        """
        removed = 0
        for ref in self.refs:
            removed += int(filestore.remove_body(ref, root=self.root))
        return removed

    def __bool__(self) -> bool:
        return bool(self.refs)


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """What a deletion removed, and the files to reclaim once committed.

    Bundling the two keeps the caller from being able to commit the rows and
    forget the files, which would leak bytes silently -- the mirror image of
    the bug the plan exists to prevent.
    """

    counts: dict[str, int] = field(default_factory=lambda: dict(_EMPTY_COUNTS))
    reclaim: ReclaimPlan = field(default_factory=ReclaimPlan)


def delete_items(
    session: Session, item_ids: list[str], *, root: Path | None = None
) -> DeletionResult:
    """Delete items and everything that only existed because of them.

    Returns counts for the caller to report, plus a `reclaim` plan naming the
    files to remove *after* the caller commits. Nothing on disk is touched
    here: see ReclaimPlan for why.

    An id that names no row is not an error: it counts zero. Two callers can
    race the same id, and a client retrying a request whose response was lost
    must see the same success the first attempt produced -- which is also why
    the endpoint is idempotent.

    Enrichment payloads are collected before the cascade removes their rows,
    because after the delete there is nothing left to look them up by.
    """
    if not item_ids:
        return DeletionResult(counts=_EMPTY_COUNTS, reclaim=ReclaimPlan(root=root))

    rows = list(
        session.execute(select(ItemRow).where(ItemRow.id.in_(item_ids))).scalars()
    )
    if not rows:
        return DeletionResult(counts=_EMPTY_COUNTS, reclaim=ReclaimPlan(root=root))

    found_ids = [row.id for row in rows]
    # Both sets are typed `str`: the columns are nullable, but a NULL reference
    # names no file, so it is filtered rather than carried as an optional
    # through every later step.
    body_refs: set[str] = {row.content_ref for row in rows if row.content_ref}
    product_refs: set[str] = {
        ref
        for ref in session.execute(
            select(EnrichmentRow.payload_ref).where(
                EnrichmentRow.item_id.in_(found_ids),
                EnrichmentRow.payload_ref.is_not(None),
            )
        ).scalars()
        if ref
    }

    # The cascade takes read_state and item_enrichment rows with these. Counted
    # first so the response can say what it removed rather than only what was
    # asked for.
    read_states = session.execute(
        select(func.count())
        .select_from(ReadStateRow)
        .where(ReadStateRow.item_id.in_(found_ids))
    ).scalar_one()
    enrichments = session.execute(
        select(func.count())
        .select_from(EnrichmentRow)
        .where(EnrichmentRow.item_id.in_(found_ids))
    ).scalar_one()

    session.execute(delete(ItemRow).where(ItemRow.id.in_(found_ids)))
    # `item_fts` is a virtual table, so the schema's ON DELETE CASCADE does not
    # reach it and the rows have to go explicitly. This is the one place that
    # needs it: both source-level deletions funnel through here, so one call
    # covers delete_item, delete_source_items and remove_source.
    deindex_items(session, found_ids)
    # Flush before counting references: the rows must be gone from the
    # transaction's view of the table, or every body still looks referenced by
    # the rows being deleted and nothing is ever reclaimed.
    session.flush()

    # Which of the candidates no longer have a live referent. Checked here,
    # inside the transaction, because this is the moment the answer is true --
    # a shared body must survive, and only the count can tell us that.
    reclaimable = [
        ref
        for ref in body_refs | product_refs
        if _still_referenced(session, ref) is False
    ]

    return DeletionResult(
        counts={
            "items": len(found_ids),
            "read_states": int(read_states),
            "enrichments": int(enrichments),
            "bodies": len(reclaimable),
        },
        reclaim=ReclaimPlan(refs=tuple(sorted(reclaimable)), root=root),
    )


def _still_referenced(session: Session, ref: str) -> bool:
    """Whether any surviving item row points at this body.

    Split out from the unlink so the count can be taken during the transaction
    while the removal happens after it.
    """
    remaining = session.execute(
        select(func.count()).select_from(ItemRow).where(ItemRow.content_ref == ref)
    ).scalar_one()
    return bool(remaining)


def items_for_source(
    session: Session,
    source_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    include_published: bool = True,
) -> list[Item]:
    """The items a per-source deletion would remove, without removing them.

    Separate from the delete so a caller can count and name what is about to go
    before it goes.

    `include_published` defaults true: deleting a whole source's contents is
    already an explicit, confirmed act, and excluding published items by
    default would leave the caller with a source that looks emptied while its
    articles are still in every client's cache.

    A time window only selects items that have a publication date. An item with
    none belongs to no window, so it is excluded from both bounds -- comparing
    the column directly rather than COALESCE-ing it to "" is what makes that
    true. Coalescing looked harmless for `since` (the empty string sorts before
    every timestamp, so those rows fell out anyway) but silently *included*
    every undated item for `until`, since "" is <= any bound. A caller asking
    to trim everything older than a date would have deleted the undated rows
    too, which is the opposite of what they asked for.
    """
    query = select(ItemRow).where(ItemRow.source_id == source_id)
    if not include_published:
        query = query.where(ItemRow.publish_status != "published")
    if since:
        query = query.where(
            ItemRow.published_at.is_not(None), ItemRow.published_at >= since
        )
    if until:
        query = query.where(
            ItemRow.published_at.is_not(None), ItemRow.published_at <= until
        )
    rows = session.execute(query.order_by(ItemRow.id)).scalars()
    return [row_to_item(row) for row in rows]


def delete_source_items(
    session: Session,
    source_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    include_published: bool = True,
    root: Path | None = None,
) -> DeletionResult:
    """Delete a source's items while keeping the source itself.

    This is the "clear out a bad crawl but keep collecting" case. The source
    keeps its `seen_frontier` and `floor_cursor`, so the subscription resumes
    where it was rather than re-sampling history from the first page.
    """
    targets = items_for_source(
        session,
        source_id,
        since=since,
        until=until,
        include_published=include_published,
    )
    return delete_items(session, [item.id for item in targets], root=root)


def delete_source(
    session: Session, source_id: str, *, root: Path | None = None
) -> DeletionResult:
    """Delete a source and its contents (ADR 0021).

    Items go first, then the fetch records, then the source row. The order is
    the one the schema allows: both reference the source, so removing it first
    would leave the children pointing at nothing for as long as the transaction
    takes to commit.

    The source's `raw_fetch` rows go with it. They are diagnostic records whose
    subject no longer exists, so nothing can read them as anything.
    """
    row = session.get(SourceRow, source_id)
    if row is None:
        return DeletionResult(
            counts={"sources": 0, "items": 0, "raw_fetches": 0, "bodies": 0},
            reclaim=ReclaimPlan(root=root),
        )

    inner = delete_items(
        session,
        [
            item.id
            for item in items_for_source(session, source_id, include_published=True)
        ],
        root=root,
    )

    raw_fetches = session.execute(
        select(func.count())
        .select_from(RawFetchRow)
        .where(RawFetchRow.source_id == source_id)
    ).scalar_one()
    # No payload refs are collected here. `RawFetch.payload_ref` is never
    # written -- pipeline._raw_fetch_row stores only content_hash, because a
    # raw feed body is rebuildable from the origin and storing it twice is
    # waste -- so a loop over it would be dead code guarding a column that is
    # always NULL. If that ever changes, the refs must be unlinked without an
    # item-reference check: a fetch payload is not an item body, so no item row
    # accounts for it and `_still_referenced` would answer a question about the
    # wrong table.
    session.execute(delete(RawFetchRow).where(RawFetchRow.source_id == source_id))
    session.flush()

    session.execute(delete(SourceRow).where(SourceRow.id == source_id))

    return DeletionResult(
        counts={
            "sources": 1,
            "items": inner.counts["items"],
            "raw_fetches": int(raw_fetches),
            "bodies": inner.counts["bodies"],
        },
        # The item bodies are the only files involved, so the plan is the one
        # the inner call built. Threaded through rather than rebuilt, so the
        # two cannot disagree about what is reclaimable.
        reclaim=inner.reclaim,
    )
