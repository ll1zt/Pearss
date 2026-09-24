"""Test servers must not fight over a port (issue #37).

Two CI runs for the same SHA are normal here: `push` and `pull_request` are
separate events and the workflow has no concurrency group. When the MCP suites
pinned their servers to constants (8091, 8093), the second run's server failed
to bind, its thread died, and the client waited on an endpoint that would never
answer — the job produced no output for 51 minutes and was killed at the
60-minute cap.

The fix is that ports come from the OS. These tests pin both halves: the helper
hands out distinct ports, and a server that cannot bind fails loudly instead of
leaving a caller to hang.

The port-collision scenario itself is pinned by running two servers at once,
which is exactly what two concurrent runs do.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Response

from tests.integration.conftest import ServerInThread, free_port

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _app() -> FastAPI:
    """A minimal app: this suite is about binding, not about behaviour."""
    app = FastAPI()

    @app.get("/ping")
    def ping() -> Response:
        return Response(content="pong", media_type="text/plain")

    return app


@pytest.fixture
def servers() -> Iterator[list[ServerInThread]]:
    started: list[ServerInThread] = []
    yield started
    for server in started:
        server.__exit__(None, None, None)


def test_two_servers_get_different_ports_and_both_answer(
    servers: list[ServerInThread],
) -> None:
    """Given two test servers starting at once (what two CI runs do),
    When both come up,
    Then each listens on its own OS-assigned port and answers.

    A constant port makes the second one die on bind; that is the bug.
    """
    first = ServerInThread(_app(), free_port())
    second = ServerInThread(_app(), free_port())
    first.__enter__()
    servers.append(first)
    second.__enter__()
    servers.append(second)

    assert first.port != second.port, (
        f"both servers were given port {first.port}; free_port must ask the OS"
    )

    for server in (first, second):
        response = httpx.get(server.base_url("/ping"), timeout=10.0)
        assert response.status_code == 200, server.port
        assert response.text == "pong"


def test_a_server_that_cannot_bind_raises_instead_of_hanging(
    servers: list[ServerInThread],
) -> None:
    """Given a port already taken by another server,
    When a second server tries to bind it,
    Then construction raises — the caller is told, not left waiting.

    The 51-minute hang was exactly this: uvicorn died on its thread, nobody
    looked, and the client kept waiting for a server that would never answer.
    """
    taken = ServerInThread(_app(), free_port())
    taken.__enter__()
    servers.append(taken)

    doomed = ServerInThread(_app(), taken.port)
    with pytest.raises(RuntimeError, match="never came up"):
        doomed.__enter__()
    # __exit__ was already run by the failing __enter__ path; do not re-add it.
