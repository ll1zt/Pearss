"""SQLAlchemy ORM models.

Persistence lives in adapters/, not core/: core owns what an Item *means*,
this module owns how it is stored. The enum columns reuse the core enums
directly so the database, the API and the MCP tool schema cannot drift apart.

Every table here mirrors docs/domain/data-model.md. P2/P3 tables arrive with
their own migration rather than being pre-created empty; ItemEnrichment is
the P3 processor-chain fact source (migration 0011).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase

from adapters.timestamps import EPOCH_FLOOR
from core.tenancy import SINGLE_TENANT

# P1 targets SQLite and P3 may move to Postgres, so column types stay within
# the subset both support: no JSON-specific operators, no native enums.

# Centralised so the model layer and the migration cannot disagree about the
# spelling of a taxonomy. Migrations inline their own copy: a migration is a
# historical record and must not import application code that will change.
METADATA_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Lengths must be wide enough for the longest value in each taxonomy, plus
# room for members added later without a migration.
ENUM_LENGTHS = {
    "error_class": 32,
    "publish_status": 16,
    "job_run_status": 16,
    "source_status": 16,
    "source_type": 16,
    "audit_outcome": 16,
    "trust_level": 16,
    # The plugin table's own taxonomies; not source/trust borrowings.
    "plugin_status": 16,
    "plugin_trust": 16,
    # The work ledger (ADR 0046).
    "work_status": 16,
    "unit_outcome": 16,
}


class Base(DeclarativeBase):
    """Declarative base with explicit constraint naming.

    Named constraints are what make downgrade reliable: an unnamed unique
    constraint gets an unpredictable name per engine, so dropping it in a
    downgrade requires guessing.
    """

    metadata = sa.MetaData(naming_convention=METADATA_NAMING_CONVENTION)


def enum_check(name: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    """Build a named CHECK constraint pinning `column` to `values`.

    A plain VARCHAR would accept anything: SQLite does not enforce an enum on
    its own, so AGENTS.md's "failures must use the ErrorClass enum, never a
    bare string" would hold only inside the application. Pushing it into the
    database means a direct SQL write or an older process cannot smuggle in a
    value that breaks the taxonomy.

    Writing the constraint as a CHECK over a plain string column rather than
    as a SQLAlchemy Enum also keeps Alembic's autogenerate quiet: it can
    compare CHECK constraints it understands, but a native Enum on SQLite is
    reported as a spurious type change forever.
    """
    literal = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({literal})", name=name)


class Source(Base):
    """A content source. Fact source: backed up."""

    __tablename__ = "source"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    type: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["source_type"]), nullable=False
    )
    url: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    # NOT NULL because this is the dedupe key. A nullable column in a unique
    # constraint is not covered by it — SQL treats NULLs as distinct — so a
    # row without one could duplicate any other, silently.
    canonical_url: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    # The name the origin gives itself, discovered at collection time. Nullable
    # because a source created before its first fetch legitimately has none; the
    # Reader API falls back to the id rather than showing a blank sidebar.
    title: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    # Retained in P1 even though the plugin registry arrives in P2, so a web
    # source can record which collector parsed it from the start.
    parser_plugin_id: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(128), nullable=True
    )
    # Paging state for sources whose origin is walked a page at a time. RSS has
    # no such concept, so both are unused by the other collectors.
    # Not derivable from `item`: a cursor is an opaque string from the origin,
    # and a gap position is not recoverable from stored rows.
    #
    # `seen_frontier` is the newest ingested tweet id; `floor_cursor` is the
    # oldest cursor any walk reached, the interval's lower end. Together they
    # bound what a walk may fetch. (`resume_rounds` was removed in 0016: the
    # interval model has no backlog to age out.)
    seen_frontier: sa.orm.Mapped[int | None] = sa.orm.mapped_column(
        sa.BigInteger, nullable=True
    )
    floor_cursor: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    # Per-source scheduling: fixed interval or adaptive bounds, plus the
    # consecutive-failure count that drives backoff.
    schedule_policy: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    # Processing chain the source's items must complete before the gate opens.
    # JSON list of {plugin_id, kind, lang, required}; NULL means no chain (P1
    # behaviour: items go ready straight after normalisation).
    pipeline_chain: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    # Target language for language products (translation); default for the
    # chain's language specs that omit their own.
    target_lang: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(16), nullable=True
    )
    # The subscription's sidebar folder, one per source (FreshRSS constrains a
    # feed to a single category and clients are built to that). NOT NULL with
    # the default name rather than nullable: an unclassified subscription still
    # sits in a folder the client can show, and two spellings of "none" would
    # make every reader of this column handle both.
    folder: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.Text, nullable=False, server_default=SINGLE_TENANT
    )
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["source_status"]), nullable=False
    )
    # Recorded intent about the source. Never makes fetched content trusted —
    # see core.enums.TrustLevel for why that distinction is load-bearing.
    trust_level: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["trust_level"]), nullable=False
    )
    # Single-user today; carried from the outset because retrofitting tenancy
    # into tables that lack it is far more painful than carrying a column.
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)

    __table_args__ = (
        enum_check("sourcetype", "type", ("rss", "web", "agent")),
        enum_check(
            "sourcestatus",
            "status",
            ("active", "paused", "disabled"),
        ),
        enum_check(
            "trustlevel",
            "trust_level",
            ("standard", "trusted", "restricted"),
        ),
        # One source per canonical URL and type within a tenant.
        sa.UniqueConstraint("tenant_id", "canonical_url", "type", name="uq_source_url"),
    )


class Origin(Base):
    """Where content comes from: the fact half of the split (ADR 0046 D-A).

    Stage A: `Source` still carries both halves and every read path uses it, so
    this table is written alongside `source` and read only by the repository
    functions added with it. The 1:1 stage gives it the `Source`'s own id,
    which is what keeps every existing item id, guid and `/v1/sources/{id}` URL
    meaningful while the split is staged.

    Fact, not policy. `schedule_policy`, `pipeline_chain` and `folder` say how
    a client consumes this origin, and they live on `Subscription` -- two
    clients consuming one origin is the case the split exists for.
    """

    __tablename__ = "origin"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    type: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["source_type"]), nullable=False
    )
    url: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    # NOT NULL for the reason `Source.canonical_url` is: this is the dedupe
    # key, and a nullable column in a unique constraint is not covered by it --
    # SQL treats NULLs as distinct -- so a row without one could duplicate any
    # other, silently.
    canonical_url: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    # The name the origin gives itself, discovered at collection time.
    # Nullable because a source created before its first fetch legitimately has
    # none; the display layer falls back to the id rather than a blank label.
    title: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    parser_plugin_id: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(128), nullable=True
    )
    # Paging state for origins walked a page at a time. Not derivable from
    # `item`: a cursor is an opaque string from the origin, and a gap position
    # is not recoverable from stored rows.
    seen_frontier: sa.orm.Mapped[int | None] = sa.orm.mapped_column(
        sa.BigInteger, nullable=True
    )
    floor_cursor: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)

    __table_args__ = (
        enum_check("sourcetype", "type", ("rss", "web", "agent")),
        # The dedupe identity, moved off `source` unchanged.
        sa.UniqueConstraint("tenant_id", "canonical_url", "type", name="uq_origin_url"),
    )


class Subscription(Base):
    """How one client consumes an origin: the policy half of the split.

    Written alongside its `source` in stage A and read by the repository
    functions added with it, so the rows the later stages read already exist
    for sources created from the moment this lands.

    `UNIQUE(tenant_id, origin_id)` is stage A only: it states "one subscription
    per origin", which is true today and stops being true when a second
    subscription becomes meaningful -- that migration drops it, and the test
    that pins it here is what makes the removal deliberate rather than silent.
    """

    __tablename__ = "subscription"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    origin_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("origin.id", ondelete="CASCADE"), nullable=False
    )
    # Per-subscription scheduling: fixed interval or adaptive bounds, plus the
    # consecutive-failure count that drives backoff.
    schedule_policy: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    # Processing chain the subscription's items must complete before the gate
    # opens. JSON list of {plugin_id, kind, lang, required}; NULL means no
    # chain (items go ready straight after normalisation).
    pipeline_chain: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    # Target language for language products (translation); default for the
    # chain's language specs that omit their own.
    target_lang: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(16), nullable=True
    )
    # The sidebar folder this subscription sits in. NOT NULL with the default
    # name rather than nullable: an unclassified subscription still sits in a
    # folder the client can show, and two spellings of "none" would make every
    # reader of this column handle both.
    folder: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.Text, nullable=False, server_default=SINGLE_TENANT
    )
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["source_status"]), nullable=False
    )
    # Recorded intent about the origin. Never makes fetched content trusted --
    # see core.enums.TrustLevel for why that distinction is load-bearing.
    trust_level: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["trust_level"]), nullable=False
    )
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)

    __table_args__ = (
        enum_check(
            "sourcestatus",
            "status",
            ("active", "paused", "disabled"),
        ),
        enum_check(
            "trustlevel",
            "trust_level",
            ("standard", "trusted", "restricted"),
        ),
        # Stage A only; the multi-subscription migration removes it.
        sa.UniqueConstraint("tenant_id", "origin_id", name="uq_subscription_origin"),
    )


class RawFetch(Base):
    """One fetch attempt. Rebuildable diagnostic data, not backed up."""

    __tablename__ = "raw_fetch"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    source_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )
    # Stage A of the split: nullable, backfilled from `source_id` by migration
    # 0026, and not read by anything yet. Migration 0027 is what makes it NOT
    # NULL, adds the foreign key and retires `source_id`.
    origin_id: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    # Reference into file storage; the payload itself never enters the DB.
    payload_ref: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    content_hash: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(128), nullable=True
    )
    http_status: sa.orm.Mapped[int | None] = sa.orm.mapped_column(
        sa.Integer, nullable=True
    )
    # Conditional-request state for the next fetch.
    etag: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    last_modified: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    error_class: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["error_class"]), nullable=True
    )
    fetched_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    correlation_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(64), nullable=False
    )

    __table_args__ = (
        enum_check(
            "errorclass",
            "error_class",
            ("network", "parse", "structure_changed", "blocked", "auth"),
        ),
        # The conditional-request state for the next fetch is the most recent
        # row for a source; raw_fetch only grows, so this needs an index.
        sa.Index("ix_raw_fetch_source_id_fetched_at", "source_id", "fetched_at"),
        # Stage A only: the column 0027 keeps, under the name the reads will
        # use. Its composite replacement arrives with that migration.
        sa.Index("ix_raw_fetch_origin_id", "origin_id"),
    )


class Item(Base):
    """A normalised content item. Fact source: backed up."""

    __tablename__ = "item"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    source_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )
    # Stage A of the split: nullable, backfilled from `source_id` by migration
    # 0026, and not read by anything yet. Migration 0027 is what makes it NOT
    # NULL, adds the foreign key, retires `source_id` and replaces the index
    # below with `(origin_id, sort_key, id)`.
    origin_id: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    # Stable across the item's life; frozen once published.
    guid: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    content_hash: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(128), nullable=False
    )
    title: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    url: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    # The origin's byline, empty when it names nobody. Not nullable: the empty
    # string is the meaningful value here ("unknown"), and NULL would make
    # every reader of this column handle two spellings of one fact. The
    # server_default lets the migration add a NOT NULL column to a populated
    # table without a separate backfill pass.
    author: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.Text, nullable=False, server_default=""
    )
    published_at: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    content_ref: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    # The publish gate: pending items are invisible to every output.
    publish_status: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["publish_status"]), nullable=False
    )
    published_to_feed_at: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    # The feed's ordering key: `coalesce(published_at, EPOCH_FLOOR)` as a
    # VIRTUAL generated column, purely so it can be indexed by name.
    #
    # The expression form cannot be relied on. An index on `coalesce(...)`
    # matches a query only while the expression text is character-for-character
    # the same, so it goes *silently* unused the first time the floor, the
    # function or the rendered binding changes -- measured: an expression index
    # served this page in 0.13ms, and the same query rendering the floor as a
    # bound parameter fell back to a full scan plus a temp B-tree (3.0ms).
    # Matching on a column name cannot fail that way.
    sort_key: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.Text,
        sa.Computed(f"coalesce(published_at, '{EPOCH_FLOOR}')", persisted=False),
        nullable=False,
    )

    __table_args__ = (
        enum_check(
            "publishstatus",
            "publish_status",
            ("pending", "ready", "published"),
        ),
        sa.UniqueConstraint("tenant_id", "guid", name="uq_item_guid"),
        sa.Index("ix_item_status_published", "publish_status", "published_at"),
        # Cross-source dedupe looks items up by content hash once per stored
        # draft during every collection.
        sa.Index("ix_item_content_hash", "content_hash"),
        # Every feed read is "gate-visible rows, newest first", paged. The
        # status index cannot serve it: the gate is a negation
        # (`publish_status != 'pending'`) and the ordering is on the sort key,
        # so that index was never chosen -- the plan was a full scan plus a
        # temp B-tree (issue #77).
        sa.Index("ix_item_sort_key_id", "sort_key", "id"),
        sa.Index("ix_item_source_sort_key_id", "source_id", "sort_key", "id"),
        # Stage A only: the column 0027 keeps. The composite above is what
        # serves the paged feed read once the reads switch over.
        sa.Index("ix_item_origin_id", "origin_id"),
    )


class Job(Base):
    """A durable scheduling declaration, independent of the trigger."""

    __tablename__ = "job"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    type: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    schedule: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    priority: sa.orm.Mapped[int] = sa.orm.mapped_column(sa.Integer, nullable=False)
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    config: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)


class JobRun(Base):
    """A single execution of a job. Rebuildable; retained for diagnosis."""

    __tablename__ = "job_run"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    job_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["job_run_status"]), nullable=False
    )
    error_class: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["error_class"]), nullable=True
    )
    # Structured, safe-to-log evidence; never raw upstream bodies.
    metrics: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    correlation_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(64), nullable=False
    )
    # Retries point back at the run they retry instead of rewriting it.
    retry_of: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.ForeignKey("job_run.id", ondelete="SET NULL"), nullable=True
    )
    started_at: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    finished_at: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )

    __table_args__ = (
        enum_check(
            "jobrunstatus",
            "status",
            (
                "queued",
                "running",
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
            ),
        ),
        enum_check(
            "errorclass",
            "error_class",
            ("network", "parse", "structure_changed", "blocked", "auth"),
        ),
        # One active run per job, enforced by the schema so two processes
        # cannot both collect the same source. The application-level claim
        # (the queued -> running conditional update) sits on top of this, not
        # instead of it: each caller creates its own run id, so only the
        # unique index can arbitrate between them.
        sa.Index(
            "uq_job_run_active_per_job",
            "job_id",
            unique=True,
            sqlite_where=sa.text("status IN ('queued', 'running')"),
            postgresql_where=sa.text("status IN ('queued', 'running')"),
        ),
        # The scheduler reads each job's recent runs every tick: the failure
        # streak walks started_at, the last finished run walks finished_at.
        sa.Index("ix_job_run_job_id_started_at", "job_id", "started_at"),
        sa.Index("ix_job_run_job_id_finished_at", "job_id", "finished_at"),
    )


class FeedOut(Base):
    """An outbound RSS feed configuration. Fact source: backed up."""

    __tablename__ = "feed_out"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    name: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    filter_query: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    # Processor chain; empty means items go ready straight after normalisation.
    pipeline_chain: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    format: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(32), nullable=False)
    # Hash only: a plaintext token in the database is a token waiting to leak.
    token_hash: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)


class ReadState(Base):
    """Per-item consumption state written back by the Reader API.

    Fact source: backed up. There is one row per (item, tenant) once a client
    reports any state; the absence of a row means unread and unstarred rather
    than being stored as an explicit false.

    Deliberately separate from Item: consumption state is mutable and
    client-owned, while a published Item's content is frozen. Keeping them
    apart is what lets a reader mark something read without touching content.
    """

    __tablename__ = "read_state"

    # One row per item per tenant, so the primary key doubles as the
    # uniqueness constraint and an upsert is a genuine upsert.
    item_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("item.id", ondelete="CASCADE"), primary_key=True
    )
    is_read: sa.orm.Mapped[bool] = sa.orm.mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    is_starred: sa.orm.Mapped[bool] = sa.orm.mapped_column(
        sa.Boolean, nullable=False, default=False
    )
    # Server receipt time, not client time: last-write-wins is resolved here,
    # and a client clock cannot be trusted to order events.
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(64), primary_key=True
    )


class ReaderCredential(Base):
    """The Reader API login, stored as a password hash.

    Single-user, so this holds one row. The plaintext password lives only in
    the environment (PEARSS_READER_PASSWORD); what is stored here is a scrypt
    hash, which is also the input the auth token is derived from — see
    ADR 0005 for why stdlib scrypt rather than bcrypt.
    """

    __tablename__ = "reader_credential"

    username: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(128), primary_key=True
    )
    # scrypt output, stored as "<n>$<r>$<p>$<salt-hex>$<hash-hex>" so the
    # parameters travel with the hash and can be changed later.
    password_hash: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)


class IdempotencyRecord(Base):
    """A replayed write's stored result.

    AGENTS.md requires every API write to be idempotent, and the style guide
    defines the identity as actor + endpoint + key with the first result reused
    within a reasonable window. That needs somewhere to keep the first result:
    checking for a duplicate without a stored response would mean replaying the
    side effect and hoping it is harmless.

    Rows expire. Keeping them forever would grow the table without bound while
    the guarantee it serves only needs to cover a client's retry window.
    """

    __tablename__ = "idempotency_record"

    actor: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, primary_key=True)
    endpoint: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, primary_key=True)
    key: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(128), primary_key=True)
    # NULL fingerprints are pre-reservation legacy responses, left unchanged.
    fingerprint: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.String(64))
    owner_token: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.String(64))
    # status_code=0 is reserved work, never a replayable response.
    # Enough to rebuild the response: status plus the body.
    status_code: sa.orm.Mapped[int] = sa.orm.mapped_column(sa.Integer, nullable=False)
    response_body: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    expires_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)


class Plugin(Base):
    """A registered collector's identity and lifecycle state (P2, ADR 0017).

    Fact source: backed up. One row per collector per tenant; the registry
    upserts manifests at startup, and `:enable`/`:disable` flip `status`.
    The checksum pins the manifest content the status decision was made on.
    """

    __tablename__ = "plugin"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    # Canonical-JSON manifest with its sha256 (the registry computes both).
    manifest_json: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    checksum: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["plugin_status"]), nullable=False
    )
    trust: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["plugin_trust"]), nullable=False
    )
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)

    __table_args__ = (
        enum_check("pluginstatus", "status", ("enabled", "disabled")),
        enum_check("plugintrust", "trust", ("builtin", "third-party")),
    )


class AuditLog(Base):
    """An immutable record of one meaningful operation."""

    __tablename__ = "audit_log"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    occurred_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    actor: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    session_id: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    operation: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    target: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    # Redacted on the way in: no token, cookie, or untrusted body ever lands
    # here, since this table is retained as a fact source.
    args_redacted: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    outcome: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["audit_outcome"]), nullable=False
    )
    correlation_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(64), nullable=False
    )

    __table_args__ = (
        enum_check(
            "auditoutcome",
            "outcome",
            ("succeeded", "rejected", "failed"),
        ),
    )


class ItemEnrichment(Base):
    """One processor product for an Item: translation, summary, TTS audio.

    Products never rewrite the original Item. The unique constraint over
    (item, plugin, version, kind, lang, input_hash) is the idempotency key:
    re-processing the same content version replaces the row instead of
    accumulating copies, and a changed content_hash produces a new
    input_hash, i.e. a new product.
    """

    __tablename__ = "item_enrichment"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    item_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("item.id", ondelete="CASCADE"), nullable=False
    )
    plugin_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    plugin_version: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(32), nullable=False
    )
    kind: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(32), nullable=False)
    lang: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.String(16), nullable=True)
    payload_ref: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    input_hash: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(128), nullable=False
    )
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(16), nullable=False)
    error_class: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(32), nullable=True
    )
    detail: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    # Retry bookkeeping (transient classes requeue; MAX_ATTEMPTS bounds it).
    attempt_count: sa.orm.Mapped[int] = sa.orm.mapped_column(
        sa.Integer, nullable=False, default=0
    )
    next_attempt_at: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    # The model that actually produced the artifact (multi-model fallback).
    model: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    # Claim fencing (A10): set when a sweeper claims the row and cleared by
    # stale-claim recovery; every result write is gated on it. NULL means the
    # row is not owned by any sweeper.
    lease_expires_at: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    owner_token: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    # Chain dataflow (ADR 0030): which earlier product fed this spec's input.
    # NULL/NULL is a first-class state -- the spec read the Item's own content,
    # which is what every single-spec chain does and what every chain did
    # before the dependency existed. Set together when a later spec consumed an
    # earlier spec's product; the digest is that product's identity so a
    # revised upstream is detectable rather than silently ignored.
    upstream_plugin_id: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    upstream_digest: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    updated_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="enrichmentstatus",
        ),
        sa.UniqueConstraint(
            "item_id",
            "plugin_id",
            "plugin_version",
            "kind",
            "lang",
            "input_hash",
            name="uq_enrichment_product",
        ),
        # These three were created by migrations 0011 and 0021 and never
        # declared here, so `alembic check` reported them as indexes to *drop*
        # -- the next autogenerated migration would have removed the indexes the
        # worker claims rows with (issue #107). They are declared because they
        # are used: the sweep selects by `status`, and the chain resolver looks a
        # product up by `upstream_plugin_id` once per claimed row.
        sa.Index("ix_enrichment_item_status", "item_id", "status"),
        sa.Index("ix_enrichment_status", "status"),
        sa.Index("ix_item_enrichment_upstream", "item_id", "upstream_plugin_id"),
    )


class Work(Base):
    """One requested batch of work, and its lifecycle (ADR 0046).

    Distinct from `job`/`job_run`, which describe *scheduled collection* per
    source: a work is a caller's request ("fetch these addresses", "delete
    these items"), has no schedule, and keeps its per-target outcomes in
    `work_unit` rather than in a metrics blob -- because "why did *this* target
    fail" has to be answerable per target.
    """

    __tablename__ = "work"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    kind: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(32), nullable=False)
    # The command's input, canonical JSON. A field of its own rather than a key
    # inside params_json: it is what the work is about.
    targets_json: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.Text, nullable=False, default="[]"
    )
    # The command's parameters as canonical JSON. Never raw upstream bodies.
    params_json: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    # The command value's digest (core.work.command_fingerprint). Nullable
    # because a work created before the fingerprint existed has none -- and
    # SQL treats NULLs as distinct, so legacy rows cannot collide in the
    # unique index below.
    fingerprint: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["work_status"]), nullable=False
    )
    correlation_id: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(64), nullable=True
    )
    # A retry points back at the work it retries instead of rewriting it.
    retry_of: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.ForeignKey("work.id", ondelete="SET NULL"), nullable=True
    )
    tenant_id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), nullable=False)
    created_at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    finished_at: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )

    __table_args__ = (
        enum_check(
            "workstatus",
            "status",
            ("queued", "running", "succeeded", "partial", "failed", "cancelled"),
        ),
        # One *live* work per command value: a caller re-issuing the same
        # command while the first is still queued or running gets that work
        # back instead of a second batch. Deliberately partial: once a work is
        # terminal, submitting the same command again is a new request (the
        # caller may genuinely want a fresh run), and a plain unique constraint
        # would forbid it forever. The predicate mirrors
        # `uq_job_run_active_per_job`.
        sa.Index(
            "uq_work_live_fingerprint",
            "fingerprint",
            unique=True,
            sqlite_where=sa.text("status IN ('queued', 'running')"),
            postgresql_where=sa.text("status IN ('queued', 'running')"),
        ),
    )


class WorkUnit(Base):
    """One target's outcome inside a work: the ledger row.

    The composite primary key is the whole idea: one row per target per work,
    so replaying a target cannot add a second row -- and the database, not a
    caller's SELECT-then-INSERT, is what refuses it. `target` is NOT NULL
    because the row exists to answer "what happened to *this*"; `error_class`
    uses the one taxonomy, so a class cannot be invented here.
    """

    __tablename__ = "work_unit"

    work_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("work.id", ondelete="CASCADE"), primary_key=True
    )
    target: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, primary_key=True)
    outcome: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["unit_outcome"]), nullable=False
    )
    error_class: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["error_class"]), nullable=True
    )
    detail: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.Text, nullable=False, default=""
    )
    # What the unit produced, when it produced something (an item id, a new
    # work id for a retry). Opaque here on purpose: each kind knows its own.
    ref: sa.orm.Mapped[str | None] = sa.orm.mapped_column(sa.Text, nullable=True)
    at: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)

    __table_args__ = (
        enum_check("unitoutcome", "outcome", ("ok", "skipped", "rejected", "failed")),
        enum_check(
            "errorclass",
            "error_class",
            ("network", "parse", "structure_changed", "blocked", "auth"),
        ),
        # The sweep reads "what is still queued" and the retry-failed path
        # reads "what failed", both filtered by work.
        sa.Index("ix_work_unit_work_outcome", "work_id", "outcome"),
    )
