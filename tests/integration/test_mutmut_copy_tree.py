"""The tree `just mutate` builds must be one the unit suite can actually run in.

mutmut does not run the tests in this checkout. It builds `mutants/` from
`source_paths` plus `also_copy`, and runs pytest with *that* directory as the
working directory. A test that reads a repository path which is in neither list
does not surface as a missing fixture: it raises inside **stats collection**,
before a single mutant runs, and `just mutate` prints "failed to collect stats"
and mutates nothing at all.

That has now happened three times, each with a different path: `justfile`,
`.env.example`, and `.forgejo`. Every time the fix was to add a line to
`also_copy`, and the thing that was supposed to prevent the next one was
somebody remembering. This asserts the property instead of the list -- it
builds the tree from mutmut's *own* resolved configuration and runs the unit
suite inside it, so the next repository path a test reads fails here, in CI,
with the path named.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Set while the nested run executes. The nested selection is `tests/unit`, which
# does not contain this file, so it cannot recurse today -- but a guard whose
# failure mode would be unbounded recursion should not rest on that staying
# true, and the skip is one line.
_SENTINEL = "PEARSS_MUTMUT_TREE_GUARD"

# Build artifacts are not part of the tree's semantics and copying them is
# pure waste; mutmut copies them too, but it does not assert anything about
# them, so a difference here is not a fidelity gap in what is being tested.
_SKIP = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".hypothesis",
    ".ruff_cache",
    ".mypy_cache",
)


def _copy_sources() -> list[Path]:
    """What mutmut copies into `mutants/`, asked of mutmut itself.

    Read from `mutmut.configuration` rather than restated here: the library
    merges `source_paths` and `also_copy` with its own defaults (`tests/`,
    `pyproject.toml`, the lock files), and a transcription of that rule would
    drift from the thing it is supposed to describe.
    """
    from mutmut.configuration import Config

    Config.ensure_loaded()
    config = Config.get()
    return [*config.source_paths, *config.also_copy]


def _build_copy_tree(destination: Path) -> None:
    """`mutants/`, built the way mutmut builds it.

    Absent paths are skipped rather than raised on, which is mutmut's own
    behaviour (`copy_also_copy_files`) and the reason a missing entry is
    invisible: the tree is quietly incomplete and the failure lands on whichever
    test happens to read the absent path.
    """
    for relative in _copy_sources():
        source = REPO_ROOT / relative
        if not source.exists():
            continue
        target = destination / relative
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        else:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, ignore=_SKIP, dirs_exist_ok=True)


def test_the_unit_suite_runs_inside_the_tree_just_mutate_builds(
    tmp_path: Path,
) -> None:
    """Given mutmut's copy tree, When the unit suite runs in it, Then it passes.

    The mutation gate's whole value is that it reports survivors; a gate that
    dies before mutating reports nothing and looks, from the outside, exactly
    like a gate with nothing to say. So the assertion is the suite's exit
    status in the tree, not the contents of `also_copy`.
    """
    if os.environ.get(_SENTINEL):
        pytest.skip("this run is already inside the copy-tree check")

    tree = tmp_path / "mutants"
    tree.mkdir()
    _build_copy_tree(tree)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/unit",
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        env={
            **os.environ,
            _SENTINEL: "1",
            # The tree's source ahead of the checkout's. The venv carries an
            # editable install pointing at the real repository, so without this
            # the run would import `core` from the checkout and pass while
            # testing the wrong tree.
            "PYTHONPATH": str(tree / "src"),
            "PYTEST_ADDOPTS": "",
        },
    )

    assert result.returncode == 0, (
        "the unit suite does not pass inside the tree `just mutate` builds, so "
        "the mutation gate dies in stats collection and mutates nothing.\n"
        "A test that reads a repository-relative path needs that path listed "
        "in `[tool.mutmut] also_copy` (or `source_paths`).\n\n"
        + result.stdout[-4000:]
        + result.stderr[-4000:]
    )
