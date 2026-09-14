"""retire source.status='deleted': deletion now removes the row

`deleted` was a soft-delete marker: the row stayed in the table as a
tombstone, its items stayed published, and re-subscribing the same feed found
that row and reactivated it. Deletion now removes the source and its items
outright (ADR 0021), so there is no state left for this value to describe.

Existing rows must move somewhere legal *before* the CHECK shrinks, or the
table rebuild rejects them. They become `disabled`: the operator's intent was
"stop collecting this", and disabled preserves the subscription and its
history without asserting anything the new model cannot express. The migration
deliberately does **not** cascade-delete their items -- deleting data is a
confirmed runtime action, never an upgrade side effect.

The rebuild is written as explicit DDL rather than `batch_alter_table`. The
batch implementation reflects the table through a MetaData that already
carries this project's naming convention, so a CHECK stored as
`ck_source_sourcestatus` comes back as `ck_source_ck_source_sourcestatus` --
the prefix is applied twice. Dropping by the stored name then fails to match,
and recreating by the bare suffix would rename every CHECK in the table.
Neither is acceptable in a migration that must be re-runnable, so the table is
rebuilt from a definition written out here, which is also what the SQLite
documentation recommends for schema changes it cannot express as ALTER.

`downgrade` maps `disabled` back to `deleted`, which is lossy but legal: rows
that were genuinely disabled before this revision are indistinguishable from
converted ones afterwards. That is inherent to the downgrade -- and any rows
the API deleted cannot be restored by it, which is why deletion lives in the
API and not here.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-14

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The table as 0016 left it, minus the constraint that is changing. Column
# order is preserved because the copy below is positional.
_COLUMNS = """
    id, type, url, canonical_url, parser_plugin_id, schedule_policy, status,
    tenant_id, created_at, updated_at, trust_level, title, seen_frontier,
    floor_cursor, pipeline_chain, target_lang, folder
"""

_CREATE_SOURCE = """
CREATE TABLE source_new (
    id VARCHAR(64) NOT NULL,
    type VARCHAR(16) NOT NULL,
    url TEXT,
    canonical_url TEXT NOT NULL,
    parser_plugin_id VARCHAR(128),
    schedule_policy TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    trust_level VARCHAR(16) DEFAULT 'standard' NOT NULL,
    title TEXT,
    seen_frontier BIGINT,
    floor_cursor TEXT,
    pipeline_chain TEXT,
    target_lang VARCHAR(16),
    folder TEXT DEFAULT 'default' NOT NULL,
    CONSTRAINT pk_source PRIMARY KEY (id),
    CONSTRAINT ck_source_sourcetype CHECK (type IN ('rss', 'web', 'agent')),
    CONSTRAINT ck_source_sourcestatus CHECK (status IN ({statuses})),
    CONSTRAINT ck_source_trustlevel
        CHECK (trust_level IN ('standard', 'trusted', 'restricted')),
    CONSTRAINT uq_source_url UNIQUE (tenant_id, canonical_url, type)
)
"""

# Constraint names are written out in the form the database actually stores,
# not the bare suffix the model declares. The naming convention expands
# ck_<table>_<suffix> at DDL time, and later migrations drop these by their
# stored names -- 0003's downgrade drops `ck_source_trustlevel`. Producing the
# bare `ck_trustlevel` here would leave a table that no longer matches the rest
# of the schema's naming and would break that downgrade.


def upgrade() -> None:
    # Order matters: migrate the data first, then rebuild with the tighter
    # constraint, or the copy rejects the rows it is carrying over.
    op.execute("update source set status = 'disabled' where status = 'deleted'")

    op.execute(_CREATE_SOURCE.format(statuses="'active', 'paused', 'disabled'"))
    op.execute(f"INSERT INTO source_new ({_COLUMNS}) SELECT {_COLUMNS} FROM source")
    op.execute("DROP TABLE source")
    op.execute("ALTER TABLE source_new RENAME TO source")


def downgrade() -> None:
    op.execute("update source set status = 'deleted' where status = 'disabled'")

    op.execute(
        _CREATE_SOURCE.format(statuses="'active', 'paused', 'disabled', 'deleted'")
    )
    op.execute(f"INSERT INTO source_new ({_COLUMNS}) SELECT {_COLUMNS} FROM source")
    op.execute("DROP TABLE source")
    op.execute("ALTER TABLE source_new RENAME TO source")
