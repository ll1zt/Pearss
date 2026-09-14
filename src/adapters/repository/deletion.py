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
from core.models import Item

LOGGER = logging.getLogger(__name__)


def _unlink_if_unreferenced(
    session: Session, ref: str | None, *, root: Path | None = None
) -> bool:
    """Remove the file `ref` names if no item row still points at it.

    The count is taken from the database as it stands now, so this must be
    called *after* the rows that referenced the body have been deleted. A ref
    that is still referenced is left exactly as it is: sharing is the common
    case for syndicated content, not an edge case.

    Returns whether a file was removed.
    """
    if not ref or not ref.startswith(filestore.REF_SCHEME):
        return False

    remaining = session.execute(
        select(func.count()).select_from(ItemRow).where(ItemRow.content_ref == ref)
    ).scalar_one()
    if remaining:
        return False

    # remove_body logs its own failures rather than raising: the rows are
    # already gone, so the deletion succeeded. A leftover file is recoverable
    # by a later sweep, while a half-failed deletion reported as an error would
    # invite a retry that finds nothing left to delete.
    return filestore.remove_body(ref, root=root)


def delete_items(
    session: Session, item_ids: list[str], *, root: Path | None = None
) -> dict[str, int]:
    """Delete items and everything that only existed because of them.

    Returns counts for the caller to report. An id that names no row is not an
    error: it counts zero. Two callers can race the same id, and a client
    retrying a request whose response was lost must see the same success the
    first attempt produced -- which is also why the endpoint is idempotent.

    Enrichment payloads are collected before the cascade removes their rows,
    because after the delete there is nothing left to look them up by.
    """
    if not item_ids:
        return {"items": 0, "read_states": 0, "enrichments": 0, "bodies": 0}

    rows = list(
        session.execute(select(ItemRow).where(ItemRow.id.in_(item_ids))).scalars()
    )
    if not rows:
        return {"items": 0, "read_states": 0, "enrichments": 0, "bodies": 0}

    found_ids = [row.id for row in rows]
    body_refs = {row.content_ref for row in rows if row.content_ref}
    product_refs = set(
        session.execute(
            select(EnrichmentRow.payload_ref).where(
                EnrichmentRow.item_id.in_(found_ids),
                EnrichmentRow.payload_ref.is_not(None),
            )
        ).scalars()
    )

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
    # Flush before counting references: the rows must be gone from the
    # transaction's view of the table, or every body looks still-referenced and
    # nothing is ever reclaimed.
    session.flush()

    bodies = 0
    for ref in body_refs | product_refs:
        bodies += int(_unlink_if_unreferenced(session, ref, root=root))

    return {
        "items": len(found_ids),
        "read_states": int(read_states),
        "enrichments": int(enrichments),
        "bodies": bodies,
    }


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
    before it goes -- which is what the confirmation dialog and the dry-run
    answer are both built from.

    `include_published` defaults true: deleting a whole source's contents is
    already an explicit, confirmed act, and excluding published items by
    default would leave the caller with a source that looks emptied while its
    articles are still in every client's cache.
    """
    query = select(ItemRow).where(ItemRow.source_id == source_id)
    if not include_published:
        query = query.where(ItemRow.publish_status != "published")
    if since:
        query = query.where(func.coalesce(ItemRow.published_at, "") >= since)
    if until:
        query = query.where(func.coalesce(ItemRow.published_at, "") <= until)
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
) -> dict[str, int]:
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
) -> dict[str, int]:
    """Delete a source and its contents (ADR 0021).

    Items go first, then the fetch records, then the source row. The order is
    the one the schema allows: both reference the source, so removing it first
    would leave the children pointing at nothing for as long as the transaction
    takes to commit.

    The source's `raw_fetch` rows and their payload files go with it. Those are
    diagnostic records rather than content, and a fetch log whose subject no
    longer exists cannot be read as anything.
    """
    row = session.get(SourceRow, source_id)
    if row is None:
        return {"sources": 0, "items": 0, "raw_fetches": 0, "bodies": 0}

    counts = delete_items(
        session,
        [
            item.id
            for item in items_for_source(session, source_id, include_published=True)
        ],
        root=root,
    )

    payload_refs = set(
        session.execute(
            select(RawFetchRow.payload_ref).where(
                RawFetchRow.source_id == source_id,
                RawFetchRow.payload_ref.is_not(None),
            )
        ).scalars()
    )
    raw_fetches = session.execute(
        select(func.count())
        .select_from(RawFetchRow)
        .where(RawFetchRow.source_id == source_id)
    ).scalar_one()
    session.execute(delete(RawFetchRow).where(RawFetchRow.source_id == source_id))
    session.flush()

    # Fetch payloads are not item bodies, so no item row can reference them;
    # they are unlinked outright. Collected before the rows were deleted, which
    # is why the refs were read first.
    bodies = counts["bodies"]
    for ref in payload_refs:
        bodies += int(_unlink_if_unreferenced(session, ref, root=root))

    session.execute(delete(SourceRow).where(SourceRow.id == source_id))

    return {
        "sources": 1,
        "items": counts["items"],
        "raw_fetches": int(raw_fetches),
        "bodies": bodies,
    }
