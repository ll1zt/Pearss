"""What actually enforces "confirmed arguments == executed arguments".

Scenario register (normative source: docs/api/mcp-tool-guide.md section 3;
issue #67).

  AC-44 重试时内容漂移会被拒绝
    Given 一次 submit_items 调用已经 elicit, 用户确认了 3 条 (title A/B/C)
    When 客户端重试原请求, 但送来 3 条内容不同的 items (同 title, 同数量)
    Then 调用被拒绝, 且语料库里没有写入那些内容

  AC-45 内容一致的重试正常执行 (不能把合法重试也拒了)
    Given 用户确认了 3 条 (title A/B/C)
    When 客户端重试并送来同样 3 条 (同一顺序、同一内容)
    Then 写入正常完成, 结果带 audit_id

  AC-46 顺序变化同样被拒绝 (边界)
    Given 用户确认了 A/B/C
    When 重试送来同一批内容但顺序为 C/A/B
    Then 调用被拒绝 —— 因为绑定是对**整个 arguments** 的精确摘要

Issue #67 的假设是: `submit_items` 的校验只比 `item_count`, 所以「同数量、不同内容」的
重试会通过并写入未经确认的内容。**实测否证了这个假设**, 本文件把真正的机制钉住。

真正的机制在框架层, 不在我们的 `_matches` 里: `MCPServer` 默认安装
`RequestStateSecurity.ephemeral()`, 它把 `request_state` 用 AES-GCM 封起来, 并把
`_request_identity(method, params)` 的 `args_digest = sha256(canonical_json(arguments))`
写进封签。第二轮带不同的 `arguments` 时摘要不符, 整个 `tools/call` 被拒
(`-32602 Invalid or expired requestState`, reason `invalid_request_state`)——
**工具体根本不会执行**。

所以「确认值 == 执行值」在本协议上是被**封签**保证的, 而不是被 `_matches` 保证的;
`item_count` 比对是第二道闸(防客户端提交与预填不一致的答案), 不是唯一的闸。
本文件的价值是: 一旦有人关掉 `request_state_security`、换成不带 request state 的
通道、或把 resolver 改成不需要 elicit, 这些场景会变红。

实测对照(同一份探针, 唯一变量是第二轮的 arguments):

    control (相同 arguments)  -> executed, stored ['A','B','C']
    drift   (同 title 同数量) -> -32602 invalid_request_state, stored []
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from adapters.repository import create_source, timestamp
from app.main import create_app, create_engine_for
from tests.integration.conftest import (
    ServerInThread,
    free_port,
    run_work_to_completion,
)
from tests.migrated_schema import migrated_database

pytestmark = pytest.mark.integration

MCP_EDGE_TOKEN = "submit-confirm-edge-token"
API_TOKEN = "submit-confirm-api-token"

# A client that can answer a form elicitation. Without this the server refuses
# the write with MissingRequiredClientCapability and never reaches the question
# these scenarios are about.
_ELICITATION_CAPABILITY: dict[str, dict[str, dict[str, object]]] = {
    "elicitation": {"form": {}}
}


def _item(title: str, external_id: str, body: str) -> dict[str, str]:
    return {
        "external_id": external_id,
        "title": title,
        "canonical_url": f"https://submit.example/{external_id}",
        "content": body,
    }


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("submitconfirm")
    url = migrated_database(root, "submitconfirm.db")
    engine = create_engine_for(url)
    with sessionmaker(engine, expire_on_commit=False)() as seed:
        create_source(
            seed,
            source_type="agent",
            url="agent://intake",
            canonical_url="agent://intake",
            schedule_policy="{}",
            tenant_id="default",
            at=timestamp(datetime.now(UTC)),
        )
        seed.commit()
    engine.dispose()
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


def _agent_source_id(url: str) -> str:
    engine = create_engine_for(url)
    try:
        with engine.connect() as conn:
            return str(
                conn.execute(
                    sa.text("select id from source where type = 'agent'")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _stored_titles(url: str) -> list[str]:
    engine = create_engine_for(url)
    try:
        with engine.connect() as conn:
            return [
                title
                for (title,) in conn.execute(
                    sa.text("select title from item order by title")
                ).fetchall()
            ]
    finally:
        engine.dispose()


async def _rpc(
    client: httpx.AsyncClient, base: str, params: dict[str, Any], *, rid: int
) -> dict[str, Any]:
    """One raw `tools/call`, so the MRTR rounds are observable.

    The camelCase wire names (`inputResponses`, `requestState`) and the
    `mcp-name` header are both load-bearing: with the snake_case spellings the
    server ignores the answers and simply asks again, which looks like a pass
    while proving nothing.
    """
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


async def _ask_then_replay(
    base: str,
    url: str,
    *,
    asked: list[dict[str, str]],
    replayed: list[dict[str, str]],
    answer_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Round 1 asks about `asked`; round 2 replays the answers with `replayed`.

    Returns round 2's JSON-RPC envelope. The answers are the schema the user was
    shown (the server's own prefilled defaults), which is what "the user pressed
    accept" looks like on the wire; `answer_overrides` replaces individual
    answers, which is how a client that edits a field is modelled.
    """
    source_id = _agent_source_id(url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        first = await _rpc(
            client,
            base,
            {
                "name": "submit_items",
                "arguments": {"source_id": source_id, "items": asked},
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
        answers.update(answer_overrides or {})
        return await _rpc(
            client,
            base,
            {
                "name": "submit_items",
                "arguments": {"source_id": source_id, "items": replayed},
                "inputResponses": {key: {"action": "accept", "content": answers}},
                "requestState": state,
            },
            rid=2,
        )


# --------------------------------------------------------------------------
# AC-44: content drift is refused
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drifted_content_on_the_retry_is_refused(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-44: same titles, same count, different bodies -> nothing is written.

    The confirmation form shows titles, so a client that swaps the bodies while
    keeping the titles is submitting something the user never saw. Measured:
    the framework refuses the whole call, so the tool body never runs.
    """
    asked = [
        _item("Drift A", "drift-a", "<p>original</p>"),
        _item("Drift B", "drift-b", "<p>original</p>"),
    ]
    drifted = [
        _item("Drift A", "drift-a", "<p>EVIL</p>"),
        _item("Drift B", "drift-b", "<p>EVIL</p>"),
    ]

    reply = await _ask_then_replay(
        base_url, env["PEARSS_DATABASE_URL"], asked=asked, replayed=drifted
    )

    # Refused, and refused for the binding reason -- not silently executed and
    # not merely declined, which would leave a client free to retry the drift.
    error = reply.get("error")
    assert error is not None, reply
    assert error["code"] == -32602, error
    assert error["data"]["reason"] == "invalid_request_state", error

    # The load-bearing assertion: the unconfirmed bodies did not land.
    stored = _stored_titles(env["PEARSS_DATABASE_URL"])
    assert "Drift A" not in stored, stored
    assert "Drift B" not in stored, stored


@pytest.mark.asyncio
async def test_reordered_items_on_the_retry_are_refused(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-46: the binding is over the whole arguments, so order counts too.

    The policy, stated once so it is not left to inference: reordering is a
    different request, because the arguments are digested whole. The user was
    shown an order and the response reports results in that order, so treating
    a permutation as the same request would be the wrong licence.
    """
    asked = [
        _item("Order A", "order-a", "<p>x</p>"),
        _item("Order B", "order-b", "<p>x</p>"),
        _item("Order C", "order-c", "<p>x</p>"),
    ]
    reordered = [asked[2], asked[0], asked[1]]

    reply = await _ask_then_replay(
        base_url, env["PEARSS_DATABASE_URL"], asked=asked, replayed=reordered
    )

    error = reply.get("error")
    assert error is not None, reply
    assert error["data"]["reason"] == "invalid_request_state", error
    assert "Order A" not in _stored_titles(env["PEARSS_DATABASE_URL"])


# --------------------------------------------------------------------------
# AC-45: the legitimate retry still works
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_identical_retry_executes(base_url: str, env: dict[str, str]) -> None:
    """AC-45: the guard must not be so strict that it rejects a real retry.

    Without this, an implementation that refused every second round would pass
    AC-44 while making every write impossible.
    """
    asked = [
        _item("Retry A", "retry-a", "<p>x</p>"),
        _item("Retry B", "retry-b", "<p>x</p>"),
    ]

    reply = await _ask_then_replay(
        base_url, env["PEARSS_DATABASE_URL"], asked=asked, replayed=asked
    )

    assert "error" not in reply, reply
    text = reply["result"]["content"][0]["text"]
    body = json.loads(text)
    # Since #199 the tool queues a job instead of storing inline: the identical
    # retry must queue the same two items, and the worker is what stores them.
    assert body["counts"]["total"] == 2, body
    assert body["work_id"], body
    assert body["audit_id"], body

    await run_work_to_completion(env, body["work_id"])

    stored = _stored_titles(env["PEARSS_DATABASE_URL"])
    assert {"Retry A", "Retry B"} <= set(stored), stored


# --------------------------------------------------------------------------
# AC-44 (local layer): the fingerprint is the check our own code performs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_foreign_answer_does_not_change_the_batch(
    base_url: str, env: dict[str, str]
) -> None:
    """AC-240: the batch that executes is the batch the tool was called with.

    Round 2 used to have to answer with the fingerprint of the batch it was shown
    (AC-44's local half), so an answer carrying a different digest was refused.
    Since #250 the dialog is one acknowledgement and the values come from the
    server, so an answer that invents fields is not read at all: the batch is
    stored, and the assertion that matters is *which* batch -- the one the tool
    was called with, bodies included. What still refuses a retry with different
    arguments is the framework's digest (AC-44 and AC-46, above).
    """
    asked = [
        _item("Foreign A", "foreign-a", "<p>x</p>"),
        _item("Foreign B", "foreign-b", "<p>x</p>"),
    ]

    reply = await _ask_then_replay(
        base_url,
        env["PEARSS_DATABASE_URL"],
        asked=asked,
        replayed=asked,
        # A well-formed digest for some other batch, and a count that matches
        # nothing: neither is a field of the dialog any more.
        answer_overrides={"items_fingerprint": "0" * 64, "item_count": "99"},
    )

    assert "error" not in reply, reply
    body = json.loads(reply["result"]["content"][0]["text"])
    assert body.get("aborted") is None, body
    # The write is a work now, so the batch lands when the worker runs it -- and
    # *which* batch it is, is what this asserts.
    assert body.get("work_id"), body
    await run_work_to_completion(env, body["work_id"])
    stored = _stored_titles(env["PEARSS_DATABASE_URL"])
    assert "Foreign A" in stored and "Foreign B" in stored, stored
