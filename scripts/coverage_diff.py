"""Report the coverage of the lines a change added, against a base ref.

Why this exists instead of a global `fail_under`: a percentage over the whole
repository is met most cheaply by writing tests for whatever is easiest to
reach, and it cannot tell a missing failure-path test from defensive code that
no input can reach. What actually protects new work is whether the lines a
*change* introduced were exercised -- that question has a yes/no answer, and it
is the one the testing guide asks ("what regression would make this fail?").

Usage:

    just coverage-diff [base] [threshold]

Run after a coverage-instrumented test run; this script only reads `.coverage`.
Exit status is 1 when more than `threshold` percent of the changed statements
are uncovered, so it can gate a commit or a CI job.

Included lines are the added/modified ones (the `+` side of a unified diff) in
the four source layers. Blank lines, comments and decorators inside docstrings
are skipped, because coverage has no statement for them: they are not
"uncovered", they are not code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import coverage

# The layers to measure. Matches [tool.coverage.run] source in pyproject.toml.
SOURCE_PREFIX = "src/"


# Files that live under src/ but are not production code -- the collector tests
# sit beside their fixtures, and the package __init__ files carry no statements.
def _is_production(path: str) -> bool:
    return "/test_" not in path and not Path(path).name.startswith("test_")


class UnknownBaseRef(RuntimeError):
    """The ref the gate was asked to diff against is not in this repository.

    Raised rather than letting `git`'s non-zero status escape as a
    `CalledProcessError`: the status is the same either way, but a traceback
    naming `['git', 'diff', ...]` does not tell a reader what to do about it.
    This is a real CI failure mode, not a hypothetical one -- a single-branch
    shallow clone has no `origin/main` to diff against.
    """


def changed_lines(base: str) -> list[tuple[str, int]]:
    """The added/modified (path, lineno) pairs introduced since `base`.

    Parsed from a zero-context unified diff so that only lines this branch
    actually wrote are considered -- not the whole file, which would let a
    change to one line inherit responsibility for an untested module.

    Raises `UnknownBaseRef` when `base` cannot be resolved here.
    """
    # Both noqa codes here are about executing an untrusted program. The
    # program is `git`, fixed, and the only variable is a ref name the operator
    # typed on their own command line -- there is no input path to this
    # function that its caller does not already control.
    result = subprocess.run(  # noqa: S603
        ["git", "diff", "--unified=0", f"{base}...HEAD", "--", "src/"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise UnknownBaseRef(base)
    diff = result.stdout

    pairs: list[tuple[str, int]] = []
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/") :]
            continue
        if not line.startswith("@@"):
            continue
        # `@@ -a,b +c,d @@` -- take c and d, where d defaults to 1.
        added = line.split("+", 1)[1].split(" ", 1)[0]
        start, _, count = added.partition(",")
        for offset in range(int(count or "1")):
            pairs.append((path, int(start) + offset))
    return pairs


def analysed() -> dict[str, tuple[set[int], set[int]]]:
    """{relative path: (executable statements, missed statements)}."""
    cov = coverage.Coverage()
    cov.load()
    root = os.getcwd()

    result: dict[str, tuple[set[int], set[int]]] = {}
    for filename in cov.get_data().measured_files():
        relative = os.path.relpath(filename, root)
        if not relative.startswith(SOURCE_PREFIX) or not _is_production(relative):
            continue
        try:
            # 5-tuple: (filename, statements, excluded, missing, formatted).
            _, statements, _excluded, missed, _formatted = cov.analysis2(filename)
        except coverage.exceptions.NoDataError:
            continue
        result[relative] = (set(statements), set(missed))
    return result


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "origin/main"
    threshold = float(argv[2]) if len(argv) > 2 else 20.0

    files = analysed()
    if not files:
        # Fail, never pass vacuously: a missing measurement means the question
        # was not answered, not that it was answered well. The message names
        # both entry points because the two callers differ -- CI reads the
        # coverage `just ci-covered` recorded, a developer may have run the
        # narrower `just coverage`.
        print(
            "coverage-diff: no measured production files; run `just ci-covered` "
            "(CI's measurement) or `just coverage` first"
        )
        return 1

    checked = missed = 0
    missed_lines: list[str] = []
    try:
        added = changed_lines(base)
    except UnknownBaseRef:
        # Exit 1 for the same reason as an absent measurement: the question was
        # not answered. The difference is that this one has a cure, so say it.
        print(
            f"coverage-diff: cannot diff against {base!r}: this repository has "
            "no such ref.\n"
            f"Fetch it first -- for example `git fetch origin "
            f"{base.split('/')[-1]}` -- and run the gate again.\n"
            "A shallow or single-branch checkout hits this: the gate needs the "
            "base\ncommit, not only the pushed one."
        )
        return 1

    for path, lineno in added:
        entry = files.get(path)
        if entry is None:
            continue
        statements, missed_in_file = entry
        if lineno not in statements:
            continue  # blank line, comment, or a docstring body
        checked += 1
        if lineno in missed_in_file:
            missed += 1
            missed_lines.append(f"{path}:{lineno}")

    if checked == 0:
        print(f"coverage-diff: no executable added lines against {base}")
        return 0

    percent = 100 * missed / checked
    summary = f"coverage-diff: {checked} added statements, {missed} uncovered"
    print(f"{summary} ({percent:.1f}%)")

    if percent > threshold:
        print(f"\nFAIL: more than {threshold}% of added statements are uncovered:")
        for location in missed_lines[:20]:
            print(f"  {location}")
        if len(missed_lines) > 20:
            print(f"  ... and {len(missed_lines) - 20} more")
        print(
            "\nAdd a test that reaches them. If a line genuinely cannot be "
            "reached in this\nenvironment (defensive, or an out-of-band entry "
            "point), say so in the commit."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
