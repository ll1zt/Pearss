"""Real filesystem races, coordinated at the OS audit boundary without mocks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.filestore import read_body

pytestmark = pytest.mark.integration

# Persistence reference: changing this value requires a data migration.
REFERENCE = "cas://2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

# Audit hooks cannot be removed, so contain the synchronization in a fresh
# interpreter. All writes and renames remain real filesystem operations.
WRITERS = """
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from adapters.filestore import store_body

root = Path(sys.argv[1])
barrier = Barrier(8, timeout=10)

def before_rename(event, args):
    if event == 'os.rename':
        barrier.wait()

sys.addaudithook(before_rename)
with ThreadPoolExecutor(max_workers=8) as pool:
    futures = [pool.submit(store_body, 'hello', root=root) for _ in range(8)]
    results = []
    for future in futures:
        try:
            results.append(future.result())
        except OSError as error:
            results.append(type(error).__name__)
print(json.dumps(results))
"""


def test_user_can_import_identical_bodies_concurrently(tmp_path: Path) -> None:
    """Given an empty store, when writes overlap, then all share exact bytes."""
    result = subprocess.run(  # noqa: S603 — fixed test code, no untrusted input
        [sys.executable, "-c", WRITERS, str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == [REFERENCE] * 8
    assert read_body(REFERENCE, root=tmp_path) == "hello"
    files = sorted(
        path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()
    )
    assert files == [Path("2c") / REFERENCE.removeprefix("cas://")]
    assert (tmp_path / files[0]).read_bytes() == b"hello"


FAILED_INSTALL = """
import sys
from pathlib import Path
from adapters.filestore import store_body

root = Path(sys.argv[1])

def obstruct_install(event, args):
    if event == 'os.rename':
        Path(args[1]).mkdir()

sys.addaudithook(obstruct_install)
try:
    store_body('hello', root=root)
except OSError:
    print('write refused')
else:
    raise AssertionError('obstructed installation succeeded')
"""


def test_user_failed_body_install_leaves_no_partial_files(tmp_path: Path) -> None:
    """Given an obstructed destination, when writing fails, then no body leaks."""
    result = subprocess.run(  # noqa: S603 — fixed test code, no untrusted input
        [sys.executable, "-c", FAILED_INSTALL, str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.stdout.strip() == "write refused"
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
