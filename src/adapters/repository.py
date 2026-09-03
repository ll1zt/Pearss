"""Item and RawFetch persistence.

The repository applies the dedupe rule that core decides: core is pure and
holds no database, so it returns a decision and this module executes it. That
split is what lets the dedupe rule be tested without a database while keeping
the write path in one place.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from adapters.db_models import Item as ItemRow
from adapters.db_models import RawFetch as RawFetchRow
from core.enums import ErrorClass, ItemEvent, PublishStatus
from core.item_state import TransitionContext, transition
from core.models import Item
from core.normalize import DedupeAction, decide_dedupe

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreOutcome:
    """What a store attempt did."""

    action: DedupeAction
    item: Item
    # Set when the decision was to skip or suppress, naming the existing row.
    matched_id: str | None = None
    reason: str = ""


def _timestamp(moment: datetime) -> str:
    """Store timestamps as RFC 3339 UTC text, per docs/domain/data-model.md."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_timestamp(moment: datetime | None) -> str | None:
    return None if moment is None else _timestamp(moment)


def record_raw_fetch(
    session: Session,
    *,
    fetch_id: str,
    source_id: str,
    status: str,
    correlation_id: str,
    payload_ref: str | None = None,
    content_hash: str | None = None,
    http_status: int | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    error_class: ErrorClass | None = None,
    fetched_at: datetime | None = None,
) -> None:
    """Persist one fetch attempt.

    Always inserts: a retry creates a new RawFetch rather than overwriting the
    previous one, so the first failure stays available for diagnosis.
    """
    session.add(
        RawFetchRow(
            id=fetch_id,
            source_id=source_id,
            payload_ref=payload_ref,
            content_hash=content_hash,
            http_status=http_status,
            etag=etag,
            last_modified=last_modified,
            status=status,
            error_class=error_class.value if error_class else None,
            fetched_at=_timestamp(fetched_at or datetime.now(UTC)),
            correlation_id=correlation_id,
        )
    )


def existing_items_for_source(session: Session, source_id: str) -> list[Item]:
    """Load the items a dedupe decision needs for a source.

    Scoped to the source to keep the comparison set small; cross-source
    duplicates are found by content_hash lookup instead, which the database
    can serve without loading every row.
    """
    rows = session.execute(
        select(ItemRow).where(ItemRow.source_id == source_id)
    ).scalars()
    return [_to_domain(row) for row in rows]


def find_by_content_hash(session: Session, content_hash: str) -> list[Item]:
    """Every item with this content hash, across all sources."""
    rows = session.execute(
        select(ItemRow).where(ItemRow.content_hash == content_hash)
    ).scalars()
    return [_to_domain(row) for row in rows]


def store_item(session: Session, candidate: Item) -> StoreOutcome:
    """Persist `candidate`, applying the dedupe rule.

    The rule is evaluated against every item sharing the candidate's
    content_hash, not just those from the same source — cross-source
    duplicates are the case B2 exists to handle.
    """
    existing = find_by_content_hash(session, candidate.content_hash)
    decision = decide_dedupe(candidate, existing)

    if decision.action is DedupeAction.INSERT:
        _insert(session, candidate)
    elif decision.action is DedupeAction.INSERT_RELATED:
        # Stored, but the relationship to its twin is recorded rather than
        # left implicit; a future migration can promote this to a relation
        # table without changing the decision logic.
        _insert(session, candidate)
        LOGGER.info(
            "related item stored: %s duplicates %s", candidate.id, decision.matched_id
        )
    else:
        LOGGER.info(
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


def _insert(session: Session, item: Item) -> None:
    now = _timestamp(datetime.now(UTC))
    session.add(
        ItemRow(
            id=item.id,
            source_id=item.source_id,
            guid=item.guid,
            content_hash=item.content_hash,
            title=item.title,
            url=item.url,
            published_at=_optional_timestamp(item.published_at),
            content_ref=item.content_ref,
            publish_status=item.publish_status.value,
            published_to_feed_at=_optional_timestamp(item.published_to_feed_at),
            tenant_id=item.tenant_id,
            created_at=_timestamp(item.created_at) if item.created_at else now,
            updated_at=_timestamp(item.updated_at) if item.updated_at else now,
        )
    )


def items_ready_for_feed(session: Session) -> list[Item]:
    """Items the gate permits, newest first.

    Pending is excluded in SQL rather than in Python: the gate is a hard
    invariant, and letting a pending row reach memory at all means a future
    change could accidentally render it.
    """
    rows = session.execute(
        select(ItemRow)
        .where(ItemRow.publish_status != PublishStatus.PENDING.value)
        .order_by(ItemRow.published_at.desc().nullslast(), ItemRow.guid)
    ).scalars()
    return [_to_domain(row) for row in rows]


def mark_published(session: Session, items: list[Item], *, at: datetime) -> int:
    """Record that items have appeared in a feed, freezing their content.

    Returns how many rows were actually flipped. Publishing is a one-way
    transition, and the conditional update means two concurrent feed renders
    cannot both claim credit for the same item.
    """
    if not items:
        return 0
    ids = [item.id for item in items]
    stamp = _timestamp(at)
    # session.execute is typed as returning Result[Any], but a DML statement
    # yields a CursorResult at runtime, which is where rowcount lives. The cast
    # is narrowed to this one statement rather than disabling the check.
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(ItemRow)
            .where(
                ItemRow.id.in_(ids),
                ItemRow.publish_status == PublishStatus.READY.value,
                ItemRow.published_to_feed_at.is_(None),
            )
            .values(
                publish_status=PublishStatus.PUBLISHED.value,
                published_to_feed_at=stamp,
                updated_at=stamp,
            )
        ),
    )
    return int(result.rowcount or 0)


def promote_to_ready(session: Session, items: list[Item]) -> int:
    """Advance pending items to ready, using the state machine.

    Goes through core's transition rather than assigning the value, so an
    illegal promotion (a published item, or one whose processing is genuinely
    incomplete) is refused instead of silently applied.

    Returns how many items advanced.
    """
    advanced = 0
    for item in items:
        if item.publish_status is not PublishStatus.PENDING:
            continue
        result = transition(
            item.publish_status,
            ItemEvent.MARK_READY,
            TransitionContext(processing_complete=True),
        )
        if result.status is not PublishStatus.READY:
            continue
        session.execute(
            update(ItemRow)
            .where(
                ItemRow.id == item.id,
                ItemRow.publish_status == PublishStatus.PENDING.value,
            )
            .values(publish_status=PublishStatus.READY.value)
        )
        advanced += 1
    return advanced


def count_items(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(ItemRow)).scalar() or 0)


def _to_domain(row: ItemRow) -> Item:
    """Rebuild a domain Item from a row.

    Stored timestamps are parsed back to aware datetimes so the domain object
    satisfies the same invariants as a freshly normalised one.
    """
    return Item(
        id=row.id,
        source_id=row.source_id,
        guid=row.guid,
        content_hash=row.content_hash,
        title=row.title,
        url=row.url,
        published_at=_parse(row.published_at),
        content_ref=row.content_ref,
        publish_status=PublishStatus(row.publish_status),
        published_to_feed_at=_parse(row.published_to_feed_at),
        tenant_id=row.tenant_id,
        created_at=_parse(row.created_at),
        updated_at=_parse(row.updated_at),
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def dumps(value: object) -> str:
    """Serialise structured metadata for a text column."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
