"""CLI: queue processor products for items stored before a chain was enabled.

`just enrichment-backfill <source_id> [limit]`. The pipeline gates *new*
items at collection time; this one-off pass brings already-stored items
into the same pipeline, idempotently (same product identity, so re-running
queues nothing for items already queued or succeeded).
"""

from __future__ import annotations

import os
import sys

from adapters.repository import backfill_chain
from adapters.db_models import Source
from adapters.repository.enrichment import parse_chain


def main() -> None:
    source_id = os.environ.get("PEARSS_BACKFILL_SOURCE_ID", "")
    limit_raw = os.environ.get("PEARSS_BACKFILL_LIMIT", "")
    if not source_id:
        print("usage: just enrichment-backfill <source_id> [limit]", file=sys.stderr)
        raise SystemExit(2)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = os.environ.get("PEARSS_DATABASE_URL") or "sqlite+pysqlite:///data/pearss.db"
    session_factory = sessionmaker(create_engine(url), expire_on_commit=False)
    with session_factory() as session:
        source = session.get(Source, source_id)
        if source is None:
            print(f"no source {source_id!r}", file=sys.stderr)
            raise SystemExit(1)
        specs = parse_chain(source.pipeline_chain, target_lang=source.target_lang)
        if not specs:
            print(
                f"source {source_id!r} has no pipeline_chain; nothing to backfill",
                file=sys.stderr,
            )
            raise SystemExit(1)
        limit = int(limit_raw) if limit_raw else None
        queued = backfill_chain(session, source_id=source_id, limit=limit)
        session.commit()
    print(f"queued {queued} enrichment row(s)")


if __name__ == "__main__":
    main()
