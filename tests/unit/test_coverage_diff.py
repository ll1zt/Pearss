"""The coverage-diff gate: its diff parsing and its verdict.

`just coverage-diff` implements the rule that a change's *own* lines must be
exercised, which testing.md gives as the answer to "what does the coverage
number mean". The gate is only as trustworthy as its two halves, so both are
tested here rather than being taken on faith:

  - parsing: which statements a diff is considered to have added. Getting this
    wrong in the permissive direction (counting pre-existing lines) would
    attribute an untouched module's gaps to the change; in the restrictive
    direction it would let a new branch pass unmeasured.
  - the verdict: the threshold comparison and its exit status, since that is
    what a commit hook reads.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
from pathlib import Path

import pytest
from coverage import CoverageData
from scripts.coverage_diff import changed_lines, main

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_a_hunk_without_a_count_means_one_line() -> None:
    """`@@ -1 +1 @@` is the one-line form; omitting the count must not mean zero.

    A parser that required `,N` would silently skip every single-line hunk,
    which is most of a small change -- the gate would report "nothing to check"
    on exactly the commits it exists for.
    """
    # The parser is exercised through a real diff, built in a scratch repo so
    # the assertion is about git's output rather than a hand-written fixture
    # that could drift from it.
    with _scratch_repo() as repo:
        _commit_file(repo, "src/mod.py", "a = 1\n")
        _commit_file(repo, "src/mod.py", "a = 1\nb = 2\n")

        added = _added_lines(repo)

    assert ("src/mod.py", 2) in added


def test_every_line_of_a_multi_line_hunk_is_reported() -> None:
    """Adding three lines must yield three candidates, not one.

    The `+c,d` count is the whole point of using the diff: a change that adds a
    function body adds many statements, and only checking the first would leave
    the rest unmeasured.
    """
    with _scratch_repo() as repo:
        _commit_file(repo, "src/mod.py", "a = 1\n")
        _commit_file(repo, "src/mod.py", "a = 1\nb = 2\nc = 3\nd = 4\n")

        added = _added_lines(repo)

    assert {2, 3, 4} <= {lineno for _, lineno in added}


def test_only_added_lines_are_reported_not_the_whole_file() -> None:
    """A one-line change must not inherit the file's untested remainder.

    This is the property that makes the gate fair: a change to one line is
    responsible for that line, not for a module it merely touched.
    """
    with _scratch_repo() as repo:
        _commit_file(repo, "src/mod.py", "".join(f"line{i} = {i}\n" for i in range(50)))
        _commit_file(repo, "src/mod.py", "line0 = 0\nline1 = 1\nCHANGED = True\n")

        added = _added_lines(repo)

    assert added == [("src/mod.py", 3)], added


def test_a_deleted_line_adds_nothing() -> None:
    """Removing code cannot introduce uncovered code."""
    with _scratch_repo() as repo:
        _commit_file(repo, "src/mod.py", "a = 1\nb = 2\n")
        _commit_file(repo, "src/mod.py", "a = 1\n")

        added = _added_lines(repo)

    assert added == []


def test_files_outside_the_source_layers_are_ignored() -> None:
    """Only `src/` is measured, so a docs or test change is not "uncovered"."""
    with _scratch_repo() as repo:
        _commit_file(repo, "README.md", "before\n")
        _commit_file(repo, "README.md", "before\nafter\n")

        added = _added_lines(repo)

    assert added == []


# --------------------------------------------------------------------------
# Scratch repository helpers
#
# `changed_lines` shells out to git, so its contract is git's diff format. A
# real temporary repository keeps the test honest about that format instead of
# asserting against a fixture someone transcribed by hand.
# --------------------------------------------------------------------------


class _scratch_repo:
    """A throwaway git repository with one commit, used as a diff base."""

    def __enter__(self) -> Path:
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.invalid")
        _git(root, "config", "user.name", "test")
        return root

    def __exit__(self, *exc: object) -> None:
        self._dir.cleanup()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _commit_file(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"write {relative}")


def _added_lines(root: Path) -> list[tuple[str, int]]:
    """`changed_lines` against the scratch repo's first commit.

    The function resolves paths relative to the working directory for its
    coverage lookups, but the parsing half only needs git, so it is safe to
    call with the repo as cwd.
    """
    import os

    previous = os.getcwd()
    os.chdir(root)
    try:
        # HEAD~1 is the base: the first commit is the base state.
        return changed_lines("HEAD~1")
    finally:
        os.chdir(previous)


# --------------------------------------------------------------------------
# The verdict: the threshold comparison and the exit status
#
# The parsing tests above pin which lines the gate *considers*. These pin what
# it *decides*, which is the half a commit hook reads: the exit status. A gate
# that parses perfectly and then exits 0 on a violation protects nothing, and
# one that exits non-zero when there is nothing to check blocks every commit.
#
# The measurement is written directly into a `.coverage` database rather than
# produced by instrumenting a live run: the gate reads the database, so the
# database is the honest input, and a fixture built by the instrumenter would
# test the instrumenter as much as the gate.
# --------------------------------------------------------------------------


def _write_measurement(root: Path, relative: str, executed: list[int]) -> None:
    """Record that `relative` ran exactly these line numbers, nothing else."""
    data = CoverageData(basename=str(root / ".coverage"))
    data.add_lines({str(root / relative): set(executed)})
    data.write()


def _verdict(root: Path, base: str, threshold: str = "20") -> tuple[int, str]:
    """Run the gate with `root` as the working directory.

    Both halves of the gate are CWD-relative -- `git diff` runs here and the
    default `.coverage` is read from here -- so the only honest way to exercise
    the real entry point is to be here.
    """
    previous = os.getcwd()
    os.chdir(root)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            status = main(["coverage_diff.py", base, threshold])
    finally:
        os.chdir(previous)
    return status, captured.getvalue()


def _change_one_statement(root: Path) -> None:
    """A repo whose change adds one statement, ready for a measurement.

    Two executable lines are added (a condition and its body) so a measurement
    can be crafted to miss exactly one of them -- that is the 50% case the
    threshold boundary needs, which a single added line cannot express.
    """
    _commit_file(root, "src/mod.py", "FLAG = False\n")
    _commit_file(root, "src/mod.py", "FLAG = False\nif FLAG:\n    NEVER = 1\n")


def test_uncovered_added_lines_fail_and_are_named() -> None:
    """The violation must be named, not just counted.

    "38 added statements, 12 uncovered" tells a reader that something is
    wrong; `src/mod.py:2` tells them where to look. The gate's own message
    says to add a test, so it has to say which one.
    """
    with _scratch_repo() as repo:
        _change_one_statement(repo)
        # Only the first line ran: the condition and its body are missed.
        _write_measurement(repo, "src/mod.py", [1])

        status, output = _verdict(repo, "HEAD~1", "20")

    assert status == 1, output
    assert "src/mod.py:2" in output, output


def test_coverage_exactly_at_the_threshold_passes() -> None:
    """`more than threshold` is the rule, so the threshold itself is allowed.

    The boundary is the part of a comparison that gets written wrong, and the
    difference is not cosmetic: at 20% a violation would fail a change that
    meets the documented bar, and at 19.9% it would let one through.
    """
    with _scratch_repo() as repo:
        _change_one_statement(repo)
        # Line 2 ran, line 3 did not: exactly 50% of the added statements.
        _write_measurement(repo, "src/mod.py", [1, 2])

        status, output = _verdict(repo, "HEAD~1", "50")

    assert status == 0, output
    assert "1 uncovered" in output, output


def test_coverage_just_above_the_threshold_fails() -> None:
    """The same measurement one point over must fail -- the other side."""
    with _scratch_repo() as repo:
        _change_one_statement(repo)
        _write_measurement(repo, "src/mod.py", [1, 2])

        status, output = _verdict(repo, "HEAD~1", "49")

    assert status == 1, output


def test_a_change_with_no_executable_lines_passes() -> None:
    """A comment-only or blank change has nothing to cover and must not fail.

    `checked == 0` is not a violation; it is a question that did not apply.
    Failing here would make every documentation-only commit red.
    """
    with _scratch_repo() as repo:
        _commit_file(repo, "src/mod.py", "FLAG = False\n")
        _commit_file(repo, "src/mod.py", "FLAG = False\n# only a comment\n")
        _write_measurement(repo, "src/mod.py", [1])

        status, output = _verdict(repo, "HEAD~1", "20")

    assert status == 0, output
    assert "no executable added lines" in output, output


def test_an_absent_measurement_fails_rather_than_passing_vacuously() -> None:
    """No `.coverage` means the question was not answered, not answered well.

    `just ci` runs the gate after the instrumented suite, so an absent file
    means the step that produces it did not run. Exiting 0 there would report
    a green gate for a check that never happened.
    """
    with _scratch_repo() as repo:
        _change_one_statement(repo)

        status, output = _verdict(repo, "HEAD~1", "20")

    assert status == 1, output
    assert "no measured production files" in output, output


def test_a_measurement_that_recorded_nothing_fails_too() -> None:
    """An empty database is absent evidence, not evidence of no gaps.

    The file exists, so a check that only tested `Path.exists()` would pass
    here -- and this is exactly what a run that imported but never executed
    production code leaves behind.
    """
    with _scratch_repo() as repo:
        _change_one_statement(repo)
        CoverageData(basename=str(repo / ".coverage")).write()

        status, output = _verdict(repo, "HEAD~1", "20")

    assert status == 1, output
    assert "no measured production files" in output, output


def test_an_unavailable_base_ref_fails_with_the_remedy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A base the repository does not have must name the base and the cure.

    This is not hypothetical: the gate first failed in CI because a
    single-branch shallow clone has no `origin/main` to diff against, and the
    failure was a `CalledProcessError` traceback that said nothing about
    fetching. The status is the same either way -- what changes is whether a
    reader can act on it.
    """
    with _scratch_repo() as repo:
        _change_one_statement(repo)
        _write_measurement(repo, "src/mod.py", [1])

        status, output = _verdict(repo, "origin/main", "20")

    assert status == 1, output
    assert "origin/main" in output, output
    assert "fetch" in output.lower(), output
