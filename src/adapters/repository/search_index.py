"""The full-text index: written and read through one `tokenize()` (ADR 0035).

The invariant this module exists to keep is that the text goes in and comes out
through the *same* segmentation. An index built from one and queried with
another matches nothing, and nothing raises -- so every write and every read
here calls `core.text_search.tokenize()`, and the golden set in
`tests/unit/test_text_search.py` is what proves the two agree.

The gate is not this module's business. `item_fts` holds every item, `pending`
included, and `matching_item_ids` filters visibility by joining back to `item`
through the existing predicate. Indexing only published items would put a second
copy of the gate in the write path, and would need a row written the moment an
item is promoted (ADR 0035 decision 4).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from adapters import filestore
from adapters.db_models import Item as ItemRow
from adapters.repository._shared import visible_item_rows
from core.language import prose_text
from core.text_search import phrase_query, tokenize

LOGGER = logging.getLogger(__name__)

__all__ = [
    "count_matches",
    "deindex_items",
    "index_item",
    "index_missing",
    "matching_item_ids",
    "optimize",
    "search_rows",
]

# A lightweight handle on the virtual table. FTS5 tables cannot live in the
# ORM's metadata -- see the migration -- so the JOIN goes through this.
_FTS = sa.table("item_fts", sa.column("item_id", sa.Text))


def index_item(session: Session, *, item_id: str, title: str, body: str) -> None:
    """Write one item's searchable text, replacing whatever was there.

    `body` is markup; the reader's prose is extracted here, so the index holds
    words rather than tags. Delete-then-insert rather than an upsert, because
    FTS5 has no upsert and rewriting is what makes an edited item lose its old
    text instead of accumulating both versions.
    """
    deindex_items(session, [item_id])
    session.execute(
        sa.text(
            "INSERT INTO item_fts (item_id, title_tok, body_tok)"
            " VALUES (:item_id, :title_tok, :body_tok)"
        ),
        {
            "item_id": item_id,
            "title_tok": tokenize(title),
            "body_tok": tokenize(prose_text(body)),
        },
    )
    LOGGER.debug("indexed item %s", item_id)


def deindex_items(session: Session, item_ids: Sequence[str]) -> None:
    """Drop items from the index. Ids that were never indexed are ignored.

    Every deletion path has to come through here. `item_fts` is a virtual table,
    so the schema's ON DELETE CASCADE does not reach it: a row left behind is a
    search result that cannot be opened, which is the shape ADR 0021 exists to
    prevent. The three deletion paths all funnel into
    `repository.deletion.delete_items`, which is why one call there covers them.
    """
    if not item_ids:
        return
    session.execute(
        sa.text("DELETE FROM item_fts WHERE item_id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        ),
        {"ids": list(item_ids)},
    )
    LOGGER.debug("deindexed %d item(s)", len(item_ids))


def index_missing(session: Session, *, limit: int = 500) -> int:
    """Index items the table does not have yet, and say how many there were.

    This is the catch-up path, not the write path: it exists so a corpus that
    predates the index -- or a write that failed after its item was stored --
    converges without anyone running a command. `just reindex` calls it in a
    loop for the bulk case.

    The comparison is done in Python rather than as a `NOT EXISTS` subquery on
    purpose: SQLite does not allow an index on a virtual table, so probing
    `item_fts.item_id` once per item is a scan of the whole index per item --
    quadratic, and quadratic in the one operation whose whole job is to run over
    everything. Reading the ids once is linear and, at this corpus size,
    a few milliseconds.
    """
    indexed = {
        row[0] for row in session.execute(sa.text("SELECT item_id FROM item_fts"))
    }
    pending = [
        (row.id, row.title, row.content_ref)
        for row in session.execute(
            sa.select(ItemRow.id, ItemRow.title, ItemRow.content_ref)
        )
        if row.id not in indexed
    ]
    for item_id, title, content_ref in pending[:limit]:
        index_item(
            session,
            item_id=item_id,
            title=title,
            body=filestore.read_body(content_ref),
        )
    return min(len(pending), limit)


def optimize(session: Session) -> None:
    """Merge the index's segments.

    FTS5 keeps what it is given as separate segments and merges them lazily;
    after a bulk load that is a lot of small segments to read through on every
    query. The ADR asks for this after a backfill, and it is cheap enough to
    run whenever a large batch has just gone in.
    """
    session.execute(sa.text("INSERT INTO item_fts(item_fts) VALUES('optimize')"))


# FTS5's bm25 returns a *negative* score whose magnitude grows with
# relevance, so "best first" is ASCENDING and the next page is *worse* --
# a larger score. Reading that backwards is the mistake that silently reverses
# the results, which is why it is written out rather than implied by a DESC.
_SCORE: Any = sa.literal_column("bm25(item_fts)")


def search_rows(
    session: Session,
    *,
    conditions: Sequence[Any],
    text: str,
    limit: int,
    cursor: tuple[str, str] | None,
) -> tuple[list[tuple[Any, float]], str | None]:
    """One page of matches, most relevant first, plus the cursor for the next.

    `conditions` are the caller's filters, already built -- the gate included --
    so this module does not need to know what an item filter is, and the
    visibility rule stays in exactly one place.

    Pagination is a keyset on (score, id) for the same reason the time-ordered
    list uses one: an offset walks a shifting window. The score is stable for a
    given query and index, and the id breaks ties, so "strictly past the last
    row" is well defined. A cursor carries the score as its printed form, which
    round-trips exactly.
    """
    expression = phrase_query(text)
    if not expression:
        return [], None

    statement = (
        sa.select(ItemRow, _SCORE.label("score"))
        .select_from(_FTS.join(ItemRow, _FTS.c.item_id == ItemRow.id))
        .where(*conditions)
        .where(sa.text("item_fts MATCH :match").bindparams(match=expression))
    )
    if cursor is not None:
        value, last_id = cursor
        statement = statement.where(
            sa.or_(
                float(value) < _SCORE,
                sa.and_(float(value) == _SCORE, ItemRow.id < last_id),
            )
        )
    rows = session.execute(
        statement.order_by(_SCORE.asc(), ItemRow.id.desc()).limit(limit + 1)
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last_row, last_score = page[-1]
        next_cursor = f"{last_score!r}|{last_row.id}"
    return [(row, score) for row, score in page], next_cursor


def count_matches(session: Session, *, conditions: Sequence[Any], text: str) -> int:
    """How many items the match selects in total.

    Shares the conditions and the match clause with `search_rows` for the reason
    the list count shares them with its page: a count that drifts from what it
    counts is worse than no count.
    """
    expression = phrase_query(text)
    if not expression:
        return 0
    statement = (
        sa.select(sa.func.count())
        .select_from(_FTS.join(ItemRow, _FTS.c.item_id == ItemRow.id))
        .where(*conditions)
        .where(sa.text("item_fts MATCH :match").bindparams(match=expression))
    )
    return int(session.execute(statement).scalar_one())


def matching_item_ids(session: Session, *, query: str, limit: int) -> list[str]:
    """The gate-visible items whose text matches `query`, best match first.

    Visibility is enforced by the join, through the same predicate the list
    endpoints use -- so a `pending` item is not reachable by searching for its
    text, and becomes reachable the moment it is published, with no write to
    this table.

    An empty query returns nothing rather than everything: `phrase_query` gives
    back an empty expression for text with no searchable characters, and an
    empty MATCH expression is a syntax error, not a wildcard.
    """
    expression = phrase_query(query)
    if not expression:
        return []
    statement = (
        sa.select(_FTS.c.item_id)
        .select_from(_FTS.join(ItemRow, _FTS.c.item_id == ItemRow.id))
        .where(visible_item_rows())
        .where(sa.text("item_fts MATCH :match").bindparams(match=expression))
        .order_by(sa.text("bm25(item_fts)"))
        .limit(limit)
    )
    return [row[0] for row in session.execute(statement)]
