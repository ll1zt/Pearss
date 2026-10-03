"""A batch of addresses is a job: one confirmation, one handle, a ledger.

Scenario register (normative source: `SCENARIOS.md`; issues #171 and #191;
AC-176, AC-193..194).

The MCP face of `POST /v1/works` with `kind=fetch`. An agent is the caller that
discovers addresses, so the tool has to exist -- and it has to confirm: the
deployment would make real outbound requests to someone else's site, once per
address.

The dialog cannot render a list (an elicitation form carries primitives), so
the confirmation is count + preview + a fingerprint of the list. The scenario
that matters is the edited one: a client that accepted a dialog about [a, b]
must not be able to execute [a, c] with that acceptance.

The other half is the shape of the answer. It used to be a report -- one result
per address, inline -- which meant a 200-address call timed out at the gateway
while the deployment kept working, and the results were lost with the response.
Now the answer is a handle, and per-address results are read from the work's
ledger with `work_units`. The scenarios below pin both halves: the handle's
size must not follow the list, and the ledger must be where the addresses end
up.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.main import create_app, create_engine_for
from tests.integration.conftest import ServerInThread, free_port
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

MCP_EDGE_TOKEN = "fetch-items-mcp-token"
API_TOKEN = "fetch-items-api-token"

SOURCE_URL = "https://items.example/feed.xml"
SOURCE_HOST = "https://items.example"
ASKED = [
    "https://items.example/blog/archives/1",
    "https://items.example/blog/archives/2",
]
EDITED = [
    "https://items.example/blog/archives/1",
    "https://items.example/blog/archives/9",
]
# Off the source's host: refused before any fetch, which is what makes an
# executed call observable here without reaching anyone's server.
OFF_HOST = [
    "https://elsewhere.example/blog/archives/1",
    "https://elsewhere.example/blog/archives/2",
]

# Without an elicitation-capable client the server refuses the write with
# MissingRequiredClientCapability and never asks the question these scenarios
# are about.
_ELICITATION_CAPABILITY: dict[str, dict[str, dict[str, object]]] = {
    "elicitation": {"form": {}}
}


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("fetchitems")
    url = migrated_database(root, "fetchitems.db")
    create_engine_for(url).dispose()
    return {
        "PEARSS_DATABASE_URL": url,
        "PEARSS_STORAGE_DIR": str(root / "storage"),
        "PEARSS_API_TOKEN": API_TOKEN,
        "PEARSS_MCP_TOKEN": MCP_EDGE_TOKEN,
        "PEARSS_SCHEDULER_ENABLED": "0",
    }


@pytest.fixture(scope="module")
def base_url(env: dict[str, str]) -> Iterator[str]:
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        app = create_app()
        with ServerInThread(app, free_port()) as server:
            yield server.base_url("")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _seed_source(database_url: str) -> str:
    """Store an rss source through the repository and return its id.

    Through the repository rather than a raw INSERT: the identity is derived
    there, and a hand-written row would have to restate every NOT NULL column
    (and break on the next one the schema gains).
    """
    from datetime import UTC, datetime

    from adapters.repository import create_source
    from adapters.timestamps import fixed_width_utc as timestamp

    engine = create_engine_for(database_url)
    try:
        with sessionmaker(engine, expire_on_commit=False)() as db:
            row = create_source(
                db,
                source_type="rss",
                url=SOURCE_URL,
                canonical_url=SOURCE_URL,
                schedule_policy='{"type": "fixed", "interval": "30m"}',
                tenant_id="default",
                at=timestamp(datetime(2026, 9, 30, 12, 0, tzinfo=UTC)),
            )
            db.commit()
            return str(row.id)
    finally:
        engine.dispose()


def _item_count(database_url: str) -> int:
    engine = create_engine_for(database_url)
    try:
        with engine.connect() as conn:
            return int(conn.execute(sa.text("select count(*) from item")).scalar_one())
    finally:
        engine.dispose()


def _work_count(database_url: str) -> int:
    """How many works were queued. The other half of "nothing executed"."""
    engine = create_engine_for(database_url)
    try:
        with engine.connect() as conn:
            return int(conn.execute(sa.text("select count(*) from work")).scalar_one())
    finally:
        engine.dispose()


def _payload(reply: dict[str, Any]) -> dict[str, Any]:
    """The JSON body of a completed `tools/call`: its first text block.

    Raw `_rpc` is used here rather than the SDK's client so the elicitation
    rounds are observable; that also means unwrapping the result by hand, the
    same way `tests/integration/test_mcp_server.py::_payload` does.
    """
    for block in reply.get("result", {}).get("content", []):
        text_value = block.get("text")
        if text_value:
            parsed: dict[str, Any] = json.loads(text_value)
            return parsed
    return {}


async def _rpc(
    client: httpx.AsyncClient, base: str, params: dict[str, Any], *, rid: int
) -> dict[str, Any]:
    """One raw `tools/call`, so the MRTR rounds are observable."""
    body = {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": _ELICITATION_CAPABILITY,
            },
        },
    }
    response = await client.post(
        f"{base}/mcp",
        json=body,
        headers={
            "authorization": f"Bearer {MCP_EDGE_TOKEN}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2026-07-28",
            "mcp-method": "tools/call",
            "mcp-name": str(params.get("name", "")),
        },
    )
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                decoded: dict[str, Any] = json.loads(line[len("data:") :].strip())
                return decoded
        return {"raw": response.text}
    try:
        decoded = response.json()
    except ValueError:
        decoded = {"raw": response.text}
    return decoded


def _arguments(source_id: str, urls: list[str], **extra: Any) -> dict[str, Any]:
    return {"source_id": source_id, "urls": urls, **extra}


async def _ask(
    base: str,
    source_id: str,
    asked: list[str],
    *,
    replayed: list[str],
    asked_extra: dict[str, Any] | None = None,
    replayed_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Round 1 asks about `asked`; round 2 replays the answers with `replayed`.

    The two extra dicts are what makes the tampering scenarios expressible:
    round 1 shows the dialog for one set of arguments and round 2 executes
    another, which is exactly what a client is able to do.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        first = await _rpc(
            client,
            base,
            {
                "name": "fetch_source_items",
                "arguments": _arguments(source_id, asked, **(asked_extra or {})),
            },
            rid=1,
        )
        result = first["result"]
        state = result["requestState"]
        key = next(iter(result["inputRequests"]))
        properties = result["inputRequests"][key]["params"]["requestedSchema"][
            "properties"
        ]
        answers = {name: spec.get("default") for name, spec in properties.items()}
        return await _rpc(
            client,
            base,
            {
                "name": "fetch_source_items",
                "arguments": _arguments(source_id, replayed, **(replayed_extra or {})),
                "inputResponses": {key: {"action": "accept", "content": answers}},
                "requestState": state,
            },
            rid=2,
        )


def _seed_ledger(database_url: str, source_id: str) -> str:
    """A finished work with two rows, written through the repository.

    Not a raw INSERT and not a mock: the tool reads the same table the executor
    writes, so a schema change has to break this test rather than pass beside it.
    """
    from adapters.repository import append_unit, create_work, finish_work
    from core.enums import ErrorClass, UnitOutcome, WorkStatus

    engine = create_engine_for(database_url)
    try:
        with sessionmaker(engine, expire_on_commit=False)() as db:
            work = create_work(
                db,
                kind="fetch",
                targets=list(ASKED),
                params={"source_id": source_id},
                fingerprint="mcp-work-tools-ledger",
                tenant_id="default",
            )
            append_unit(
                db,
                work_id=work.id,
                target=ASKED[0],
                outcome=UnitOutcome.OK,
                detail="stored",
                ref="item-1",
            )
            append_unit(
                db,
                work_id=work.id,
                target=ASKED[1],
                outcome=UnitOutcome.FAILED,
                error_class=ErrorClass.PARSE,
                detail="no item",
            )
            finish_work(db, work.id, status=WorkStatus.PARTIAL)
            db.commit()
            return str(work.id)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_the_dialog_names_the_list_once(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-176: one elicitation for the list, carrying its fingerprint."""
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])

    async with httpx.AsyncClient(timeout=30.0) as client:
        reply = await _rpc(
            client,
            base_url,
            {"name": "fetch_source_items", "arguments": _arguments(source_id, ASKED)},
            rid=1,
        )

    result = reply["result"]
    assert len(result["inputRequests"]) == 1, result
    only = next(iter(result["inputRequests"].values()))
    message = only["params"]["message"]
    # The list is named in words (count and addresses), because the form is one
    # acknowledgement since #250. The fingerprint, which is what covers the
    # addresses the preview truncates, is asserted against the derived fields in
    # tests/unit/test_write_guard.py.
    assert "2" in message, message
    for url in ASKED:
        assert url in message, message
    assert set(only["params"]["requestedSchema"]["properties"]) == {
        "confirmed",
        "edit",
    }, only


