"""The application module must be importable without side effects (issue #21).

`import app.main` used to build the engine, create the database directory,
and wire the full middleware stack as an import side effect: tools, tests,
and future workers paid that cost on every import, and any move to
multi-process or per-tenant deployment multiplies the surprise. The module
now builds the ASGI app lazily on first attribute access (PEP 562), which
is the only contract uvicorn's `app.main:app` string form needs.

Each check runs in a fresh interpreter: the side effect is module import
itself, so an in-process test could not observe a clean state (the module
may already be imported by the suite's own fixtures).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Importing fastapi and the app stack takes a moment; give the subprocess
# room without ever waiting on a hang.
_IMPORT_TIMEOUT = 120


@pytest.mark.unit
def test_importing_app_main_constructs_no_application() -> None:
    """Plain `import app.main` must build nothing.

    Given a fresh interpreter
    When the module is imported and its namespace inspected
    Then no `app` attribute exists yet -- no engine, no middleware stack,
    no database directory created as a price of admission.
    """
    program = (
        "import app.main\nimport sys\nsys.exit(1 if 'app' in vars(app.main) else 0)\n"
    )
    proc = subprocess.run(  # noqa: S603 — fixed test code, no untrusted input
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=_IMPORT_TIMEOUT,
    )
    assert proc.returncode == 0, (
        "importing app.main must not construct the application\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr[-2000:]}"
    )


@pytest.mark.unit
def test_the_uvicorn_attribute_contract_still_builds_the_app() -> None:
    """`app.main.app` must still yield a callable ASGI application.

    Given a fresh interpreter
    When the `app` attribute is accessed the way uvicorn's string form does
    Then the application is built on demand and is callable -- the lazy
    seam must not break the documented entry points (justfile serve,
    the plugin-restart E2E spawn).
    """
    program = (
        "import app.main\n"
        "application = app.main.app\n"
        "import sys\n"
        "sys.exit(0 if callable(application) else 1)\n"
    )
    proc = subprocess.run(  # noqa: S603 — fixed test code, no untrusted input
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=_IMPORT_TIMEOUT,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


@pytest.mark.unit
def test_the_lazy_build_happens_once_and_caches_in_the_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First attribute access builds; later reads hit the cached attribute.

    Given the module's lazy seam (issue #21)
    When `app.main.app` is read twice in-process
    Then both reads yield the same object and the namespace now holds the
    attribute -- the PEP 562 hook must not rebuild per access. This is
    also the only in-process reach into the build body, so the coverage
    gate can see the seam actually constructing.
    """
    monkeypatch.setenv("PEARSS_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path}/lazy.db")
    import app.main

    first = app.main.app
    assert callable(first)
    assert "app" in vars(app.main)
    assert app.main.app is first
