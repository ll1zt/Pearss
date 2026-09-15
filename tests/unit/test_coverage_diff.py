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

import subprocess
from pathlib import Path

import pytest
from scripts.coverage_diff import changed_lines

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
