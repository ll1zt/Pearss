"""Subscription persistence: the policy half of the source/origin split.

Stage A (ADR 0046 D-B): `source` still answers every read, and this module is
the write/read pair for the table the policy columns are moving to. The 1:1
stage is what makes `subscription.id == source.id`: it keeps the item ids,
guids and `/v1/sources/{id}` URLs a client holds meaningful while the split is
staged.

The one-subscription-per-origin rule lives in the schema, not here, so a
caller that tries a second subscription gets an IntegrityError rather than a
second row. The migration that removes it is what changes this module's
meaning, not a flag in it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import Subscription as SubscriptionRow
from core.enums import SourceStatus

LOGGER = logging.getLogger(__name__)


def create_subscription(
    session: Session,
    *,
    subscription_id: str,
    origin_id: str,
    schedule_policy: str,
    tenant_id: str,
    at: str,
    pipeline_chain: str | None = None,
    target_lang: str | None = None,
    folder: str,
    trust_level: str = "standard",
    status: str = SourceStatus.ACTIVE.value,
) -> SubscriptionRow:
    """Insert a subscription, or return the existing row sharing its id.

    Idempotent on the id for the same reason `create_origin` is: the id is
    derived from the identity the constraint uses, so a retried create finds
    its own row rather than racing the unique constraint.
    """
    existing = session.get(SubscriptionRow, subscription_id)
    if existing is not None:
        LOGGER.debug("subscription %s already exists; returning it", subscription_id)
        return existing

    row = SubscriptionRow(
        id=subscription_id,
        tenant_id=tenant_id,
        origin_id=origin_id,
        schedule_policy=schedule_policy,
        pipeline_chain=pipeline_chain,
        target_lang=target_lang,
        folder=folder,
        status=status,
        trust_level=trust_level,
        created_at=at,
        updated_at=at,
    )
    session.add(row)
    session.flush()
    LOGGER.debug("created subscription %s for origin %s", subscription_id, origin_id)
    return row


def get_subscription(session: Session, subscription_id: str) -> SubscriptionRow | None:
    return session.get(SubscriptionRow, subscription_id)


def list_subscriptions(
    session: Session, *, tenant_id: str, limit: int, offset: int = 0
) -> list[SubscriptionRow]:
    """One page, oldest first so a cursor stays stable under inserts."""
    return list(
        session.execute(
            select(SubscriptionRow)
            .where(SubscriptionRow.tenant_id == tenant_id)
            .order_by(SubscriptionRow.created_at, SubscriptionRow.id)
            .limit(limit + 1)
            .offset(offset)
        ).scalars()
    )
