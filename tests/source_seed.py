"""Seed a source the way the write path does, with an id the test chose.

`create_source` derives the id from the canonical URL -- that is what makes
"add this feed" idempotent -- and that is exactly what makes it unusable for a
test that names its source `s1` and asserts on that name. This writes the two
rows the façade writes: `origin` (the facts) and `subscription` (the policy),
sharing one id, so the read paths find what the test seeded.

It takes a `Session` or a plain `Connection` because both kinds of fixture are
in use: a suite that seeds inside a `with engine.begin()` block has no session,
and demanding one would mean restructuring the fixture for the helper's sake.

It is the one place a test may hand-write these rows. When the schema gains a
column the façade writes, it belongs here too: the point of keeping it in one
module is that this is the only place to look.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from adapters.db_models import Origin as OriginRow
from adapters.db_models import Subscription as SubscriptionRow
from core.enums import SourceStatus, TrustLevel
from core.folders import DEFAULT_FOLDER

DEFAULT_AT = "2026-01-01T00:00:00.000000Z"


def seed_source(
    session: Session | Connection,
    *,
    source_id: str,
    url: str | None,
    origin_id: str | None = None,
    canonical_url: str | None = None,
    source_type: str = "rss",
    schedule_policy: str = "{}",
    pipeline_chain: str | None = None,
    target_lang: str | None = None,
    folder: str = DEFAULT_FOLDER,
    status: str = SourceStatus.ACTIVE.value,
    trust_level: str = TrustLevel.STANDARD.value,
    parser_plugin_id: str | None = None,
    title: str | None = None,
    tenant_id: str = "default",
    at: str = DEFAULT_AT,
) -> str:
    """Insert origin + subscription for `source_id`; returns the id.

    `origin_id` defaults to `source_id` (the 1:1 stage's rule). Passing it
    separately is how a test builds the state the legacy channel alias exists
    for: an origin id that is not a subscription id.
    """
    canonical = canonical_url if canonical_url is not None else url
    facts_id = origin_id if origin_id is not None else source_id
    origin: dict[str, Any] = {
        "id": facts_id,
        "type": source_type,
        "url": url,
        "canonical_url": canonical,
        "title": title,
        "parser_plugin_id": parser_plugin_id,
        "tenant_id": tenant_id,
        "created_at": at,
        "updated_at": at,
    }
    subscription: dict[str, Any] = {
        "id": source_id,
        "tenant_id": tenant_id,
        "origin_id": facts_id,
        "schedule_policy": schedule_policy,
        "pipeline_chain": pipeline_chain,
        "target_lang": target_lang,
        "folder": folder,
        "status": status,
        "trust_level": trust_level,
        "created_at": at,
        "updated_at": at,
    }
    if isinstance(session, Session):
        session.add(OriginRow(**origin))
        session.add(SubscriptionRow(**subscription))
        session.flush()
    else:
        session.execute(sa.insert(OriginRow).values(**origin))
        session.execute(sa.insert(SubscriptionRow).values(**subscription))
    return source_id
