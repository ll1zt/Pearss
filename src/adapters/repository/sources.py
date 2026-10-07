"""Source persistence: one row out of two tables.

ADR 0046 D-B/D-C. `origin` holds the facts, `subscription` the policy; this
module is where a caller that still thinks in one "source" gets both. It is the
compatibility seam, and it is deliberately small: `create_source` writes the two
rows, `get_source`/`list_sources` compose a `SourceView`, and
`update_source_policy` writes the patchable half.

`SourceView` is frozen on purpose: a caller that mutated a returned row would
otherwise believe it had changed something.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import Origin as OriginRow
from adapters.db_models import Subscription as SubscriptionRow
from adapters.repository.origins import create_origin
from adapters.repository.subscriptions import create_subscription
from core.enums import SourceStatus
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

    # The origin's id: the field names keep the `source` spelling because the
    # HTTP payloads built from this are unchanged (issue #366 renames the
    # surface, not this). `subscription_id` is the channel's own identity --
    # equal to `id` while the 1:1 rule holds, different for a second channel
    # over the same origin (ADR 0061).
    id: str
    subscription_id: str
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
        subscription_id=subscription.id,
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


def _revive_subscription(
    session: Session,
    subscription_id: str,
    *,
    at: str,
    schedule_policy: str,
    pipeline_chain: str | None,
    target_lang: str | None,
    folder: str,
    trust_level: str,
) -> None:
    """Switch a subscription back on if it was unsubscribed (ADR 0063).

    Subscribing to something already subscribed is idempotent and returns the
    existing row unchanged. Subscribing to something that was *unsubscribed* is
    a new intent about the same source, and the row that carries it -- along
    with the readiness rows that make its items visible again -- is still
    there, so it is re-activated rather than re-created.

    The settings that come with the new request are the ones that take effect:
    a caller re-subscribing with a 2h interval means 2h, and a subscription
    that ignored it would silently collect on the old schedule. A subscription
    that is paused is left alone -- paused is a state the user chose, not one
    unsubscribe set.
    """
    subscription = session.get(SubscriptionRow, subscription_id)
    if subscription is None or subscription.status != SourceStatus.DISABLED.value:
        return
    subscription.status = SourceStatus.ACTIVE.value
    subscription.schedule_policy = schedule_policy
    subscription.pipeline_chain = pipeline_chain
    subscription.target_lang = target_lang
    subscription.folder = folder
    subscription.trust_level = trust_level
    subscription.updated_at = at
    session.flush()


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

    Both rows share this id, which is what keeps every item id, guid and
    `/v1/sources/{id}` URL a client already holds meaningful.

    There is a revival branch again, and it means something narrower than the
    one that was removed (ADR 0063). That one revived *soft-deleted* rows,
    which answered 201 while never being collected again; this one re-activates
    a subscription that was unsubscribed. Unsubscribing keeps the row -- and
    with it the per-channel readiness that makes its items visible -- so
    subscribing again switches it back on instead of inserting a second one.
    Purging still removes the rows outright, so a subscribe after a purge finds
    nothing and inserts fresh, with an empty cursor that re-samples history.
    """
    source_id = derive_source_id(
        tenant_id=tenant_id, source_type=source_type, canonical_url=canonical_url
    )
    existing = session.get(OriginRow, source_id)
    if existing is not None:
        # DEBUG: "add this feed" is idempotent, and a retrying client asks
        # again by design (issue #296).
        LOGGER.debug("source %s already exists; returning it", source_id)
        _revive_subscription(
            session,
            source_id,
            at=at,
            schedule_policy=schedule_policy,
            pipeline_chain=pipeline_chain,
            target_lang=target_lang,
            folder=folder,
            trust_level=trust_level,
        )
        view = _compose(session, source_id)
        if view is None:
            raise LookupError(f"source {source_id!r} has no origin row")
        return view

    # The origin first: the subscription points at it. Both are keyed on the
    # derived id so the pair stays one aggregate.
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
        "created source %s (%s) as origin + subscription", source_id, source_type
    )
    return _view(origin, subscription)


def _compose(session: Session, source_id: str) -> SourceView | None:
    """`origin` joined to its policy; None when there is no such origin.

    An origin without a subscription is not "not found": the 1:1 stage
    guarantees one, so its absence means a write bypassed this façade (a
    hand-written INSERT, say). Raised rather than reported as a 404, because a
    404 would read as "you named the wrong id" and hide the real breakage.
    """
    subscription = session.get(SubscriptionRow, source_id)
    if subscription is not None:
        # A subscription id resolves through its origin: stage A made the two
        # equal, and a second channel over the same origin (ADR 0061) does not,
        # so looking the origin up by the subscription id would find nothing and
        # report a row that exists as unreadable.
        origin = session.get(OriginRow, subscription.origin_id)
        if origin is None:
            return None
        return _view(origin, subscription)
    if session.get(OriginRow, source_id) is None:
        return None
    raise LookupError(
        f"origin {source_id!r} has no subscription; the invariant was broken "
        "by a write that did not go through create_source"
    )


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
