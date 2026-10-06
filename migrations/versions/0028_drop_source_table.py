"""drop `source`, and make origin/subscription the only source of facts

The last stage of ADR 0046's split. Stage A (0026) copied the facts into
`origin` and the policy into `subscription`; stage B (0027) moved every read and
write onto them and left `source` as a mirror only the compatibility façade
touched. This removes the mirror: `item`/`raw_fetch` keep `origin_id` (now NOT
NULL and a foreign key into `origin`), lose `source_id`, and the table goes.

The two tables are rebuilt by hand rather than with `batch_alter_table`, for a
reason that is measured rather than stylistic: batch recreates a table from
reflected DDL, and SQLite reflects 0022's VIRTUAL generated column
(`item.sort_key`) onto the *next* column -- the batch rendered
`origin_id VARCHAR(64) VIRTUAL` and the CREATE failed outright (the same failure
0026's downgrade hit). Hand-written DDL also keeps the CHECK constraints, which
SQLite reflection does not return at all: a batch recreate silently drops
`ck_item_publishstatus` and `ck_raw_fetch_errorclass`, leaving the enums
unenforced by the database -- the thing AGENTS.md says must not happen.

Each SQL line is assembled from short Python literals for a second, unrelated
reason: the foreign key has to reach SQLite as one line, because SQLAlchemy's
SQLite reflection parses the name and the `ON DELETE` out of the DDL text. Split
across lines it comes back unnamed and without its cascade rule, and
`alembic check` then reports a spurious remove/add pair for the constraint the
migration just created.

`item_fts` is a standalone FTS5 table (0023: no `content=` option), so it is
unaffected by the rebuild; it joins back on `item_id`.

The copy is `INSERT INTO item_new ... SELECT ... FROM item`, which fails loudly
if any row has a NULL `origin_id` -- a row that would otherwise become an item
nobody can reach. 0026 backfilled every row and every write path has set it
since, so a failure here means a write bypassed the façade, and that is worth
stopping for.

`PRAGMA foreign_keys` is left at its default (off) for the migration
connection: `DROP TABLE source` with enforcement on would fire
`item.source_id`'s ON DELETE CASCADE and delete every item (SQLite runs an
implicit DELETE FROM when it drops a table). The child side is gone by the time
the table is dropped, so there is nothing to cascade to -- but the order below
is what makes that true, not luck.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-06

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Inlined, like every migration: a migration is a historical record and must
# produce the same schema years from now even if a taxonomy has changed since.
_PUBLISH_STATUS = ("pending", "ready", "published")
_ERROR_CLASS = ("network", "parse", "structure_changed", "blocked", "auth")
_SOURCE_TYPE = ("rss", "web", "agent")
_SOURCE_STATUS = ("active", "paused", "disabled")
_TRUST_LEVEL = ("standard", "trusted", "restricted")
_DEFAULT_FOLDER = "default"
EPOCH_FLOOR = "0000-01-01T00:00:00.000000Z"

# The fact columns, in the order they are written. `sort_key` is generated and
# therefore never named in an INSERT.
_ITEM_FACTS = (
    "id, guid, content_hash, title, url, author, published_at, content_ref,"
    " publish_status, published_to_feed_at, tenant_id, created_at, updated_at"
)
_RAW_FETCH_FACTS = (
    "payload_ref, content_hash, http_status, etag, last_modified, status,"
    " error_class, fetched_at, correlation_id"
)

# The head shape: one `origin_id`, NOT NULL, cascading from `origin`.
_CREATE_ITEM = (
    "CREATE TABLE item_new (\n"
    "    id VARCHAR(64) NOT NULL,\n"
    "    origin_id VARCHAR(64) NOT NULL,\n"
    "    guid TEXT NOT NULL,\n"
    "    content_hash VARCHAR(128) NOT NULL,\n"
    "    title TEXT NOT NULL,\n"
    "    url TEXT NOT NULL,\n"
    "    author TEXT DEFAULT ('') NOT NULL,\n"
    "    published_at TEXT,\n"
    "    content_ref TEXT,\n"
    "    publish_status VARCHAR(16) NOT NULL,\n"
    "    published_to_feed_at TEXT,\n"
    "    tenant_id VARCHAR(64) NOT NULL,\n"
    "    created_at TEXT NOT NULL,\n"
    "    updated_at TEXT NOT NULL,\n"
    "    sort_key TEXT NOT NULL GENERATED ALWAYS AS"
    f" (coalesce(published_at, '{EPOCH_FLOOR}')) VIRTUAL,\n"
    "    CONSTRAINT pk_item PRIMARY KEY (id),\n"
    "    CONSTRAINT fk_item_origin_id_origin FOREIGN KEY(origin_id)"
    " REFERENCES origin (id) ON DELETE CASCADE,\n"
    "    CONSTRAINT uq_item_guid UNIQUE (tenant_id, guid),\n"
    "    CONSTRAINT ck_item_publishstatus CHECK"
    f" (publish_status IN {_PUBLISH_STATUS})\n"
    ")\n"
)

_CREATE_RAW_FETCH = (
    "CREATE TABLE raw_fetch_new (\n"
    "    id VARCHAR(64) NOT NULL,\n"
    "    origin_id VARCHAR(64) NOT NULL,\n"
    "    payload_ref TEXT,\n"
    "    content_hash VARCHAR(128),\n"
    "    http_status INTEGER,\n"
    "    etag TEXT,\n"
    "    last_modified TEXT,\n"
    "    status TEXT NOT NULL,\n"
    "    error_class VARCHAR(32),\n"
    "    fetched_at TEXT NOT NULL,\n"
    "    correlation_id VARCHAR(64) NOT NULL,\n"
    "    CONSTRAINT pk_raw_fetch PRIMARY KEY (id),\n"
    "    CONSTRAINT fk_raw_fetch_origin_id_origin FOREIGN KEY(origin_id)"
    " REFERENCES origin (id) ON DELETE CASCADE,\n"
    "    CONSTRAINT ck_raw_fetch_errorclass CHECK"
    f" (error_class IN {_ERROR_CLASS})\n"
    ")\n"
)

# The 0027 shape this downgrades to: `source_id` NOT NULL with its foreign key
# into `source`, and the nullable `origin_id` stage A added next to it. Both
# columns are filled from the same value, which is what the previous release
# reads (`source_id`) and what its own guard checks them equal against
# (`origin_id`).
_CREATE_ITEM_LEGACY = (
    "CREATE TABLE item_new (\n"
    "    id VARCHAR(64) NOT NULL,\n"
    "    source_id VARCHAR(64) NOT NULL,\n"
    "    guid TEXT NOT NULL,\n"
    "    content_hash VARCHAR(128) NOT NULL,\n"
    "    title TEXT NOT NULL,\n"
    "    url TEXT NOT NULL,\n"
    "    author TEXT DEFAULT ('') NOT NULL,\n"
    "    published_at TEXT,\n"
    "    content_ref TEXT,\n"
    "    publish_status VARCHAR(16) NOT NULL,\n"
    "    published_to_feed_at TEXT,\n"
    "    tenant_id VARCHAR(64) NOT NULL,\n"
    "    created_at TEXT NOT NULL,\n"
    "    updated_at TEXT NOT NULL,\n"
    "    sort_key TEXT NOT NULL GENERATED ALWAYS AS"
    f" (coalesce(published_at, '{EPOCH_FLOOR}')) VIRTUAL,\n"
    "    origin_id VARCHAR(64),\n"
    "    CONSTRAINT pk_item PRIMARY KEY (id),\n"
    "    CONSTRAINT fk_item_source_id_source FOREIGN KEY(source_id)"
    " REFERENCES source (id) ON DELETE CASCADE,\n"
    "    CONSTRAINT uq_item_guid UNIQUE (tenant_id, guid),\n"
    "    CONSTRAINT ck_item_publishstatus CHECK"
    f" (publish_status IN {_PUBLISH_STATUS})\n"
    ")\n"
)

_CREATE_RAW_FETCH_LEGACY = (
    "CREATE TABLE raw_fetch_new (\n"
    "    id VARCHAR(64) NOT NULL,\n"
    "    source_id VARCHAR(64) NOT NULL,\n"
    "    payload_ref TEXT,\n"
    "    content_hash VARCHAR(128),\n"
    "    http_status INTEGER,\n"
    "    etag TEXT,\n"
    "    last_modified TEXT,\n"
    "    status TEXT NOT NULL,\n"
    "    error_class VARCHAR(32),\n"
    "    fetched_at TEXT NOT NULL,\n"
    "    correlation_id VARCHAR(64) NOT NULL,\n"
    "    origin_id VARCHAR(64),\n"
    "    CONSTRAINT pk_raw_fetch PRIMARY KEY (id),\n"
    "    CONSTRAINT fk_raw_fetch_source_id_source FOREIGN KEY(source_id)"
    " REFERENCES source (id) ON DELETE CASCADE,\n"
    "    CONSTRAINT ck_raw_fetch_errorclass CHECK"
    f" (error_class IN {_ERROR_CLASS})\n"
    ")\n"
)

_CREATE_SOURCE = (
    "CREATE TABLE source (\n"
    "    id VARCHAR(64) NOT NULL,\n"
    "    type VARCHAR(16) NOT NULL,\n"
    "    url TEXT,\n"
    "    canonical_url TEXT NOT NULL,\n"
    "    title TEXT,\n"
    "    parser_plugin_id VARCHAR(128),\n"
    "    seen_frontier BIGINT,\n"
    "    floor_cursor TEXT,\n"
    "    schedule_policy TEXT NOT NULL,\n"
    "    pipeline_chain TEXT,\n"
    "    target_lang VARCHAR(16),\n"
    f"    folder TEXT DEFAULT '{_DEFAULT_FOLDER}' NOT NULL,\n"
    "    status VARCHAR(16) NOT NULL,\n"
    "    trust_level VARCHAR(16) NOT NULL,\n"
    "    tenant_id VARCHAR(64) NOT NULL,\n"
    "    created_at TEXT NOT NULL,\n"
    "    updated_at TEXT NOT NULL,\n"
    "    CONSTRAINT pk_source PRIMARY KEY (id),\n"
    "    CONSTRAINT ck_source_sourcetype CHECK"
    f" (type IN {_SOURCE_TYPE}),\n"
    "    CONSTRAINT ck_source_sourcestatus CHECK"
    f" (status IN {_SOURCE_STATUS}),\n"
    "    CONSTRAINT ck_source_trustlevel CHECK"
    f" (trust_level IN {_TRUST_LEVEL}),\n"
    "    CONSTRAINT uq_source_url UNIQUE (tenant_id, canonical_url, type)\n"
    ")\n"
)


def _rebuild_item(*, legacy: bool) -> None:
    """Replace `item`, with `origin_id` as the fact or the 0027 pair."""
    if legacy:
        op.execute(_CREATE_SOURCE)
        op.execute(
            "insert into source (id, type, url, canonical_url, title,"
            " parser_plugin_id, seen_frontier, floor_cursor, schedule_policy,"
            " pipeline_chain, target_lang, folder, status, trust_level,"
            " tenant_id, created_at, updated_at)"
            " select o.id, o.type, o.url, o.canonical_url, o.title,"
            " o.parser_plugin_id, o.seen_frontier, o.floor_cursor,"
            " s.schedule_policy, s.pipeline_chain, s.target_lang, s.folder,"
            " s.status, s.trust_level, o.tenant_id, o.created_at, o.updated_at"
            " from origin o join subscription s on s.origin_id = o.id"
        )
        op.execute(_CREATE_ITEM_LEGACY)
        op.execute(
            f"insert into item_new (id, source_id, origin_id, {_ITEM_FACTS})"
            f" select id, origin_id, origin_id, {_ITEM_FACTS} from item"
        )
    else:
        op.execute(_CREATE_ITEM)
        op.execute(
            f"insert into item_new (id, origin_id, {_ITEM_FACTS})"
            f" select id, origin_id, {_ITEM_FACTS} from item"
        )

    op.execute("DROP TABLE item")
    op.execute("ALTER TABLE item_new RENAME TO item")
    op.create_index(
        "ix_item_status_published", "item", ["publish_status", "published_at"]
    )
    op.create_index("ix_item_content_hash", "item", ["content_hash"])
    op.create_index("ix_item_sort_key_id", "item", ["sort_key", "id"])
    # 0027 already moved the composite onto `origin_id`, so this index is the
    # same in both shapes. `ix_item_source_sort_key_id` (0022) is the one that
    # only the 0027 shape has: it indexes `source_id`, which the upgrade drops.
    op.create_index(
        "ix_item_origin_sort_key_id", "item", ["origin_id", "sort_key", "id"]
    )
    if legacy:
        op.create_index(
            "ix_item_source_sort_key_id", "item", ["source_id", "sort_key", "id"]
        )
    op.execute("ANALYZE")


def _rebuild_raw_fetch(*, legacy: bool) -> None:
    """Replace `raw_fetch` the same way, with its own index pair."""
    if legacy:
        op.execute(_CREATE_RAW_FETCH_LEGACY)
        op.execute(
            f"insert into raw_fetch_new (id, source_id, origin_id, {_RAW_FETCH_FACTS})"
            f" select id, origin_id, origin_id, {_RAW_FETCH_FACTS} from raw_fetch"
        )
    else:
        op.execute(_CREATE_RAW_FETCH)
        op.execute(
            f"insert into raw_fetch_new (id, origin_id, {_RAW_FETCH_FACTS})"
            f" select id, origin_id, {_RAW_FETCH_FACTS} from raw_fetch"
        )

    op.execute("DROP TABLE raw_fetch")
    op.execute("ALTER TABLE raw_fetch_new RENAME TO raw_fetch")
    if legacy:
        op.create_index(
            "ix_raw_fetch_source_id_fetched_at",
            "raw_fetch",
            ["source_id", "fetched_at"],
        )
        op.create_index("ix_raw_fetch_origin_id", "raw_fetch", ["origin_id"])
    else:
        op.create_index(
            "ix_raw_fetch_origin_id_fetched_at",
            "raw_fetch",
            ["origin_id", "fetched_at"],
        )


def upgrade() -> None:
    """Retire the mirror: `origin`/`subscription` are the only fact tables."""
    _rebuild_item(legacy=False)
    _rebuild_raw_fetch(legacy=False)
    # Last, and only now: the child tables no longer reference it, so there is
    # no ON DELETE CASCADE left to fire on the implicit DELETE FROM.
    op.execute("DROP TABLE source")


def downgrade() -> None:
    """Bring the mirror back, and with it the two `source_id` columns."""
    _rebuild_item(legacy=True)
    _rebuild_raw_fetch(legacy=True)