@pytest.mark.asyncio
async def test_an_edited_list_is_refused_and_stores_nothing(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-176 / AC-194: accepting a dialog about one list cannot execute another.

        Same count, same first address -- exactly the case a count-only check
        would accept -- and the list nobody was shown does not execute: the
        accepted state is bound to the arguments it was created for. "Does not
    execute" now means nothing was *queued* either: a work row for the edited
    list would keep fetching long after the caller saw an error.
    """
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])
    before = _work_count(env["PEARSS_DATABASE_URL"])

    reply = await _ask(base_url, source_id, ASKED, replayed=EDITED)

    error = reply.get("error")
    assert error is not None, reply
    assert error["code"] == -32602, error
    assert _item_count(env["PEARSS_DATABASE_URL"]) == 0
    # The module shares one database, so "queued nothing" is a delta: a work
    # row for the edited list would keep fetching after the caller saw an error.
    assert _work_count(env["PEARSS_DATABASE_URL"]) == before


@pytest.mark.asyncio
async def test_ac193_the_answer_is_a_handle_whose_size_does_not_follow_the_list(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-193: the response carries a handle, so it is blind to how many targets.

    Ten addresses and forty addresses produce answers of *the same length*.
    That is the falsifiable form of "no per-address results": any field whose
    size follows the list -- a results array, an echo of the urls -- shows up
    here as a difference, and the old shape (a report) fails this by design.
    """
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])
    ten = [f"{SOURCE_HOST}/blog/archives/{n}" for n in range(10, 20)]
    forty = [f"{SOURCE_HOST}/blog/archives/{n}" for n in range(10, 50)]
    assert len(ten) == 10 and len(forty) == 40
    before = _work_count(env["PEARSS_DATABASE_URL"])

    small = await _ask(base_url, source_id, ten, replayed=ten)
    large = await _ask(base_url, source_id, forty, replayed=forty)

    payload_small = _payload(small)
    payload_large = _payload(large)
    assert set(payload_small) == set(payload_large)
    assert "results" not in payload_small and "urls" not in payload_small
    assert len(json.dumps(payload_small)) == len(json.dumps(payload_large)), (
        "the answer grew with the number of addresses, so it is carrying a "
        "report: "
        f"{len(json.dumps(payload_small))} vs {len(json.dumps(payload_large))}"
    )
    # A handle nobody can follow would be a capability removed, so the work it
    # names is real and carries the whole list.
    assert payload_large["counts"]["total"] == 40
    assert payload_large["status"] == "queued"
    assert _work_count(env["PEARSS_DATABASE_URL"]) == before + 2


