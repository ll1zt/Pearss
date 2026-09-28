import os
import pathlib
import sys
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# `disable_existing_loggers=False` is load-bearing, not a style preference.
# The default disables every logger that already exists, so a process that
# runs migrations -- the CLI, a test session, an app that configures its
# credential on first use -- silently loses all application logging for
# whatever comes next. Tests caught this only because one asserted on a
# warning, and it passed in isolation while failing in a full run.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The URL is resolved here rather than in alembic.ini so that no credentials
# live in the repository. PEARSS_DATABASE_URL is honoured in every
# environment; the default points at a gitignored local file so a fresh clone
# can migrate without any configuration.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from adapters.db_models import Base  # noqa: E402

target_metadata = Base.metadata


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return os.environ.get(
        "PEARSS_DATABASE_URL",
        "sqlite+pysqlite:///./data/pearss.db",
    )


def _ensure_parent_directory(url: str) -> None:
    """Create the parent directory for a file-backed SQLite database.

    SQLite will not create it, and `data/` is gitignored, so a fresh clone has
    no such directory: `alembic upgrade head` would fail with "unable to open
    database file" on the very first command the README tells you to run.
    Other schemes are untouched, since a Postgres URL has no local directory.
    """
    if not url.startswith("sqlite"):
        return
    path = url.split("///", 1)[-1] if "///" in url else ""
    if not path or path.startswith(":memory:"):
        return
    parent = pathlib.Path(path).expanduser().parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)


url = _database_url()
_ensure_parent_directory(url)
config.set_main_option("sqlalchemy.url", url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


# Objects SQLite creates for itself. Autogenerate compares the models against
# the database, so anything in the database that no model declares looks like
# something to drop -- and dropping it would be wrong every time:
#
#   - `sqlite_stat1`, created by the ANALYZE migration 0022 runs;
#   - the FTS5 full-text table and the five shadow tables it maintains
#     (migration 0023). A virtual table cannot be represented in metadata at all.
#
# A prefix list rather than "ignore anything unmanaged": a genuinely undeclared
# index must still fail the check, which is the whole point of running it
# (issue #107). A *new* virtual table therefore fails until it is named here,
# and that is the intended cost -- one deliberate line beats a silent drop.
_UNMANAGED_TABLE_PREFIXES = ("sqlite_", "item_fts")


def _include_object(
    object_: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Keep SQLite's own objects out of the autogenerate diff."""
    if type_ == "table" and name is not None:
        return not name.startswith(_UNMANAGED_TABLE_PREFIXES)
    return True


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            # SQLite cannot ALTER constraints; render_as_batch makes
            # table-scoped changes recreate-and-copy, which keeps migrations
            # working on both SQLite today and Postgres in P3.
            render_as_batch=connectable.dialect.name == "sqlite",
            # Report tables the models define but the database lacks, and vice
            # versa, so a model/schema drift shows up instead of silently
            # producing a no-op migration.
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
