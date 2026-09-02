"""SQLAlchemy ORM models.

Persistence lives in adapters/, not core/: core owns what an Item *means*,
this module owns how it is stored. The enum columns reuse the core enums
directly so the database, the API and the MCP tool schema cannot drift apart.

Every table here mirrors docs/domain/data-model.md. The P2/P3 tables
(ItemEnrichment, ReadState, Plugin) are deliberately absent — they arrive with
their own migration rather than being pre-created empty.
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
    canonical_url: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.Text, nullable=True
    )
    # Retained in P1 even though the plugin registry arrives in P2, so a web
    # source can record which collector parsed it from the start.
    parser_plugin_id: sa.orm.Mapped[str | None] = sa.orm.mapped_column(
        sa.String(128), nullable=True
    )
    # Per-source scheduling: fixed interval or adaptive bounds, plus the
    # consecutive-failure count that drives backoff.
    schedule_policy: sa.orm.Mapped[str] = sa.orm.mapped_column(sa.Text, nullable=False)
    status: sa.orm.Mapped[str] = sa.orm.mapped_column(
        sa.String(ENUM_LENGTHS["source_status"]), nullable=False
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
