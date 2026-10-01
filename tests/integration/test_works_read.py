"""Reading a work: the handle answers, and the ledger enumerates.

Scenario register (normative source: SCENARIOS.md, AC-186..188; issue #189).

A handle is only useful if it leads somewhere. `GET /v1/works/{id}` says how the
work is doing -- status plus the folded counts -- and `/units` enumerates what
happened to each target. That second read is what replaces the report the old
endpoint tried to return in one response: it pages, it can be re-read, and it
still answers after the fact.

`rejected` (this deployment refused the target before acting: not the source's
host, no such capability, over a ceiling) and `failed` (the action ran and the
world said no) are counted apart, because only the second is worth re-sending.

The executor arrives in #190, so these tests write the ledger through the
repository -- the same path the executor will use, which is what the read side
is supposed to describe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from adapters.repository import append_unit, work_by_id
from app.main import create_app, create_engine_for
from core.enums import ErrorClass, UnitOutcome
from tests.migrated_schema import migrated_database

WRITE_TOKEN = "works-read-token"
READ_TOKEN = "works-read-only-token"


def _targets(count: int) -> list[str]:
    return [
        f"https://kogumagoya.example/blog/archives/{2000 + n}" for n in range(count)
    ]


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "works_read.db")
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


async def _submit(writer: httpx.AsyncClient, targets: list[str]) -> str:
    response = await writer.post(
        "/v1/works",
        headers={"Idempotency-Key": f"read-{len(targets)}-{targets[0][-4:]}"},
        json={"kind": "fetch", "targets": targets, "params": {"source_id": "s1"}},
    )
    assert response.status_code == 202, response.text
    return str(response.json()["work_id"])


def _write_ledger(
    session_factory: sessionmaker[Session],
    work_id: str,
    entries: list[tuple[str, UnitOutcome, ErrorClass | None]],
) -> None:
    with session_factory() as session:
        for target, outcome, error_class in entries:
            append_unit(
                session,
                work_id=work_id,
                target=target,
                outcome=outcome,
                error_class=error_class,
                detail="",
            )
        session.commit()


# --------------------------------------------------------------------------
# AC-186: the handle leads to the ledger, and the ledger is complete
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac186_the_handle_reports_status_and_folded_counts(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-186: `GET /v1/works/{id}` answers from the ledger, not from a counter."""
    targets = _targets(3)
    work_id = await _submit(writer, targets)
    _write_ledger(session_factory, work_id, [(targets[0], UnitOutcome.OK, None)])

    response = await writer.get(f"/v1/works/{work_id}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["work_id"] == work_id
    assert payload["kind"] == "fetch"
    assert payload["status"] == "queued"
    assert payload["counts"] == {
        "ok": 1,
        "skipped": 0,
        "rejected": 0,
        "failed": 0,
        "queued": 2,
        "total": 3,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac186_the_ledger_enumerates_every_target_exactly_once(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-186: the ledger covers the submitted targets, once each.

    Exactly once is the claim that matters: a duplicate row would mean an
    action ran twice, and a missing one would mean a target nobody can account
    for.
    """
    targets = _targets(4)
    work_id = await _submit(writer, targets)
    _write_ledger(
        session_factory,
        work_id,
        [(target, UnitOutcome.OK, None) for target in targets],
    )

    response = await writer.get(f"/v1/works/{work_id}/units")

    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert sorted(row["target"] for row in rows) == sorted(targets)
    assert len(rows) == len(targets)


# --------------------------------------------------------------------------
# AC-187: attribution, and the two failure kinds kept apart
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac187_rejected_and_failed_are_attributed_and_counted_apart(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-187: every non-ok row carries its target and its class."""
    targets = _targets(3)
    work_id = await _submit(writer, targets)
    _write_ledger(
        session_factory,
        work_id,
        [
            (targets[0], UnitOutcome.REJECTED, ErrorClass.STRUCTURE_CHANGED),
            (targets[1], UnitOutcome.FAILED, ErrorClass.NETWORK),
        ],
    )

    summary = await writer.get(f"/v1/works/{work_id}")
    assert summary.json()["counts"]["rejected"] == 1
    assert summary.json()["counts"]["failed"] == 1

    rows = (await writer.get(f"/v1/works/{work_id}/units")).json()["data"]
    by_target = {row["target"]: row for row in rows}
    assert by_target[targets[0]]["outcome"] == "rejected"
    assert by_target[targets[0]]["error_class"] == "structure_changed"
    assert by_target[targets[1]]["outcome"] == "failed"
    assert by_target[targets[1]]["error_class"] == "network"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac187_the_ledger_can_be_filtered_to_the_failures(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-187: "what do I need to re-send?" is one filtered read.

    This is the question the old endpoint could not answer after a timeout,
    because the answer only existed inside the response that was lost.
    """
    targets = _targets(4)
    work_id = await _submit(writer, targets)
    _write_ledger(
        session_factory,
        work_id,
        [
            (targets[0], UnitOutcome.OK, None),
            (targets[1], UnitOutcome.FAILED, ErrorClass.NETWORK),
            (targets[2], UnitOutcome.REJECTED, ErrorClass.STRUCTURE_CHANGED),
            (targets[3], UnitOutcome.SKIPPED, None),
        ],
    )

    failed = await writer.get(
        f"/v1/works/{work_id}/units", params={"outcome": "failed"}
    )

    assert failed.status_code == 200, failed.text
    rows = failed.json()["data"]
    assert [row["target"] for row in rows] == [targets[1]]


# --------------------------------------------------------------------------
# AC-188: paging is stable
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac188_paging_the_ledger_visits_every_row_once(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-188: page two must not skip or repeat a target."""
    targets = _targets(5)
    work_id = await _submit(writer, targets)
    _write_ledger(
        session_factory, work_id, [(target, UnitOutcome.OK, None) for target in targets]
    )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await writer.get(f"/v1/works/{work_id}/units", params=params)
        assert page.status_code == 200, page.text
        payload = page.json()
        seen.extend(row["target"] for row in payload["data"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == sorted(targets)
    assert len(seen) == len(set(seen))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_work_is_not_found(writer: httpx.AsyncClient) -> None:
    """A wrong id is a 404, not an empty ledger."""
    response = await writer.get("/v1/works/work-nope")

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error"]["code"] == "work_not_found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_malformed_cursor_is_refused_rather_than_restarting(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """Silently serving page one would look like a successful page two."""
    work_id = await _submit(writer, _targets(2))

    response = await writer.get(
        f"/v1/works/{work_id}/units", params={"cursor": "not-a-cursor"}
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["code"] == "invalid_cursor"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_work_row_and_the_summary_agree(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """The HTTP view and the stored row describe the same work.

    The summary carries the target *count*, not the target list: a 1000-target
    work must not put 1000 strings in a response that is meant to be O(1). The
    enumeration belongs to `/units`, which pages.
    """
    targets = _targets(2)
    work_id = await _submit(writer, targets)

    response = await writer.get(f"/v1/works/{work_id}")

    with session_factory() as session:
        stored = work_by_id(session, work_id)
    assert stored is not None
    assert response.json()["counts"]["total"] == len(stored.targets)
    assert response.json()["params"] == stored.params
    assert "targets" not in response.json()
