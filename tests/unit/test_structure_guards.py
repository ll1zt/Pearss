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
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from core.enrichment import EnrichmentStatus
from core.enums import (
    ErrorClass,
    JobRunStatus,
    PluginStatus,
    PublishStatus,
    SourceStatus,
    TrustLevel,
)
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# The suites `just ci` runs in full. `contract` is not one of them: it is a
# single, different marker level, and it runs once by construction. These are
# the levels that were being run twice -- once bare, once instrumented.
_RUN_SUITE_LEVELS = ("unit", "fixture", "integration", "e2e")


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
    """A migrated database, from the per-session template.

    Through the shared helper rather than `alembic` directly: an in-process
    `command.upgrade` reaches `migrations/env.py`'s `fileConfig`, which rebinds
    the root logger to the current `sys.stderr` for the rest of the session.
    See `tests/migrated_schema.py`.
    """
    return migrated_database(tmp_path, "ddl.db")


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
    # Neither is user-set, so neither belongs in a deployment template:
    # PEARSS_SANDBOXED is injected by the sandbox's own env allowlist, and
    # PEARSS_TEST_DNS is set by the test suite to make sandboxed children
    # resolve hermetic addresses. Documenting the latter would invite an
    # operator to set it and quietly disable the SSRF guard for every fetch.
    runtime_injected = {"PEARSS_SANDBOXED", "PEARSS_TEST_DNS"}

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


# --------------------------------------------------------------------------
# Test hygiene
#
# The rules in docs/guides/testing.md that a machine can check, checked. They
# are here rather than in a linter config because they are about the test
# files' content, which ruff does not model -- and because a rule that lives
# only in prose is a rule that gets forgotten by the next author.
#
# Each guard is deliberately narrow: it forbids a pattern that is *never*
# right in this repository, rather than trying to judge whether a particular
# test is well written. Judgement stays with review.
# --------------------------------------------------------------------------


def _test_files() -> list[Path]:
    """Every test module, excluding this one.

    This file is about test conventions, so its own docstrings quote the very
    patterns the guards below forbid (`assert_called`, `or True`). Scanning it
    would make each guard fail on its own explanation.
    """
    return sorted(
        path
        for path in [*Path("tests").rglob("*.py"), *Path("src").rglob("test_*.py")]
        if "__pycache__" not in path.parts and path != Path(__file__)
    )


def _test_bodies(path: Path) -> str:
    """The file's source, so a check can look at what the assertions say."""
    return path.read_text(encoding="utf-8")


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """The file's lines that are code, not comments or docstrings.

    A rule about what a test *does* must not fire on prose that describes it.
    Parsing with `ast` and keeping only lines that carry statements would be
    stricter, but a comment quoting an anti-pattern would still slip through;
    skipping `#`-comments and lines inside triple-quoted strings is what these
    guards actually need.
    """
    kept: list[tuple[int, str]] = []
    in_docstring = False
    for lineno, line in enumerate(_test_bodies(path).splitlines(), start=1):
        stripped = line.strip()
        # Toggle on triple quotes; a line opening and closing one is not a
        # docstring body. Good enough for finding code, and deliberately not a
        # parser -- the guards below are narrow enough to tolerate that.
        if stripped.count('"""') % 2 == 1:
            in_docstring = not in_docstring
            continue
        if in_docstring or stripped.startswith("#"):
            continue
        kept.append((lineno, line))
    return kept


def test_no_test_asserts_on_mock_call_counts() -> None:
    """`assert_called*` / `call_count` assert implementation, not behaviour.

    A call count breaks on any refactor that keeps the behaviour -- which is
    the definition of a test coupled to the implementation. The assertions
    that matter are on the result: the response, the row, the rendered feed.
    """
    pattern = re.compile(r"\bassert_called|\bcall_count\b|\bassert_awaited")
    offenders = [
        f"{path}:{lineno}: {line.strip()}"
        for path in _test_files()
        for lineno, line in _code_lines(path)
        if pattern.search(line)
    ]

    assert not offenders, (
        "assert on the behaviour, not on how many times something was called:\n"
        + "\n".join(offenders)
    )


