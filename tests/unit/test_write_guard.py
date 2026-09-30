"""Every write tool confirms exactly the arguments it executes, once.

Scenario register (normative source: docs/api/mcp-tool-guide.md section 3;
issue #76).

  AC-75 每个写工具的确认覆盖全部执行参数
    Given 任一写工具的调用参数集合
    When  只改动其中一个参数的值
    Then  确认字段里至少有一项随之变化(漂移可被发现)
    And   这条断言对所有写工具成立(从真实注册表机械遍历, 不是逐个手写)

  AC-76 守卫只存在一份
    Given src/app/mcp/tools/writes.py
    When  搜索 declined/aborted 的字面文本与手写比对
    Then  拒绝文本各出现一次, 且没有工具自己写 `_matches(...)`
    And   每个写工具都经由同一个守卫(数量与注册表一致)

  AC-77 既有行为完全不变(这是重构, 不是改语义)
    Given 改动前后的完整 MCP 测试套件
    When  对同一组请求(确认/拒绝/纂改/不支持 elicitation)
    Then  行为一致, 且工具面快照无变化
    (由 CI 的 contract 与既有套件承担: test_mcp_server 的
    test_every_write_tool_aborts_on_a_tampered_confirmation 驱动真实
    elicitation 逐个工具验过, 本文件不重复)

**AC-75 是「已正确, 现在钉住」** —— 必须如实说明: 逐参数扰动后确认字段都会变,
今天已经成立, 因为 #67 给 submit_items 补了 items_fingerprint。本文件的价值在将来:
它是**从真实注册表推出来的**(不是手写的 12 条), 所以第 13 个写工具自动纳入;
而改动前那条测试(test_mcp_server 里的 9 个工具)是手写清单, 恰好漏了
delete_items / delete_source_items / submit_items 三个。

**AC-76 才是本次红的原因**: 守卫文本写了 12 遍, 手写比对写了 12 处。

Why the sweep is structural rather than behavioural: the protocol already refuses
a retry whose arguments drifted -- the SDK seals `request_state` with
sha256 over the whole arguments, so the tool body never runs at all (measured;
see tests/integration/test_submit_items_confirmation.py). `_matches` is the
second gate, for a client that answers the form with something other than what
was prefilled. No request can therefore make a missing field visible, which is
why #67's omission survived twelve copies of the same code: what rots here is
the shape, not a behaviour.
"""

from __future__ import annotations

import inspect
import typing
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resolve import Resolve

from app.mcp.core_client import CoreAPIClient
from app.mcp.tools.writes import register_writes

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITES = REPO_ROOT / "src/app/mcp/tools/writes.py"
CONFIRMATION = REPO_ROOT / "src/app/mcp/confirmation.py"

# One plausible value per argument name, so every resolver can be called with
# real shapes: the fields it derives are what these scenarios compare.
ARGUMENTS: dict[str, Any] = {
    "url": "https://example.test/feed.xml",
    "urls": ["https://example.test/a.xml", "https://example.test/b.xml"],
    "batch_size": 5,
    "min_interval_seconds": 1.5,
    "schedule_policy": {"type": "fixed", "interval": "30m"},
    "type": "rss",
    "pipeline_chain": [{"plugin_id": "translate", "kind": "translation"}],
    "target_lang": "en",
    "folder": "tech",
    "source_id": "src-1",
    "status": "disabled",
    "item_ids": ["i1", "i2"],
    "since": "2026-01-01T00:00:00Z",
    "until": "2026-02-01T00:00:00Z",
    "include_published": True,
    "plugin_id": "rss",
    "trust_level": "standard",
    "parser_plugin_id": "rss",
    "items": [{"title": "A", "url": "https://x.test/a", "content": "<p>a</p>"}],
}


def _write_tools() -> dict[str, Any]:
    """Every registered write tool with the resolver its `confirmed` carries.

    Read from the server rather than from a list here: a tool added later is
    covered by these scenarios without anyone remembering to add it, which is
    the whole point of AC-75's "机械遍历, 不是逐个手写".
    """
    server = MCPServer("scenario-register")
    register_writes(server, CoreAPIClient(object()))
    tools: dict[str, Any] = {}
    for name, tool in sorted(server._tool_manager._tools.items()):
        hints = typing.get_type_hints(tool.fn, include_extras=True)
        resolver = next(
            meta.fn
            for hint in hints.values()
            for meta in getattr(hint, "__metadata__", ())
            if isinstance(meta, Resolve)
        )
        tools[name] = resolver
    return tools


