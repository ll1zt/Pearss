"""The migrated schema, built once per session and copied per test.

Every database-backed suite -- integration, e2e, contract -- wants the same
thing: a real schema, produced by the real migrations, in a file of its own so
one test's rows cannot reach the next. What they were each doing is running
`alembic upgrade head` for every test that needed one.

That is 20 migrations per test, measured at 538 ms a call. The integration
suite makes about 350 of those calls out of 568 tests, so the same twenty
migrations were being derived over and over into hundreds of identical empty
files: ~190 s of the suite's 669 s, 28% of the wall clock, and the single
largest identifiable block in it (issue #44). It is what made the suite go from
0.26 s to 1.18 s per test, which is what put CI against its 60-minute cap.

Two properties have to survive. The schema under test is still the one
production runs -- including the CHECK constraints and CASCADE rules that
`Base.metadata.create_all` would reproduce only by someone remembering to keep
the two in sync. And every caller still gets its own file, so the
function-scoped fixtures keep the isolation they were written for; sharing one
database would be the obvious wrong fix, and `test_migrated_database_reuse.py`
pins the isolation so it cannot land.

Why the Config is built without the ini file
--------------------------------------------

`migrations/env.py` calls `logging.config.fileConfig`, which is what an Alembic
CLI is expected to do: it *replaces* the root logger's handlers, binding them to
whatever `sys.stderr` is at that instant. Under pytest that is a capture stream
which is closed when the test ends.

This was masked while every test migrated for itself -- each call rebound the
handler to its own test's stream, so a later `LOGGER.error(...)` in the same
test still landed in the right place. Deriving the schema once per session made
the binding stale for every test after the first, and the record went to a
closed stream instead: `test_worker_recovery`'s two stop-drain cases assert on
captured stderr and failed on exactly that.

`env.py` guards the call with `if config.config_file_name is not None:`, so a
`Config` built from the values directly -- rather than from `alembic.ini` --
never reaches it. That is the fix, and it is the correct one regardless of
caching: a test session has no business reconfiguring its own logging to borrow
the CLI's.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]

# Built on first use, in the interpreter that uses it. Deliberately not a
# pytest fixture: callers in three directories reach this through their own
# conftests, and several test modules call it directly, so the cache has to
# live below all of them.
_template_dir: Path | None = None
_template: Path | None = None


def build(destination: Path) -> None:
    """Run the real migrations into `destination`.

    Public because a test that needs an uncached reference -- something to
    compare the reused template against -- must build one without going
    through `migrated_database`'s cache. It is also the seam the reuse test
    counts, which is why deriving the schema is a separate function from
    caching it.

    The `Config` is built from values, not from `alembic.ini`: that is what
    keeps `env.py`'s `fileConfig` -- and its root-logger side effect -- out of
    the test process. See the module docstring.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        str(sa.URL.create("sqlite+pysqlite", database=str(destination))),
    )
    command.upgrade(config, "head")


def _schema_template() -> Path:
    """The migrated schema as a template file, migrated once per session.

    The path lives in a private temporary directory removed at interpreter
    exit, and nothing ever writes to it after it is built: callers only ever
    copy *from* it, which is what keeps it a template.
    """
    global _template, _template_dir
    if _template is None:
        _template_dir = Path(tempfile.mkdtemp(prefix="pearss-schema-"))
        atexit.register(shutil.rmtree, _template_dir, ignore_errors=True)
        _template = _template_dir / "template.db"
        build(_template)
    return _template


def migrated_database(directory: Path, filename: str) -> str:
    """A migrated database at `directory/filename`; return its URL.

    The schema is the production one -- the real migrations ran to produce the
    template this copies. The copy is the caller's alone, so tests that seed
    rows, or that need two databases, keep working unchanged.
    """
    destination = directory / filename
    destination.parent.mkdir(parents=True, exist_ok=True)

    template = _schema_template()
    shutil.copyfile(template, destination)
    # The migrations leave the database in rollback-journal mode, so there is
    # no sidecar today and a plain copyfile is complete. This guards the case
    # where that changes: under WAL the newest committed pages live in the -wal
    # file, and copying the .db alone would silently drop them.
    for suffix in ("-wal", "-shm"):
        sidecar = template.with_name(template.name + suffix)
        if sidecar.exists():
            shutil.copyfile(sidecar, destination.with_name(destination.name + suffix))

    return str(sa.URL.create("sqlite+pysqlite", database=str(destination)))
