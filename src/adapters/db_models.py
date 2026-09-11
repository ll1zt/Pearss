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
    # no such concept, so all three are unused by the other collectors.
    # Not derivable from `item`: a cursor is an opaque string from the origin,
    # and a gap position is not recoverable from stored rows.
    seen_frontier: sa.orm.Mapped[int | None] = sa.orm.mapped_column(
        sa.BigInteger, nullable=True
    )
    resume_cursor: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    resume_rounds: sa.orm.Mapped[int] = sa.orm.mapped_column(
        sa.Integer, nullable=False, default=0
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
            ("active", "paused", "disabled", "deleted"),
        ),
        enum_check(
            "trustlevel",
            "trust_level",
            ("standard", "trusted", "restricted"),
        ),
        # One source per canonical URL and type within a tenant.
        sa.UniqueConstraint("tenant_id", "canonical_url", "type", name="uq_source_url"),
    )


class RawFetch(Base):
    """One fetch attempt. Rebuildable diagnostic data, not backed up."""

    __tablename__ = "raw_fetch"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    source_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("source.id", ondelete="CASCADE"), nullable=False
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
    )


class Item(Base):
    """A normalised content item. Fact source: backed up."""

    __tablename__ = "item"

    id: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.String(64), primary_key=True)
    source_id: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.ForeignKey("source.id", ondelete="CASCADE"), nullable=False
    )
    # Stable across the item's life; frozen once published.
    guid: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    content_hash: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(128), nullable=False
    )
    title: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    url: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
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
    )
