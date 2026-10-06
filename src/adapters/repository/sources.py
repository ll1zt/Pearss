"""Source persistence: the compatibility façade over origin and subscription.

ADR 0046 D-B. Internally everything now reads `origin` (the facts) and
`subscription` (the policy); this is the one module that still knows the
`source` table exists, and it does so for two reasons:

* **Writes.** `create_source` keeps inserting the `source` row, because
  `item.source_id`'s foreign key still resolves against it and migration 0027
  has not run yet. The row's columns are frozen at creation and nothing reads
  them -- which is why there is no mirror to keep in step: a mirror nobody
  reads is a second fact source with none of the benefits.
* **Reads at the old shape.** `get_source` / `list_sources` compose a
  `SourceView` from origin + subscription, so the HTTP payload built from them
  is unchanged while its data comes from the table that owns it.

`SourceView` is frozen on purpose: a caller that mutated a returned row would
otherwise believe it had changed something.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from adapters.db_models import Origin as OriginRow
from adapters.db_models import Source as SourceRow
from adapters.db_models import Subscription as SubscriptionRow
from adapters.repository.origins import create_origin
from adapters.repository.subscriptions import create_subscription
from core.enums import (
    SourceStatus,
)
from core.folders import DEFAULT_FOLDER

LOGGER = logging.getLogger(__name__)

# The patchable policy columns, all of them on the subscription. Listed so a
# caller cannot smuggle a column the write path does not mean to accept: an
# unknown name would otherwise become a plain Python attribute on the ORM
# object, silently changing nothing.
_POLICY_COLUMNS = frozenset(
    {
        "schedule_policy",
        "status",
        "trust_level",
        "pipeline_chain",
        "target_lang",
        "folder",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceView:
    """One source as the API still describes it: `origin` joined to its policy.

    The field names keep the `source` spelling because the HTTP payloads built
    from this are unchanged; renaming the surface is issue #366's job, not a
    side effect of moving the storage.
    """

    id: str
    type: str
    url: str | None
    canonical_url: str
    title: str | None
    parser_plugin_id: str | None
    seen_frontier: int | None
    floor_cursor: str | None
    schedule_policy: str
    pipeline_chain: str | None
    target_lang: str | None
    folder: str
    status: str
    trust_level: str
    tenant_id: str
    created_at: str
    updated_at: str


def derive_source_id(*, tenant_id: str, source_type: str, canonical_url: str) -> str:
    """Deterministic source id from the identity the uniqueness constraint uses.

    Deriving rather than generating randomly means "add this feed" is idempotent
    on its own terms: the same URL produces the same id, so a retried request
    cannot create a second source even before the Idempotency-Key is consulted.

    The name keeps `source` because it is what every caller and every stored
    `job.config` already spells; the value is the origin's id.
    """
    payload = "\x1f".join(("src1", tenant_id, source_type, canonical_url))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _view(origin: OriginRow, subscription: SubscriptionRow) -> SourceView:
    return SourceView(
        id=origin.id,
        type=origin.type,
        url=origin.url,
        canonical_url=origin.canonical_url,
        title=origin.title,
        parser_plugin_id=origin.parser_plugin_id,
        seen_frontier=origin.seen_frontier,
        floor_cursor=origin.floor_cursor,
        schedule_policy=subscription.schedule_policy,
        pipeline_chain=subscription.pipeline_chain,
        target_lang=subscription.target_lang,
        folder=subscription.folder,
        status=subscription.status,
        trust_level=subscription.trust_level,
        tenant_id=origin.tenant_id,
        created_at=origin.created_at,
        updated_at=origin.updated_at,
    )


def create_source(
    session: Session,
    *,
    source_type: str,
    url: str,
    canonical_url: str,
    schedule_policy: str,
    tenant_id: str,
    trust_level: str = "standard",
    parser_plugin_id: str | None = None,
    pipeline_chain: str | None = None,
    target_lang: str | None = None,
    folder: str = DEFAULT_FOLDER,
    at: str,
) -> SourceView:
    """Insert a source, or return the existing one for this identity.

    Returning the existing row rather than raising is what makes a retried
    create answer with the same body instead of a 409: the caller's intent was
    "this source should exist", and after the first request it does.

    The write is a façade over three tables while ADR 0046's split is staged.
    `source` keeps the foreign key `item.source_id` resolves against, and
    `origin`/`subscription` are written alongside it so the rows the read paths
    use already exist. `origin.id` and `subscription.id` are this `source.id`,
    which is what keeps the three addressable by one key -- and every item id,
    guid and `/v1/sources/{id}` URL unchanged.

    There is no revival branch any more. It existed for soft-deleted rows,
    which answered 201 while never being collected again; deletion now removes
    the row (ADR 0021), so a re-subscribe finds nothing and inserts fresh. The
    new source starts with an empty cursor and re-samples history -- which is
    what deletion means, and the reason `seen_frontier` is worth keeping when
    the intent was only to clear a bad crawl.
    """
    source_id = derive_source_id(
        tenant_id=tenant_id, source_type=source_type, canonical_url=canonical_url
    )
    existing = session.get(SourceRow, source_id)
    if existing is not None:
        # DEBUG: "add this feed" is idempotent, and a retrying client asks
        # again by design (issue #296).
        LOGGER.debug("source %s already exists; returning it", source_id)
        view = _compose(session, source_id)
        if view is None:
            raise LookupError(
                f"source {source_id!r} has a mirror row but no origin; the "
                "stage-A backfill did not run against this database"
            )
        return view

    row = SourceRow(
        id=source_id,
        type=source_type,
        url=url,
        canonical_url=canonical_url,
        parser_plugin_id=parser_plugin_id,
        schedule_policy=schedule_policy,
        pipeline_chain=pipeline_chain,
        target_lang=target_lang,
        folder=folder,
        status=SourceStatus.ACTIVE.value,
        trust_level=trust_level,
        tenant_id=tenant_id,
        created_at=at,
        updated_at=at,
    )
    session.add(row)
    session.flush()

    # The same write, expressed against the tables the split is moving to. The
    # origin is created first because the subscription points at it; both are
    # keyed on this row's id so the three stay one aggregate.
    origin = create_origin(
        session,
        origin_id=source_id,
        source_type=source_type,
        url=url,
        canonical_url=canonical_url,
        tenant_id=tenant_id,
        at=at,
        parser_plugin_id=parser_plugin_id,
    )
    subscription = create_subscription(
        session,
        subscription_id=source_id,
        origin_id=origin.id,
        schedule_policy=schedule_policy,
        tenant_id=tenant_id,
        at=at,
        pipeline_chain=pipeline_chain,
        target_lang=target_lang,
        folder=folder,
        trust_level=trust_level,
    )
    LOGGER.debug(
        "created source %s (%s) with its origin and subscription",
        source_id,
        source_type,
    )
    return _view(origin, subscription)


def _compose(session: Session, source_id: str) -> SourceView | None:
    """`origin` joined to its policy; None when there is no such origin.

    An origin without a subscription is not "not found": the 1:1 stage
    guarantees one, so its absence means a write bypassed this façade (a
    hand-written INSERT, say). Raised rather than reported as a 404, because a
    404 would read as "you named the wrong id" and hide the real breakage.
    """
    origin = session.get(OriginRow, source_id)
    if origin is None:
        return None
    subscription = session.get(SubscriptionRow, source_id)
    if subscription is None:
        raise LookupError(
            f"origin {source_id!r} has no subscription; stage A's 1:1 invariant "
            "was broken by a write that did not go through create_source"
        )
    return _view(origin, subscription)


def get_source(session: Session, source_id: str) -> SourceView | None:
    """The composed view, or None when there is no such origin."""
    return _compose(session, source_id)


def list_sources(
    session: Session, *, tenant_id: str, limit: int, offset: int = 0
) -> list[SourceView]:
    """One page of sources, oldest first so a cursor stays stable under inserts."""
    rows = session.execute(
        select(OriginRow, SubscriptionRow)
        .join(SubscriptionRow, SubscriptionRow.origin_id == OriginRow.id)
        .where(OriginRow.tenant_id == tenant_id)
        .order_by(OriginRow.created_at, OriginRow.id)
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return [_view(origin, subscription) for origin, subscription in rows]


def update_source_policy(
    session: Session,
    *,
    source_id: str,
    changes: Mapping[str, str | None],
    at: str,
) -> SourceView | None:
    """Apply a PATCH to the policy half, returning the composed view.

    Every patchable field is policy, so the write lands on the subscription --
    `origin` owns none of them and is left alone.
    """
    subscription = session.get(SubscriptionRow, source_id)
    if subscription is None:
        return None
    unknown = sorted(set(changes) - _POLICY_COLUMNS)
    if unknown:
        raise ValueError(f"not subscription policy columns: {unknown}")
    for column, value in changes.items():
        setattr(subscription, column, value)
    subscription.updated_at = at
    session.flush()
    return get_source(session, source_id)


def delete_source_mirror(session: Session, *, source_id: str) -> None:
    """Remove the stage-A mirror row.

    The façade's job for the same reason `create_source` is: the row exists only
    because `item.source_id`'s foreign key still resolves against it, and
    knowing when it may go is this module's business. Called after the items
    are gone -- the foreign key cascades from it.
    """
    session.execute(delete(SourceRow).where(SourceRow.id == source_id))
