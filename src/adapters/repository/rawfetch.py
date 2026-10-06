"""RawFetch persistence: one append-only row per fetch attempt.

Always inserts - a retry is a new row, so the first failure stays available."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from adapters.db_models import RawFetch as RawFetchRow
from adapters.timestamps import fixed_width_utc as timestamp
from core.enums import (
    ErrorClass,
)


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
            # Both columns: `origin_id` is the fact, `source_id` the stage-A
            # mirror 0027 drops. raw_fetch is diagnostic data, so a drift here
            # would cost a lookup, not content -- but the guard test still
            # holds the two equal.
            origin_id=source_id,
            source_id=source_id,
            payload_ref=payload_ref,
            content_hash=content_hash,
            http_status=http_status,
            etag=etag,
            last_modified=last_modified,
            status=status,
            error_class=error_class.value if error_class else None,
            fetched_at=timestamp(fetched_at or datetime.now(UTC)),
            correlation_id=correlation_id,
        )
    )
