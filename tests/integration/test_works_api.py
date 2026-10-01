"""POST /v1/works: a command goes in, a handle comes out.

Scenario register (normative source: SCENARIOS.md, AC-183..185; issue #188).

The endpoint's contract is O(1) in and out. The request carries the targets;
the response carries a handle and the folded counts; the per-target ledger is a
separate read. That shape is what makes a job survivable -- a client timeout
can no longer lose a report, because there is no report in the response.

These tests pin the consequences: the response does not grow with the request,
a repeated command while the first is still in flight is one work rather than
two, and a large submission returns instead of blocking.

Runs the real application over a real migrated SQLite database, as the other
Core API integration suites do: the shape under test is the HTTP contract plus
the rows it writes.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app, create_engine_for
from tests.migrated_schema import migrated_database

WRITE_TOKEN = "works-write-token"


def _targets(
    count: int, *, prefix: str = "https://kogumagoya.example/blog/archives/"
) -> list[str]:
    return [f"{prefix}{2000 + n}" for n in range(count)]


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "works.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(tmp_path / "plugins"))
    yield create_app(session_factory)


@pytest_asyncio.fixture
async def writer(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


async def _submit(
    writer: httpx.AsyncClient,
    targets: list[str],
    *,
    key: str = "works-key",
    kind: str = "fetch",
    params: dict[str, object] | None = None,
) -> httpx.Response:
    return await writer.post(
        "/v1/works",
        headers={"Idempotency-Key": key},
        json={
            "kind": kind,
            "targets": targets,
            "params": params or {"source_id": "s1"},
        },
    )


def _work_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        rows = session.execute(
            sa.text("select id, kind, status, tenant_id from work")
        ).mappings()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# AC-183: the response is a handle, not a report
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac183_a_submission_returns_a_handle_and_the_folded_counts(
    writer: httpx.AsyncClient,
) -> None:
    """AC-183: handle + counts, and nothing that grows with the target list."""
    response = await _submit(writer, _targets(5), key="works-small")

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["work_id"]
    assert payload["status"] == "queued"
    assert payload["counts"]["total"] == 5
    assert payload["counts"]["queued"] == 5
    assert payload["correlation_id"]
    # The whole point: no per-target results in the response.
    assert "results" not in payload
    assert not [key for key, value in payload.items() if isinstance(value, list)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac183_the_response_shape_does_not_change_with_the_target_count(
    writer: httpx.AsyncClient,
) -> None:
    """AC-183: 5 targets and 50 targets produce the same response *shape*.

    A response that grows with its input forces every caller to branch on size;
    that is the failure mode this endpoint exists to remove.
    """
    small = await _submit(writer, _targets(5), key="works-shape-5")
    large = await _submit(
        writer, _targets(50, prefix="https://other.example/"), key="works-shape-50"
    )

    assert small.status_code == 202 and large.status_code == 202
    assert set(small.json()) == set(large.json())


# --------------------------------------------------------------------------
# AC-184: the same command while it is still in flight is one work
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac184_resubmitting_the_same_command_reuses_the_live_work(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-184: a retry must not create a second batch.

    Different Idempotency-Key on purpose: the retry here is a *command* being
    submitted twice (which is what an agent does after a timeout: it re-issues
    the call with a fresh key), not an HTTP replay of one request.
    """
    targets = _targets(4)
    first = await _submit(writer, targets, key="works-first")
    second = await _submit(writer, targets, key="works-second")

    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["work_id"] == second.json()["work_id"]

    rows = _work_rows(session_factory)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "queued"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_different_command_is_a_different_work(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """The dedupe is by command value, not by "a work exists"."""
    first = await _submit(writer, _targets(3), key="works-a")
    second = await _submit(
        writer, _targets(3, prefix="https://elsewhere.example/"), key="works-b"
    )

    assert first.json()["work_id"] != second.json()["work_id"]
    assert len(_work_rows(session_factory)) == 2


# --------------------------------------------------------------------------
# AC-185: a large submission returns instead of blocking
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac185_a_large_submission_returns_without_doing_the_work(
    writer: httpx.AsyncClient,
) -> None:
    """AC-185: 500 targets are accepted in well under a second.

    The bound is generous on purpose (a shared runner is slow); what it rules
    out is the old behaviour, where the endpoint fetched every address before
    answering at all.
    """
    started = time.monotonic()
    response = await _submit(writer, _targets(500), key="works-large")
    elapsed = time.monotonic() - started

    assert response.status_code == 202, response.text
    assert response.json()["counts"]["total"] == 500
    assert elapsed < 2.0, f"submission blocked for {elapsed:.2f}s"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_submission_over_the_ceiling_is_refused_with_a_pointer(
    writer: httpx.AsyncClient,
) -> None:
    """The ceiling exists so one request cannot ask for unbounded work."""
    response = await _submit(writer, _targets(5000), key="works-over")

    assert response.status_code == 422, response.text
    error = response.json()["detail"]["error"]
    assert error["code"] == "work_too_large"
    assert "targets" in error["suggestion"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_kind_is_refused(writer: httpx.AsyncClient) -> None:
    """A typo must not create a work nothing will ever execute."""
    response = await _submit(writer, _targets(1), key="works-kind", kind="frobnicate")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"]["code"] == "unknown_work_kind"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_submission_without_a_key_is_refused(writer: httpx.AsyncClient) -> None:
    """Writes require an Idempotency-Key, as everywhere else in /v1."""
    response = await writer.post(
        "/v1/works",
        json={"kind": "fetch", "targets": _targets(1), "params": {}},
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["code"] == "idempotency_key_required"


@pytest.mark.unit
def test_the_fingerprint_of_a_command_is_a_function_of_its_value() -> None:
    """AC-184's mechanism, pinned directly: same value, same digest; any
    difference in kind, targets or params changes it, and target order matters
    because the ledger and the response both report that order.
    """
    from core.work import command_fingerprint

    base = command_fingerprint(
        kind="fetch", targets=["a", "b"], params={"source_id": "s1"}
    )

    assert base == command_fingerprint(
        kind="fetch", targets=["a", "b"], params={"source_id": "s1"}
    )
    assert base != command_fingerprint(
        kind="delete", targets=["a", "b"], params={"source_id": "s1"}
    )
    assert base != command_fingerprint(
        kind="fetch", targets=["b", "a"], params={"source_id": "s1"}
    )
    assert base != command_fingerprint(
        kind="fetch", targets=["a", "b"], params={"source_id": "s2"}
    )
    assert len(base) == 64
