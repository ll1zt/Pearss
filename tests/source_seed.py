"""Seed a source the way the write path does, with an id the test chose.

`create_source` derives the id from the canonical URL -- that is what makes
"add this feed" idempotent -- and that is exactly what makes it unusable for a
test that names its source `s1` and asserts on that name. This writes the same
three rows the façade writes: `source` (the mirror `item.source_id` still
resolves against), `origin` (the facts) and `subscription` (the policy), so the
read paths find what the test seeded.

It is the one place a test may hand-write these rows. When the schema gains a
column the façade writes, it belongs here too: the point of keeping it in one
module is that this is the only place to look.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from adapters.db_models import Origin as OriginRow
from adapters.db_models import Source as SourceRow
from adapters.db_models import Subscription as SubscriptionRow
from core.enums import SourceStatus, TrustLevel
from core.folders import DEFAULT_FOLDER

DEFAULT_AT = "2026-01-01T00:00:00.000000Z"


def mirror_sources(connection: Session | Connection) -> None:
    """Create origin/subscription rows for any `source` row that lacks them.

    Several suites seed the mirror row with SQL directly: it is the row the
    previous release wrote, and the ids those tests assert on are theirs to
    choose. The read paths use origin/subscription now, so those rows have to
    exist -- this is the same copy migration 0026 performs, applied to whatever
    the test just inserted. Call it after the INSERT, before the commit.
    """
    connection.execute(
        text(
            "insert into origin (id, tenant_id, type, url, canonical_url, title,"
            " parser_plugin_id, seen_frontier, floor_cursor, created_at,"
            " updated_at)"
            " select id, tenant_id, type, url, canonical_url, title,"
            " parser_plugin_id, seen_frontier, floor_cursor, created_at,"
            " updated_at from source where id not in (select id from origin)"
        )
    )
    connection.execute(
        text(
            "insert into subscription (id, tenant_id, origin_id, schedule_policy,"
            " pipeline_chain, target_lang, folder, status, trust_level,"
            " created_at, updated_at)"
            " select id, tenant_id, id, schedule_policy, pipeline_chain,"
            " target_lang, folder, status, trust_level, created_at, updated_at"
            " from source where id not in (select id from subscription)"
        )
    )


def seed_source(
    session: Session,
    *,
    source_id: str,
    url: str | None,
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
    """Insert source + origin + subscription for `source_id`; returns the id."""
    canonical = canonical_url if canonical_url is not None else url
    session.add(
        SourceRow(
            id=source_id,
            type=source_type,
            url=url,
            canonical_url=canonical,
            title=title,
            parser_plugin_id=parser_plugin_id,
            schedule_policy=schedule_policy,
            pipeline_chain=pipeline_chain,
            target_lang=target_lang,
            folder=folder,
            status=status,
            trust_level=trust_level,
            tenant_id=tenant_id,
            created_at=at,
            updated_at=at,
        )
    )
    session.add(
        OriginRow(
            id=source_id,
            type=source_type,
            url=url,
            canonical_url=canonical,
            title=title,
            parser_plugin_id=parser_plugin_id,
            tenant_id=tenant_id,
            created_at=at,
            updated_at=at,
        )
    )
    session.add(
        SubscriptionRow(
            id=source_id,
            tenant_id=tenant_id,
            origin_id=source_id,
            schedule_policy=schedule_policy,
            pipeline_chain=pipeline_chain,
            target_lang=target_lang,
            folder=folder,
            status=status,
            trust_level=trust_level,
            created_at=at,
            updated_at=at,
        )
    )
    session.flush()
    return source_id
