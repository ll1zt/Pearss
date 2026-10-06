"""Item storage: the write path executes core's dedupe decision.

core is pure and returns a decision; this module applies it. That split is
what lets the dedupe rule be tested without a database."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.repository._shared import StoreOutcome, _optional_timestamp, row_to_item
from adapters.timestamps import fixed_width_utc as timestamp
from core.enums import PublishStatus
from core.models import Item
from core.normalize import DedupeAction, decide_dedupe

LOGGER = logging.getLogger(__name__)


def existing_items_for_source(session: Session, source_id: str) -> list[Item]:
    """Load the items a dedupe decision needs for a source.

    Scoped to the source to keep the comparison set small; cross-source
    duplicates are found by content_hash lookup instead, which the database
    can serve without loading every row.
    """
    rows = session.execute(
        select(ItemRow).where(ItemRow.origin_id == source_id)
    ).scalars()
    return [row_to_item(row) for row in rows]


def find_by_content_hash(session: Session, content_hash: str) -> list[Item]:
    """Every item with this content hash, across all sources."""
    rows = session.execute(
        select(ItemRow).where(ItemRow.content_hash == content_hash)
    ).scalars()
    return [row_to_item(row) for row in rows]


def find_by_identity(session: Session, *, item_id: str, guid: str) -> list[Item]:
    """Items that are this same entry, by id or guid.

    Both are checked because the dedupe rule compares either, and a row written
    by an earlier version of the derivation could match one and not the other.
    """
    rows = session.execute(
        select(ItemRow).where(or_(ItemRow.id == item_id, ItemRow.guid == guid))
    ).scalars()
    return [row_to_item(row) for row in rows]


def _match_source(
    matched_id: str | None, *, identity: list[Item], existing: list[Item]
) -> str:
    """Which comparison matched: the identity key or the content hash.

    The two are unioned for the decision, but they mean different things: an
    identity match is the same entry re-fetched, a content match is the same
    article from another source. Naming which one fired is the difference
    between "the origin reworded it" and "two feeds carry it" (issue #292).
    """
    if matched_id is None:
        return "none"
    if any(seen.id == matched_id for seen in identity):
        return "identity"
    if any(seen.id == matched_id for seen in existing):
        return "content_hash"
    # unreachable: `matched_id` is taken from the union of these two lists, so a
    # match that is in neither cannot reach here. Kept as a total function, and
    # declared rather than tested -- there is no input that exercises it
    # (AC-311).
    return "unknown"


def store_item(session: Session, candidate: Item) -> StoreOutcome:
    """Persist `candidate`, applying the dedupe rule.

    The comparison set is the union of "same entry" and "same content": the
    first catches an article whose body changed between fetches, the second
    catches the same article arriving from another source. Neither alone is
    enough, and content-only matching is what previously let a reworded article
    fall through to an INSERT that collided on the frozen guid.
    """
    identity = find_by_identity(session, item_id=candidate.id, guid=candidate.guid)
    existing = list(identity)
    for other in find_by_content_hash(session, candidate.content_hash):
        if all(seen.id != other.id for seen in existing):
            existing.append(other)
    decision = decide_dedupe(candidate, existing)
    # Every decision, at DEBUG: which rule fired and what it matched. The INFO
    # lines below cover only the terminal outcomes, and "why did this draft not
    # become a row?" is the question a diagnosis opens with (issue #292).
    LOGGER.debug(
        "dedupe %s: %s matched %s via %s",
        decision.action.value,
        candidate.id,
        decision.matched_id or "-",
        _match_source(decision.matched_id, identity=identity, existing=existing),
    )

    if decision.action in (DedupeAction.INSERT, DedupeAction.INSERT_RELATED):
        _insert(session, candidate)
        if decision.action is DedupeAction.INSERT_RELATED:
            # Stored, but the relationship to its twin is recorded rather than
            # left implicit; a future migration can promote this to a relation
            # table without changing the decision logic.
            #
            # DEBUG, not INFO (issue #297): this fires once per related draft,
            # so a batch of them would make INFO grow with the input. The
            # per-draft line in `normalise_and_store` carries the same fact at
            # the same level.
            LOGGER.debug(
                "related item stored: %s duplicates %s",
                candidate.id,
                decision.matched_id,
            )
    elif decision.action is DedupeAction.UPDATE_PENDING:
        if not _update_pending(
            session, candidate, matched_id=decision.matched_id or ""
        ):
            decision = replace(
                decision,
                action=DedupeAction.SKIP_PUBLISHED_CONTENT_CHANGED,
                reason=(
                    "entry published before content replacement; keeping frozen content"
                ),
            )
    else:
        if decision.action is DedupeAction.SKIP_PUBLISHED_CONTENT_CHANGED:
            # Worth a warning rather than a debug line: the article a reader
            # sees will differ from the source's current text, and nothing
            # will fix that by itself.
            LOGGER.warning(
                "content changed after publication, keeping the published version: %s",
                candidate.id,
            )
        else:
            # DEBUG, not INFO (issue #297): one line per deduped draft would
            # make INFO grow with the batch. `normalise_and_store` reports the
            # same outcome per draft at the same level.
            LOGGER.debug(
                "not stored (%s): %s -> %s",
                decision.action.value,
                candidate.id,
                decision.matched_id,
            )

    return StoreOutcome(
        action=decision.action,
        item=candidate,
        matched_id=decision.matched_id,
        reason=decision.reason,
    )


def _update_pending(session: Session, candidate: Item, *, matched_id: str) -> bool:
    """Replace unseen content and close its gate atomically with the status check."""
    result = session.execute(
        update(ItemRow)
        .where(
            ItemRow.id == matched_id,
            ItemRow.tenant_id == candidate.tenant_id,
            ItemRow.publish_status.in_(
                [PublishStatus.PENDING.value, PublishStatus.READY.value]
            ),
            ItemRow.published_to_feed_at.is_(None),
        )
        .values(
            title=candidate.title,
            url=candidate.url,
            author=candidate.author,
            content_hash=candidate.content_hash,
            content_ref=candidate.content_ref,
            publish_status=PublishStatus.PENDING.value,
            published_at=_optional_timestamp(candidate.published_at),
            updated_at=timestamp(datetime.now(UTC)),
        )
        .returning(ItemRow.id)
    )
    return result.scalar_one_or_none() is not None


def _insert(session: Session, item: Item) -> None:
    now = timestamp(datetime.now(UTC))
    session.add(
        ItemRow(
            id=item.id,
            # The truth column first, then the stage-A legacy column it
            # replaces. Both are written equal: 0027 drops `source_id`, so
            # until then it is a mirror that only the compatibility façade
            # reads.
            origin_id=item.origin_id,
            source_id=item.origin_id,
            guid=item.guid,
            content_hash=item.content_hash,
            title=item.title,
            url=item.url,
            author=item.author,
            published_at=_optional_timestamp(item.published_at),
            content_ref=item.content_ref,
            publish_status=item.publish_status.value,
            published_to_feed_at=_optional_timestamp(item.published_to_feed_at),
            tenant_id=item.tenant_id,
            created_at=timestamp(item.created_at) if item.created_at else now,
            updated_at=timestamp(item.updated_at) if item.updated_at else now,
        )
    )
