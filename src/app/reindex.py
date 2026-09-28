"""CLI: index stored items the search index does not have yet.

`just reindex [batch]`. Migration 0023 creates the table but cannot fill it:
filling it means reading a body per item out of the filestore, and a migration
has to stay fast and reversible. The scheduler also tops the index up
incrementally, so this command is for the bulk case -- a corpus that predates
the index -- and for forcing the catch-up when someone wants it now rather than
within the interval.

Idempotent by construction: `index_missing` only writes items the table does not
already have, so running this twice writes nothing the second time.
"""

from __future__ import annotations

import os
import sys
import time

from adapters.repository.search_index import index_missing, optimize

BATCH = 500


def main() -> None:
    raw = os.environ.get("PEARSS_REINDEX_BATCH", "")
    try:
        batch = int(raw) if raw else BATCH
    except ValueError:
        print(f"batch must be a number, got {raw!r}", file=sys.stderr)
        raise SystemExit(2) from None
    if batch < 1:
        print("batch must be at least 1", file=sys.stderr)
        raise SystemExit(2)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = os.environ.get("PEARSS_DATABASE_URL") or "sqlite+pysqlite:///data/pearss.db"
    session_factory = sessionmaker(create_engine(url), expire_on_commit=False)

    indexed_total = 0
    started = time.monotonic()
    while True:
        with session_factory() as session:
            indexed = index_missing(session, limit=batch)
            session.commit()
        indexed_total += indexed
        if indexed < batch:
            break

    # After a bulk load FTS5 is holding a lot of small segments, which every
    # query then reads through. Merging them is the ADR's instruction after a
    # backfill, and it is cheap when there is nothing to merge.
    with session_factory() as session:
        optimize(session)
        session.commit()

    elapsed = time.monotonic() - started
    print(f"reindexed {indexed_total} item(s) in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