def test_no_test_mocks_a_module_in_this_repository() -> None:
    """`unittest.mock` is for external systems that cannot be run for real.

    Mocking a module in this repository tests the mock rather than the code.
    The suites use real SQLite, real HTTP and a real MCP client instead, so
    there is no legitimate use to protect here -- if one appears, this guard
    should be reconsidered rather than merely silenced.
    """
    forbidden = re.compile(
        r"^\s*(from unittest import mock"
        r"|from unittest\.mock import"
        r"|import unittest\.mock)"
    )
    offenders = [
        f"{path}:{lineno}: {line.strip()}"
        for path in _test_files()
        for lineno, line in _code_lines(path)
        if forbidden.match(line)
    ]

    assert not offenders, (
        "these suites do not mock; use a real instance:\n" + "\n".join(offenders)
    )


def test_no_test_sleeps_unboundedly() -> None:
    """A bare `sleep` is a race the suite hopes will resolve.

    Waiting for a server to accept connections is legitimate, but it must be
    bounded polling that fails loudly -- a fixed sleep either wastes time or
    flakes, and it hides the real synchronisation requirement. The allowed
    form is a `time.sleep` inside a bounded loop; this guard finds sleeps that
    are not.

    Two shapes are deliberately not flagged: a sleep that is part of a string
    handed to a subprocess (there it is the payload under test, not the suite
    waiting), and a sleep whose duration is a parameter of a threaded worker --
    that is an interleaving control, where the test *wants* a specific overlap
    and asserts the outcome of it rather than waiting for something to become
    ready. Both carry a comment saying which they are.
    """
    offenders: list[str] = []
    for path in _test_files():
        numbered = _code_lines(path)
        for index, (lineno, line) in enumerate(numbered):
            if not re.search(r"^\s*(?:time|asyncio)\.sleep\(", line):
                continue
            if re.search(r"[\"'].*time\.sleep", line):
                continue  # a subprocess payload, not the suite waiting
            if "widen" in line or "time.sleep(delay)" in line:
                continue  # an interleaving control, labelled as one
            window = [text for _, text in numbered[max(0, index - 10) : index]]
            bounded = any(
                re.search(r"for _ in range\(|while ", text) for text in window
            )
            if not bounded:
                offenders.append(f"{path}:{lineno}: {line.strip()}")

    assert not offenders, (
        "bound the wait with a polling loop that fails loudly:\n" + "\n".join(offenders)
    )


def test_no_test_assertion_is_vacuously_true() -> None:
    """`or True` makes an assertion incapable of failing.

    Seen in this repository: an assertion ending in `or True`. It reads as a
    check and is a no-op, which is worse than having no assertion because it
    looks like coverage.
    """
    pattern = re.compile(r"\bassert\b.*\bor\s+True\b")
    offenders = [
        f"{path}:{lineno}: {line.strip()}"
        for path in _test_files()
        for lineno, line in _code_lines(path)
        if pattern.search(line)
    ]

    assert not offenders, "this assertion can never fail:\n" + "\n".join(offenders)