@pytest.mark.asyncio
async def test_the_ledger_is_where_the_addresses_end_up(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-193: the same confirmation, read back through the read tools.

    `get_work` answers with the handle's contents (status and counts, no
    targets) and `work_units` enumerates the targets. Before the worker runs
    there are no rows yet, and that is the honest answer -- the queued count is
    where "not done yet" lives.
    """
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])

    reply = await _ask(base_url, source_id, OFF_HOST, replayed=OFF_HOST)
    work_id = _payload(reply)["work_id"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        work = await _rpc(
            client,
            base_url,
            {"name": "get_work", "arguments": {"work_id": work_id}},
            rid=3,
        )
        units = await _rpc(
            client,
            base_url,
            {"name": "work_units", "arguments": {"work_id": work_id}},
            rid=4,
        )

    summary = _payload(work)
    assert summary["work_id"] == work_id
    assert summary["status"] == "queued"
    assert summary["counts"] == {
        "ok": 0,
        "skipped": 0,
        "rejected": 0,
        "failed": 0,
        "queued": 2,
        "total": 2,
    }
    assert "targets" not in summary
    assert _payload(units)["data"] == []


@pytest.mark.asyncio
async def test_ac194_a_tampered_pacing_knob_is_refused(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-194: the confirmation covers the params, not just the target list.

    Pacing is the difference between a polite request and a burst, so it is
    part of what the user was shown. Executing a wider burst than the dialog
    named has to fail the same way an edited list does -- and queue nothing.
    """
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])
    before = _work_count(env["PEARSS_DATABASE_URL"])

    reply = await _ask(
        base_url,
        source_id,
        ASKED,
        replayed=ASKED,
        asked_extra={"batch_size": 5},
        replayed_extra={"batch_size": 9},
    )

    error = reply.get("error")
    assert error is not None, reply
    assert error["code"] == -32602, error
    assert _work_count(env["PEARSS_DATABASE_URL"]) == before


@pytest.mark.asyncio
async def test_the_ledger_renders_what_happened(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-193: `work_units` is the per-address report, read after the fact.

    The rows are written through the repository -- the real ledger, no mocks --
    and read back through the tool, because what this change adds is the
    projection: target, outcome, error_class, detail and the item it produced,
    with `outcome` as the filter that answers "what do I need to re-send?".
    """
    source_id = _seed_source(env["PEARSS_DATABASE_URL"])
    work_id = _seed_ledger(env["PEARSS_DATABASE_URL"], source_id)

    async with httpx.AsyncClient(timeout=30.0) as client:
        every = await _rpc(
            client,
            base_url,
            {"name": "work_units", "arguments": {"work_id": work_id}},
            rid=5,
        )
        only_failed = await _rpc(
            client,
            base_url,
            {
                "name": "work_units",
                "arguments": {"work_id": work_id, "outcome": "failed"},
            },
            rid=6,
        )
        missing = await _rpc(
            client,
            base_url,
            {"name": "get_work", "arguments": {"work_id": "nope"}},
            rid=7,
        )

    rows = _payload(every)["data"]
    assert [(row["target"], row["outcome"]) for row in rows] == [
        (ASKED[0], "ok"),
        (ASKED[1], "failed"),
    ]
    assert rows[0]["ref"] == "item-1"
    assert rows[0]["detail"] == "stored"
    assert rows[1]["error_class"] == "parse"
    assert [row["target"] for row in _payload(only_failed)["data"]] == [ASKED[1]]
    # The error is the API's code, not one the tool invented: an agent seeing
    # work_not_found from a tool and from /v1 is seeing one fact.
    assert _payload(missing)["error"] == "work_not_found"
