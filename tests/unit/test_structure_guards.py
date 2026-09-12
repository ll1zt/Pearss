"""Structural guards: conventions that rot silently and cost hours.

These tests assert the project's own structure rather than behaviour. They are
cheap (no I/O beyond reading the repo and one migrated schema) and each one
exists because that particular drift has either happened or is a plausible
future accident:

- enum values and the DB CHECK constraints that enforce them drift apart the
  first time someone adds an enum member without a migration
- a migration's downgrade() becomes an empty stub and `just migrate-check`
  keeps passing while downgrade is a lie
- a fixture stops being referenced by any test and becomes dead corpus
- docs reference ADR numbers that do not exist (or were renumbered)
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from core.enrichment import EnrichmentStatus
from core.enums import (
    ErrorClass,
    JobRunStatus,
    PluginStatus,
    PublishStatus,
    SourceStatus,
    TrustLevel,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# enum <-> DB CHECK constraint sync
# --------------------------------------------------------------------------


def _table_ddl(url: str) -> dict[str, str]:
    """The CREATE TABLE statements of a real migrated schema."""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "select name, sql from sqlite_master"
                    " where type = 'table' and sql is not null"
                )
            ).fetchall()
        return {name: sql for name, sql in rows}
    finally:
        engine.dispose()


def _migrated_url(tmp_path: Path) -> str:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "ddl.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return url


# column -> the enum whose members the CHECK must allow and nothing else.
_ENUM_COLUMNS: dict[str, Any] = {
    "publish_status": PublishStatus,
    "status": SourceStatus,  # source.status; job_run and plugin checked below
    "trust_level": TrustLevel,
    "error_class": ErrorClass,
}

# Same-named columns on different tables hold different enums, so the lookup
# is by (table, column) where it would otherwise be ambiguous.
_TABLED_ENUM_COLUMNS: dict[tuple[str, str], Any] = {
    ("job_run", "status"): JobRunStatus,
    ("plugin", "status"): PluginStatus,
    ("item_enrichment", "status"): EnrichmentStatus,
}

_CHECK_IN = re.compile(r"\b(\w+)\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_VALUE = re.compile(r"'([^']*)'")


def test_enum_members_and_db_check_constraints_agree(tmp_path: Path) -> None:
    """Every DB-backed enum is exactly what its CHECK constraint allows.

    Checked against the *live migrated DDL*, not the migration sources: the
    schema is what enforces at runtime, and parsing DDL instead of Python
    means partial-index predicates (which intentionally name a subset) can
    never be mistaken for a column's constraint.
    """
    ddl = _table_ddl(_migrated_url(tmp_path))

    checked: set[tuple[str, str]] = set()
    for table, sql in ddl.items():
        for column, values_text in _CHECK_IN.findall(sql):
            allowed = set(_VALUE.findall(values_text))
            if not allowed:
                continue
            # Table-specific first: `status` alone is ambiguous (source,
            # job_run, plugin and item_enrichment all have one).
            enum = _TABLED_ENUM_COLUMNS.get((table, column)) or _ENUM_COLUMNS.get(
                column
            )
            if enum is None:
                continue
            checked.add((table, column))
            expected = {member.value for member in enum}
            assert allowed == expected, (
                f"{table}.{column}: the CHECK constraint allows {sorted(allowed)} "
                f"but {enum.__name__} defines {sorted(expected)}. Whichever side "
                "moved without the other is the bug -- a value the enum rejects "
                "still inserts, or a valid value the DB refuses."
            )

    missing = (set(_ENUM_COLUMNS) | {c for _, c in _TABLED_ENUM_COLUMNS}) - {
        column for _, column in checked
    }
    assert not missing, (
        f"columns {sorted(missing)} carry an enum but no CHECK constraint was "
        "found in the migrated schema; SQLite does not enforce VARCHAR enums "
        "on its own (AGENTS.md, error taxonomy)"
    )


# --------------------------------------------------------------------------
# migrations: downgrade must actually downgrade
# --------------------------------------------------------------------------


def test_every_migration_downgrades_with_real_work() -> None:
    """`downgrade()` stubs make `just migrate-check` a lie -- unless they aren't.

    The recipe proves upgrade/downgrade/upgrade round-trips, but only if each
    downgrade reverses something. The one legitimate no-op is a pure data
    migration whose deliberate non-reversal is documented (0005: widened
    timestamps stay widened, because narrower values are the bug it fixes).
    The decidable rule: an upgrade that changes the *schema* must be reversed
    by real `op.*` calls; an upgrade that only rewrites data may downgrade to
    nothing.
    """
    versions = REPO_ROOT / "migrations" / "versions"
    offenders: list[str] = []
    _SCHEMA_OPS = {
        "create_table",
        "drop_table",
        "add_column",
        "drop_column",
        "alter_column",
        "rename_table",
        "rename_column",
        "create_index",
        "drop_index",
        "create_check_constraint",
        "drop_constraint",
        "create_unique_constraint",
        "drop_unique_constraint",
        "create_foreign_key",
        "drop_foreign_key",
        "create_primary_key",
        "drop_primary_key",
    }

    def _op_calls(node: ast.AST) -> set[str]:
        return {
            n.func.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "op"
        }

    for path in sorted(versions.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        if "downgrade" not in functions:
            offenders.append(f"{path.name} (no downgrade function)")
            continue
        upgrades_schema = bool(
            _op_calls(functions.get("upgrade", ast.Module())) & _SCHEMA_OPS
        )
        downgrades_schema = bool(_op_calls(functions["downgrade"]) & _SCHEMA_OPS)
        if upgrades_schema and not downgrades_schema:
            offenders.append(path.name)

    assert not offenders, (
        f"migrations that change the schema but downgrade to nothing: {offenders}. "
        "`just migrate-check` cannot catch an unreversible step if the step "
        "pretends to reverse."
    )


# --------------------------------------------------------------------------
# fixtures: corpus that no test reads is dead corpus
# --------------------------------------------------------------------------


def test_every_collector_fixture_is_referenced_by_a_test() -> None:
    """A fixture nobody asserts against is not a regression net.

    The guide calls fixtures reviewable test corpus. When a parser changes and
    a fixture stops matching, the test referencing it must fail -- a fixture
    with no referencing test has silently stopped protecting anything.
    """
    collectors = REPO_ROOT / "src" / "plugins" / "collectors"
    orphans: list[str] = []

    for fixture in sorted(collectors.glob("*/fixtures/*")):
        if not fixture.is_file():
            continue
        collector_dir = fixture.parents[1]
        test_sources = [
            p.read_text(encoding="utf-8")
            for p in sorted(collector_dir.glob("test_*.py"))
        ]
        if fixture.name not in "\n".join(test_sources):
            orphans.append(f"{fixture.relative_to(REPO_ROOT)}")

    assert not orphans, (
        f"fixtures no test references: {orphans}. Either wire them into a test "
        "or delete them -- unreferenced corpus is not protecting anything."
    )


# --------------------------------------------------------------------------
# docs: ADR references must resolve
# --------------------------------------------------------------------------


def test_every_adr_reference_in_docs_resolves_to_a_file() -> None:
    """'ADR 0016 says ...' must mean a file that exists.

    Renumbering or splitting an ADR without updating prose leaves the docs
    pointing at nothing -- and the next contributor implements against a
    decision they cannot find.
    """
    docs_dir = REPO_ROOT / "docs"
    adr_dir = docs_dir / "adr"
    existing = {path.name.split("-")[0] for path in adr_dir.glob("*.md")}

    prose_files = [
        *docs_dir.rglob("*.md"),
        REPO_ROOT / "README.md",
        REPO_ROOT / "AGENTS.md",
    ]
    dangling: list[str] = []

    for path in prose_files:
        for match in re.finditer(
            r"\bADR[ -]?(\d{4})\b", path.read_text(encoding="utf-8"), re.IGNORECASE
        ):
            number = match.group(1)
            if number not in existing:
                dangling.append(f"{path.relative_to(REPO_ROOT)}: ADR {number}")

    assert not dangling, (
        f"ADR references that resolve to no file in docs/adr/: {dangling}"
    )


def test_every_adr_file_number_matches_its_own_id() -> None:
    """`0016-public-exposure-hardening.md` must speak of itself as 0016."""
    adr_dir = REPO_ROOT / "docs" / "adr"
    mismatched: list[str] = []

    for path in sorted(adr_dir.glob("[0-9]" * 4 + "-*.md")):
        number = path.name.split("-")[0]
        if number not in path.read_text(encoding="utf-8"):
            mismatched.append(path.name)

    assert not mismatched, (
        f"ADR files whose own number appears nowhere in their text: {mismatched}"
    )


# --------------------------------------------------------------------------
# .env.example: the deployment contract must not drift from the code
# --------------------------------------------------------------------------


def _pearss_names_in_source() -> set[str]:
    """Every PEARSS_* variable the code (or a recipe) can actually read.

    Three read forms, all of them real in this tree and each one missed by a
    naive `environ.get` scan:
      - os.environ.get("PEARSS_X", ...)
      - env_int("PEARSS_X", ...) / _configured("PEARSS_X")
      - ${PEARSS_X:-default} inside justfile recipes
    Bare prefix stems used by the sandbox env allowlist ("PEARSS_X_") are not
    variables and are excluded.
    """
    targets = [*Path("src").rglob("*.py"), Path("justfile")]
    names: set[str] = set()
    for path in targets:
        if "__pycache__" in path.parts or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        names |= set(re.findall(r'["\'](PEARSS_[A-Z0-9_]+)["\']', text))
        names |= set(re.findall(r"\$\{(PEARSS_[A-Z0-9_]+)", text))
    return {name for name in names if not name.endswith("_")}


def _pearss_names_in_template() -> set[str]:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    # Both live assignments and the commented examples count as documented.
    return set(re.findall(r"^\s*#?\s*(PEARSS_[A-Z0-9_]+)=", text, re.MULTILINE))


def test_env_example_documents_every_variable_the_code_reads() -> None:
    """A variable the code reads but the template omits is a deployment trap.

    Someone deploys from the template, the feature silently stays off (or
    fail-closes), and the cause is an env name nobody wrote down. The reverse
    is also checked: a template entry the code cannot read is a lie that
    survives every review because nothing tests it.
    """
    # Injected by the sandbox's own env allowlist, deliberately not user-set.
    runtime_injected = {"PEARSS_SANDBOXED"}

    source = _pearss_names_in_source()
    template = _pearss_names_in_template()

    undocumented = sorted(source - template - runtime_injected)
    assert not undocumented, (
        f"the code reads these but .env.example does not document them: {undocumented}"
    )

    fictional = sorted(template - source)
    assert not fictional, (
        f".env.example documents variables nothing reads: {fictional}. "
        "Either wire them up or delete the entry."
    )