def _arguments_of(fn: Any) -> list[str]:
    """The tool's own arguments: not `ctx`, and not the confirmed value."""
    return [
        p for p in inspect.signature(fn).parameters if p not in ("ctx", "confirmed")
    ]


def _confirmed(resolver: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """The fields the dialog would carry, as name to prefilled value."""
    elicit = resolver(**arguments)
    return {name: field.default for name, field in elicit.schema.model_fields.items()}


def _perturbed(value: Any) -> Any:
    """A different value of the same kind, so the resolver sees a valid shape.

    A list of dicts is perturbed *inside* the first element rather than by
    growing: that is precisely the drift #67 was about (same count, same
    titles, different bodies), and lengthening the list would not exercise it.
    """
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value + "X"
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if value is None:
        return "X"
    if isinstance(value, dict):
        return {**value, "__perturbed__": True}
    if isinstance(value, list):
        if not value:
            return ["X"]
        head = value[0]
        if isinstance(head, dict):
            return [{**head, "__perturbed__": True}, *value[1:]]
        return [_perturbed(head), *value[1:]]
    raise AssertionError(f"no perturbation defined for {value!r}")


# --------------------------------------------------------------------------
# AC-75: a drifted argument must move something in the confirmation
# --------------------------------------------------------------------------


def test_every_write_argument_would_show_up_in_the_confirmation() -> None:
    """AC-75: for each write tool, no argument can change unnoticed.

    Asserting the diff rather than the field list is what keeps this from
    pinning today's implementation: a tool may name its fields however it
    likes, as long as changing any argument changes what the user was asked
    to confirm.
    """
    invisible: list[str] = []
    tools = _write_tools()
    # The count is a claim, not a property: a new write tool has to change this
    # line, which is where someone notices that the confirmation surface grew.
    assert len(tools) == 15, (
        f"expected the fifteen write tools, found {sorted(tools)} -- update the "
        "claim in this docstring if the surface really changed"
    )

    for name, resolver in tools.items():
        parameters = _arguments_of(resolver)
        baseline = {p: ARGUMENTS[p] for p in parameters}
        confirmed = _confirmed(resolver, baseline)
        assert confirmed, f"{name} confirms nothing at all"

        for parameter in parameters:
            drifted = {**baseline, parameter: _perturbed(baseline[parameter])}
            if _confirmed(resolver, drifted) == confirmed:
                invisible.append(f"{name}({parameter})")

    assert not invisible, (
        "these arguments could be changed without changing anything the user "
        "confirms, so a tampered client would execute what was never shown "
        f"(issue #76): {invisible}"
    )


# --------------------------------------------------------------------------
# AC-76: the guard exists once, and every write goes through it
# --------------------------------------------------------------------------


def test_the_write_guard_is_written_once() -> None:
    """AC-76: twelve copies of a guard is twelve chances to miss a field.

    The count of `_guarded_write` call sites is compared against the live
    registry rather than a literal, so a thirteenth write tool that forgets the
    guard fails here instead of re-introducing the shape #67 fell into.
    """
    source = WRITES.read_text(encoding="utf-8")
    # The refusal is produced by `_write_verdict`, which lives with the rest of
    # the confirmation machinery; a tool file that contains the text again means
    # a tool is answering for itself.
    whole_path = source + CONFIRMATION.read_text(encoding="utf-8")

    for literal in ('"declined": True', '"aborted": True'):
        assert whole_path.count(literal) == 1, (
            f"{literal} appears {whole_path.count(literal)} times across the "
            "write path; the refusal belongs to the one guard, not to each tool"
        )

    assert source.count("_matches(") == 0, (
        "a tool is comparing confirmed arguments by hand again; hand over the "
        "fields dict to the guard instead, so the comparison cannot list fewer "
        "fields than the dialog"
    )

    assert source.count("await _guarded_write(") == len(_write_tools()), (
        f"{source.count('await _guarded_write(')} write tools route through the "
        f"guard but {len(_write_tools())} are registered: every mutating tool "
        "must confirm, compare, execute and attribute in that order"
    )
