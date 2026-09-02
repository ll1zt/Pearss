import os
import pathlib
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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
    )

    with context.begin_transaction():
        context.run_migrations()


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