def _collect_levels(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and owned helper
        [
            sys.executable,
            str(REPO_ROOT / "tests/marker_guard.py"),
            "--collect-only",
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "-q",
            *(
                [str(REPO_ROOT / "tests"), str(REPO_ROOT / "src")]
                if path == REPO_ROOT
                else [str(path)]
            ),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )


def test_every_test_module_declares_a_marker() -> None:
    """User running marked suites must not silently lose any collected test."""
    result = _collect_levels(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("source", "expected", "missing"),
    [
        (
            "@pytest.mark.unit\n@pytest.mark.parametrize('x', [1])\n"
            "def test_marked(x): pass\ndef test_missing(): pass\n",
            4,
            "test_missing",
        ),
        ("@pytest.mark.asyncio\nasync def test_async(): pass\n", 4, "test_async"),
        (
            "@pytest.mark.parametrize('x', [1, 2])\ndef test_parameter(x): pass\n",
            4,
            "test_parameter",
        ),
        ("class TestUnmarked:\n    def test_method(self): pass\n", 4, "test_method"),
        (
            "pytestmark = pytest.mark.unit\n"
            "class TestInherited:\n    def test_method(self): pass\n",
            0,
            "",
        ),
        (
            "@pytest.mark.integration\nclass TestInherited:\n"
            "    def test_method(self): pass\n",
            0,
            "",
        ),
        (
            "pytestmark = pytest.mark.asyncio\nasync def test_nonlevel(): pass\n",
            4,
            "test_nonlevel",
        ),
    ],
)
def test_user_gets_effective_execution_level_coverage(
    tmp_path: Path, source: str, expected: int, missing: str
) -> None:
    """Given real pytest modules, inherited levels count; unrelated marks do not."""
    module = tmp_path / "test_levels.py"
    module.write_text("import pytest\n" + source, encoding="utf-8")
    result = _collect_levels(module)
    assert result.returncode == expected, result.stdout + result.stderr
    if missing:
        assert "missing execution-level marker:" in result.stderr
        assert missing in result.stderr


def test_user_required_suite_fails_when_no_tests_are_selected(tmp_path: Path) -> None:
    """Given a valid marked test, selecting an empty suite must fail in just."""
    module = tmp_path / "test_selected.py"
    module.write_text(
        "import pytest\n@pytest.mark.unit\ndef test_present(): pass\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - fixed recipe and generated fixture
        ["just", "--justfile", str(REPO_ROOT / "justfile"), "_test", "e2e"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            **os.environ,
            "PYTEST_ADDOPTS": f"-c {REPO_ROOT / 'pyproject.toml'} {module}",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "code 5" in result.stderr


# --------------------------------------------------------------------------
# The publish gate has one definition
#
# `is_visible` was written as "the publish gate, made executable" and has never
# had a production caller: the exclusion happens in SQL, at five read sites,
# and it has to -- filtering after the fetch would let a pending row reach
# memory where a later change could render it (see `items_ready_for_feed`).
# So the Python predicate is not the mechanism, and ADR 0022's description of
# it as one is what prompted this guard.
#
# The invariant itself held: all five copies agreed, and the E2E test asserts
# pending is absent from every read surface. What did not hold was "one place".
# The fix is `visible_item_rows()`, the same rule as a SQL predicate, adopted
# at every read site -- and this guard is what keeps it that way. A rule
# written five times is a rule that drifts once and leaks a pending item into
# a cache that cannot be corrected.
# --------------------------------------------------------------------------


def _gate_sites() -> list[tuple[str, str]]:
    """Every production line that is part of the pending exclusion.

    Two shapes count: the predicate written out (`publish_status` compared
    against a PENDING value), and a call to the shared helper. The first is
    what must not spread; the second is what replaces it.
    """
    sites: list[tuple[str, str]] = []
    for path in Path("src").rglob("*.py"):
        if "__pycache__" in path.parts or path.name.startswith("test_"):
            continue
        for line in _test_bodies(path).splitlines():
            is_inline = "PENDING" in line and "publish_status" in line
            if is_inline or "visible_item_rows(" in line:
                sites.append((str(path), line.strip()))
    return sites


def _visibility_functions() -> set[str]:
    """Names of the functions that select rows a client may see.

    Scoping by function rather than by module is what makes the guard mean
    "these reads must share the gate". `feed.py` also holds `promote_pending`,
    whose PENDING comparisons are an in-memory state check on a loaded `Item`
    and a compare-and-swap guard on one row -- a different question from "may a
    client see this", and deliberately not routed through the helper.
    """
    return {
        "items_ready_for_feed",
        "visible_items",  # adapters/rss_out
        "count_items",
        "list_items",
        "stream_item_ids",
        "stream_contents",
    }


def _lines_in_function(path: Path, name: str) -> list[str]:
    """The source lines of one function, by indentation.

    Not a parser: it starts at `def name(` and stops at the next line that
    begins in column zero. Enough to scope a text check, and the guard's own
    tests keep it honest.
    """
    lines = _test_bodies(path).splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(f"def {name}("):
            inside = True
            continue
        if inside and line and not line[0].isspace() and not line.startswith(")"):
            break
        if inside:
            out.append(line)
    return out


def test_the_pending_exclusion_is_written_once() -> None:
    """The visibility rule has one definition, not one per read site.

    Each read that selects items for a client must go through the shared
    predicate; a copy is a rule that drifts the first time one site is edited
    and another is not, and the failure it produces -- a pending item in a
    client's cache -- cannot be corrected after the fact.
    """
    offenders: list[str] = []
    for path in [
        Path("src/adapters/repository/feed.py"),
        Path("src/adapters/repository/items_query.py"),
        Path("src/adapters/repository/reader.py"),
        Path("src/adapters/rss_out.py"),
    ]:
        for name in _visibility_functions():
            body = _lines_in_function(path, name)
            if not body:
                continue
            for line in body:
                if (
                    "PENDING" in line
                    and "publish_status" in line
                    and "visible_item_rows(" not in line
                ):
                    offenders.append(f"{path}:{name}: {line.strip()}")

    assert not offenders, (
        "these visibility reads restate the pending predicate instead of "
        "calling visible_item_rows():\n" + "\n".join(offenders)
    )


def test_the_gate_helper_is_what_the_visibility_reads_use() -> None:
    """The helper must be wired into the reads, not merely defined.

    A helper nobody calls is how `is_visible` became a documented gate with no
    call site. Checking the uses keeps the definition and its application from
    drifting apart in the other direction.
    """
    used = {
        str(path)
        for path in Path("src").rglob("*.py")
        if "__pycache__" not in path.parts
        and not path.name.startswith("test_")
        and "visible_item_rows()" in _test_bodies(path)
    }

    missing = sorted(
        {
            "src/adapters/repository/feed.py",
            "src/adapters/repository/items_query.py",
            "src/adapters/repository/reader.py",
        }
        - used
    )
    assert not missing, f"these visibility reads ignore the shared gate: {missing}"


def test_the_ci_workflow_has_one_trigger_source() -> None:
    """The CI workflow must not listen to both `push` and `pull_request`.

    It used to, and every push to a branch with an open PR produced two
    identical full runs — lint, unit, integration, e2e, contract, coverage,
    ~45 minutes each, on a shared runner (issue #40). They also raced: the
    concurrent pair was what made the MCP suites' hardcoded ports collide and
    hang a job for 51 minutes (issue #37).

    The invariant is mechanical, so it is checked mechanically: whoever adds
    `push:` back gets a red test with the reason attached, instead of a
    doubled runner bill nobody notices.

    Read through `_workflow_triggers`, not by scanning indentation: the first
    version of this guard only understood the block form, so a legitimate
    `on: [pull_request]` failed it with "found triggers: []" while
    `on: [push, pull_request]` -- the shape this guard exists to reject -- was
    read as no triggers at all. Both directions were wrong.
    """
    triggers = _workflow_triggers(
        (REPO_ROOT / ".forgejo/workflows/ci.yml").read_text(encoding="utf-8")
    )

    assert "pull_request" in triggers, (
        f"CI must gate pull requests; found triggers: {triggers}"
    )
    assert "push" not in triggers, (
        "CI listens to both push and pull_request, so every push to a branch "
        "with an open PR burns two identical full runs (issue #40). Keep "
        "`pull_request` (it gates the PR head, and pushing fires "
        "`synchronize`) and use `workflow_dispatch` for anything else."
    )


def _workflow_triggers(workflow: str) -> list[str]:
    """The trigger keys of a workflow's top-level `on:` value.

    `on:` has four shapes and a text scan gets three of them wrong:
    a block mapping (the usual form), a flow sequence (`on: [push]`), a flow
    mapping (`on: {push: null}`), and a bare scalar (`on: push`). Parsing is
    cheap here -- the value is a key list, not a nested document -- so the
    guard reads whichever shape the file uses rather than the one it expects.
    """
    key = re.compile(r'^("?on"?)\s*:\s*(.*)$')
    lines = workflow.splitlines()

    for index, line in enumerate(lines):
        if line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        match = key.match(line)
        if match is None:
            continue

        inline = match.group(2).strip()
        if inline.startswith("["):  # flow sequence
            return [part.strip().strip("\"'") for part in _split_flow(inline)]
        if inline.startswith("{"):  # flow mapping: keys, not values
            return [
                part.split(":", 1)[0].strip().strip("\"'")
                for part in _split_flow(inline)
            ]
        if inline:  # bare scalar
            return [inline.strip("\"'")]

        # Block mapping. Direct children share one indent; a child's own
        # parameters sit deeper (`    branches-ignore:` / `      - main`).
        children: list[str] = []
        child_indent: int | None = None
        for following in lines[index + 1 :]:
            if not following.strip() or following.lstrip().startswith("#"):
                continue
            if not following[:1].isspace():
                break  # the next top-level key ends the value
            depth = len(following) - len(following.lstrip())
            child_indent = depth if child_indent is None else child_indent
            if depth != child_indent:
                continue
            children.append(following.strip().split(":", 1)[0].strip().strip("\"'"))
        return children

    return []


def _split_flow(flow: str) -> list[str]:
    """Top-level comma-separated items of a flow collection, brackets removed."""
    return [part for part in flow.strip().strip("[]{} ").split(",") if part.strip()]


@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        # Block mapping: the form the tree uses.
        (
            "on:\n  pull_request:\n  workflow_dispatch:\n",
            ["pull_request", "workflow_dispatch"],
        ),
        # Block mapping with a trigger's own parameters nested below.
        ("on:\n  push:\n    branches-ignore:\n      - main\n", ["push"]),
        # Flow sequence, which the indentation scan read as no triggers at all.
        (
            "on: [pull_request, workflow_dispatch]\n",
            ["pull_request", "workflow_dispatch"],
        ),
        ("on: [push, pull_request]\n", ["push", "pull_request"]),
        # Flow mapping: the keys, not the values.
        ("on: {push: null, pull_request: null}\n", ["push", "pull_request"]),
        # Bare scalar.
        ("on: push\n", ["push"]),
        # Quoted key and values.
        ('"on": ["push", "pull_request"]\n', ["push", "pull_request"]),
    ],
)
def test_workflow_trigger_shapes_are_all_read(
    workflow: str, expected: list[str]
) -> None:
    """Each shape `on:` can take is read, so the guard is not shape-blind.

    A guard that only understands the author's favourite YAML spelling does not
    protect the invariant: it fails on a legitimate file, or -- worse -- reads a
    forbidden trigger as nothing and passes.
    """
    assert _workflow_triggers(workflow) == expected


def test_the_ci_aggregate_runs_the_suites_once() -> None:
    """`just ci` must not run the full suite bare *and* instrumented.

    It did, so the same tests ran twice. That was waste rather than the cause
    of the 60-minute failure -- the double-run form passed in 10m15s (run #94)
    and 48m5s (#452), and what actually grew is integration's per-test cost
    (issue #44). The gate reads the measurement instead, because one
    instrumented run answers every question the bare runs did.

    Checked by counting the full-suite selections `just ci` would actually
    execute, not by reading the `ci:` line's text. `coverage-diff` also runs
    the whole instrumented suite, and the pytest command can be written into a
    recipe body rather than named as a dependency; an earlier version of this
    guard called both of those fine.
    """
    selections = _marker_selections("ci")

    # Count how many pytest invocations select each suite. The suite is "run
    # twice" precisely when every marker is selected twice, which is what both
    # of the redundant forms look like from outside -- whether the second run
    # arrives as `coverage-gate` re-invoking pytest or as the same command
    # written into a recipe body.
    counts: Counter[str] = Counter()
    for _command, selection in selections:
        for level in _RUN_SUITE_LEVELS:
            if re.search(rf"\b{level}\b", selection):
                counts[level] += 1

    redundant = {level: n for level, n in counts.items() if n > 1}
    assert not redundant, (
        f"`just ci` selects these suites more than once: {redundant}.\n"
        f"Every pytest invocation it runs: {selections}\n"
        "Run the suites once (`ci-covered`, instrumented) and let "
        "`coverage-gate` read that .coverage instead of repeating it "
        "(issues #42, #46)."
    )
    assert counts, "`just ci` runs no suites at all; the guard lost its subject"


def _marker_selections(recipe: str) -> list[tuple[str, str]]:
    """(command, `-m` selection) for every pytest call `just <recipe>` runs.

    Includes the `contract` call; the caller counts only the levels in
    `_RUN_SUITE_LEVELS`, so a contract run is reported but never counted as a
    duplicate of the full suites.
    """
    found: list[tuple[str, str]] = []
    for line in _expand_recipe(recipe):
        try:
            tokens = shlex.split(line)
        except ValueError:  # unbalanced quoting: not a command we can read
            continue
        if "pytest" not in tokens:
            continue
        selection = tokens[tokens.index("-m") + 1] if "-m" in tokens else ""
        found.append((line, selection))
    return found


def _expand_recipe(recipe: str) -> list[str]:
    """Every leaf command `just <recipe>` would run, with indirection resolved.

    Two things make a textual reading of the justfile wrong here. `just` prints
    a nested recipe invocation as `just <recipe> ...` rather than inlining it
    (that is what `_test` is), so the commands actually executed are one hop
    away. And a recipe body is as valid a place for a pytest call as a
    dependency list is -- the first version of this guard only looked at the
    `ci:` line's tokens and missed both.
    """

    def walk(args: list[str], depth: int) -> list[str]:
        if depth > 6:  # a cycle; `just` would have failed long before this
            return []
        result = subprocess.run(  # noqa: S603 - fixed binary, owned justfile
            ["just", "--justfile", str(REPO_ROOT / "justfile"), "--dry-run", *args],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "PYTEST_ADDOPTS": ""},
        )
        assert result.returncode == 0, result.stderr

        commands: list[str] = []
        # Join backslash continuations first: one command may span several
        # lines, and a `-m` argument can sit on the continuation.
        for raw in result.stderr.replace("\\\n", " ").splitlines():
            line = raw.strip()
            if not line:
                continue
            tokens = shlex.split(line)
            if tokens and tokens[0] == "just":
                commands.extend(walk(tokens[1:], depth + 1))
            else:
                commands.append(line)
        return commands

    return walk([recipe], 0)


