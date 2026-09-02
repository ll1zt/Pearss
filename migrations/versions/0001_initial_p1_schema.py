"""initial P1 schema

Creates the seven tables the P1 scope needs, per docs/domain/data-model.md:

    source, raw_fetch, item, job, job_run, feed_out, audit_log

The P2/P3 tables (item_enrichment, read_state, plugin) are deliberately
absent; they arrive with their own migration rather than being pre-created
empty and then altered.

Revision ID: 0001
Revises:
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Enum taxonomies are inlined here rather than imported from core.enums.
# Migrations are historical records: they must produce the same schema years
# from now even if the enum has since gained members. Importing application
# code would silently rewrite history.
_ERROR_CLASS = ("network", "parse", "structure_changed", "blocked", "auth")
_PUBLISH_STATUS = ("pending", "ready", "published")
_JOB_RUN_STATUS = ("queued", "running", "succeeded", "failed", "timed_out", "cancelled")
_SOURCE_STATUS = ("active", "paused", "disabled", "deleted")
_SOURCE_TYPE = ("rss", "web", "agent")
_AUDIT_OUTCOME = ("succeeded", "rejected", "failed")


def upgrade() -> None:
    """Create the P1 schema."""
    op.create_table(
        "source",
        sa.Column("id", sa.String(length=64), nullable=False),
        # A native enum, not a bare VARCHAR: the model declares it as an
        # enum, and autogenerate would otherwise keep reporting a type change
        # forever. The CHECK this emits is what stops an out-of-taxonomy
        # value entering the table.
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("parser_plugin_id", sa.String(length=128), nullable=True),
        sa.Column("schedule_policy", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source")),
        sa.CheckConstraint(
            f"type IN {_SOURCE_TYPE}", name=op.f("ck_source_sourcetype")
        ),
        sa.CheckConstraint(
            f"status IN {_SOURCE_STATUS}", name=op.f("ck_source_sourcestatus")
        ),
        sa.UniqueConstraint(
            "tenant_id", "canonical_url", "type", name=op.f("uq_source_url")
        ),
    )

    op.create_table(
        "raw_fetch",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("payload_ref", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("fetched_at", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name=op.f("fk_raw_fetch_source_id_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_fetch")),
        sa.CheckConstraint(
            f"error_class IN {_ERROR_CLASS}",
            name=op.f("ck_raw_fetch_errorclass"),
        ),
    )

    op.create_table(
        "item",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("guid", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Text(), nullable=True),
        sa.Column("content_ref", sa.Text(), nullable=True),
        sa.Column("publish_status", sa.String(length=16), nullable=False),
        sa.Column("published_to_feed_at", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name=op.f("fk_item_source_id_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item")),
        sa.CheckConstraint(
            f"publish_status IN {_PUBLISH_STATUS}",
            name=op.f("ck_item_publishstatus"),
        ),
        sa.UniqueConstraint("tenant_id", "guid", name=op.f("uq_item_guid")),
    )
    # Feed generation filters on (publish_status, published_at), so the gate
    # query stays indexed as the item table grows.
    op.create_index(
        "ix_item_status_published", "item", ["publish_status", "published_at"]
    )

    op.create_table(
        "job",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
    )

    op.create_table(
        "job_run",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("metrics", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("retry_of", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.id"],
            name=op.f("fk_job_run_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of"],
            ["job_run.id"],
            name=op.f("fk_job_run_retry_of_job_run"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_run")),
        sa.CheckConstraint(
            f"status IN {_JOB_RUN_STATUS}", name=op.f("ck_job_run_jobrunstatus")
        ),
        sa.CheckConstraint(
            f"error_class IN {_ERROR_CLASS}", name=op.f("ck_job_run_errorclass")
        ),
    )

    op.create_table(
        "feed_out",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("filter_query", sa.Text(), nullable=False),
        sa.Column("pipeline_chain", sa.Text(), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feed_out")),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("args_redacted", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.CheckConstraint(
            f"outcome IN {_AUDIT_OUTCOME}", name=op.f("ck_audit_log_auditoutcome")
        ),
    )


def downgrade() -> None:
    """Drop the P1 schema.

    Reverse order to upgrade so foreign keys never dangle, even on engines
    that enforce them.
    """
    op.drop_table("audit_log")
    op.drop_table("feed_out")
    op.drop_table("job_run")
    op.drop_table("job")
    op.drop_index("ix_item_status_published", table_name="item")
    op.drop_table("item")
    op.drop_table("raw_fetch")
    op.drop_table("source")