def test_the_ci_workflow_cancels_superseded_runs_explicitly() -> None:
    """CI must state its concurrency group, not inherit one.

    The trigger guard above keeps `push` out so a revision gets one run. That
    was half the property: the other half is what happens when a *second*
    revision arrives while the first is still running. The workflow used to say
    Forgejo's default "cancels the superseded run instead of queueing behind
    it", which is true of recent versions -- and was false twice: cancellation
    did not apply to `pull_request` workflows at all (forgejo#9797), and the
    group only matched runs still pending, so a run whose job had already failed
    kept running (forgejo#10863). Both are the kind of default that changes
    under you.

    It matters here because the runner is shared and `capacity` is 5: two runs
    of this suite compete for the same 14 cores. #458 and #459 were both killed
    at the 60-minute cap while a different run of the same suite was executing
    on the host (issue #44, #47).

    Read from the workflow rather than from memory, and keyed on the behaviour
    (`cancel-in-progress: true` inside a `concurrency:` block) rather than on a
    particular spelling of the group expression.
    """
    workflow = (REPO_ROOT / ".forgejo/workflows/ci.yml").read_text(encoding="utf-8")
    lines = workflow.splitlines()

    start = next(
        (i for i, line in enumerate(lines) if line.startswith("concurrency:")), None
    )
    assert start is not None, (
        "the CI workflow has no `concurrency:` block, so a superseded run is "
        "cancelled only if the server's default still does that -- and it has "
        "regressed twice (forgejo#9797, forgejo#10863). State the group "
        "(issues #40, #47)."
    )

    block: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break  # the next top-level key ends the block
        block.append(line.strip())

    # `cancel-in-progress` is what actually cancels; `group` without it only
    # serialises, which on a shared runner is worse than the default.
    assert any(
        entry.replace(" ", "").startswith("cancel-in-progress:true") for entry in block
    ), (
        "the CI `concurrency:` block does not set `cancel-in-progress: true`, so "
        f"a superseded run waits for the previous one instead of replacing it: {block}"
    )
    assert any(entry.startswith("group:") for entry in block), (
        f"the CI `concurrency:` block has no `group:`, so it groups nothing: {block}"
    )


def test_the_documented_ci_entry_point_is_what_the_workflow_runs() -> None:
    """`just ci-nix` calls itself "the exact command CI executes". Check it.

    Three documents and the architecture spec's §7.6 describe CI as
    `nix flake check` then `nix develop --command just ci`. The workflow ran
    only the second, so the flake gate never ran in CI at all, and `ci-nix`
    -- the recipe that exists to reproduce CI locally -- had no caller. A
    documented gate that is not wired up is worse than an undocumented one:
    readers trust it.

    The comparison is mechanical, so it is made mechanically: expand the
    documented entry point with `just --dry-run` and require each command it
    would run to appear in the workflow. Adding a step to one side and not the
    other now fails here, which is the drift this pins.
    """
    expanded = subprocess.run(  # noqa: S603 - fixed binary, owned justfile
        ["just", "--justfile", str(REPO_ROOT / "justfile"), "--dry-run", "ci-nix"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
    )
    assert expanded.returncode == 0, expanded.stderr
    # `just --dry-run` prints the commands it would run to stderr, and the
    # recipes it would invoke to stdout. The commands are the subject here.
    documented = [
        line.strip()
        for line in expanded.stderr.splitlines()
        if line.strip() and not line.startswith("just ")
    ]

    workflow = (REPO_ROOT / ".forgejo/workflows/ci.yml").read_text(encoding="utf-8")
    missing = [command for command in documented if command not in workflow]

    assert documented, "`just ci-nix` expands to nothing; the guard lost its subject"
    assert not missing, (
        "`just ci-nix` claims to be the exact command CI executes, but the "
        f"workflow does not run: {missing}\n"
        "Either add the step to .forgejo/workflows/ci.yml or stop describing "
        "`ci-nix` as what CI executes -- a documented gate nobody runs is a "
        "gate readers trust and do not get."
    )


def test_coverage_gate_reads_its_base_and_threshold_from_the_environment() -> None:
    """The recipe claims a merge workflow can pin these without editing it.

    It claimed that while taking plain positional defaults, so
    `COVERAGE_BASE=... just coverage-gate` quietly used `origin/main` -- a gate
    described as configurable that ignored its configuration. B06 asks for the
    ability explicitly, so it is pinned by behaviour: expand the recipe with
    the variables set and read what it would actually run.
    """

    def expanded(**env: str) -> str:
        # Strip any COVERAGE_* the caller's shell exported, so the default case
        # measures the recipe rather than the environment running the suite.
        base = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
        command = ["just", "--justfile", str(REPO_ROOT / "justfile")]
        command += ["--dry-run", "coverage-gate"]
        result = subprocess.run(  # noqa: S603 - fixed binary, owned justfile
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**base, **env, "PYTEST_ADDOPTS": ""},
        )
        assert result.returncode == 0, result.stderr
        return result.stderr

    assert 'coverage_diff.py "origin/main" "20"' in expanded(), (
        "the coverage gate's defaults changed; the pinned default is origin/main "
        "at 20% and several documents quote it"
    )
    assert 'coverage_diff.py "HEAD~5" "99"' in expanded(
        COVERAGE_BASE="HEAD~5", COVERAGE_THRESHOLD="99"
    ), (
        "COVERAGE_BASE and COVERAGE_THRESHOLD are not wired into the recipe, so "
        "a merge workflow cannot pin them without editing the justfile (B06)"
    )
