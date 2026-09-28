"""The guide's claims about the tool surface must be mechanically true.

Scenario register (normative source: docs/api/mcp-tool-guide.md; issue #69).

  AC-55 guide 的默认 limit 与实现一致
    Given guide 第 2 条规则声称的 MCP 默认 limit 与 /v1 默认 limit
    When  与工具源码、路由源码里的实际默认值对照
    Then  两个数字都与实现一致 (且实现内部各自统一)

  AC-56 guide 的章节编号无重复且递增
    Given mcp-tool-guide.md
    When  提取所有 "## <中文数字>、" 标题
    Then  编号严格递增且无重复

  AC-57 描述与注解的一致性有断言
    Given 22 个工具的 description 与 annotations (契约快照)
    When  跑一致性检查
    Then  三类措辞与对应 hint 全部吻合
    And   故意改坏一个注解会让检查变红 (注入式验证, 见下)

Why these are guards and not review steps: the guide is the normative source for
the tool surface, and it is prose. Prose drifts silently, and both drifts here
had already been read past many times -- AC-55's number was wrong in the
*under*-promising direction (the guide said 50, the tools returned 20, so a
reader tuning for cost had the wrong baseline) and AC-56's duplicate "七、" made
every "第七节" cross-reference undecidable, including one in an accepted ADR
that cannot be edited.

The AC-57 predicates are biconditionals on purpose, and they were derived from
the snapshot rather than asserted from the rule: the first pass found two tools
(`set_plugin_status`, `submit_items`) annotated `open_world_hint=true` whose
descriptions never said why. That is the guide's own rule violated -- the
annotation claims an external side effect the description does not mention --
and the fix was the description, not the predicate.

Falsification for AC-57: flipping `destructive_hint` to true on `get_item` in
the snapshot turns it red (a read tool claiming irreversibility). Restoring the
snapshot turns it green. AC-55 and AC-56 are their own injection: they are red
against the guide as it stood before this change.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs/api/mcp-tool-guide.md"
SNAPSHOT = REPO_ROOT / "tests/contract/mcp_tools.json"

CN_NUMERALS = "一二三四五六七八九十"

# The three side-effect claims a description can make, and the annotation each
# one pins. Kept next to each other because they are one rule ("the annotation
# is the machine-readable half of the description"), not three rules.
CONFIRMATION_CLAIM = r"requires user confirmation|requires confirmation"
IRREVERSIBLE_CLAIM = r"permanent|not reversible|irreversible|cannot be undone"
# "origin" alone is deliberately absent: `list_runs` says error text is
# "origin-side", which is about the *shape* of a string, not a request. A
# predicate loose enough to catch that would have to be loosened further to
# excuse it, and then it would stop catching anything.
EXTERNAL_CLAIM = (
    r"outbound|third party|real request|real load on the origin"
    r"|provider budget|provider charges|spends provider|external request|fetches"
)


def _guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _mcp_limit_defaults() -> set[int]:
    """Every `limit: int = N` default among the MCP tools, read from the AST.

    The AST rather than a regex because the signatures are both one-line and
    multi-line, and a regex that handled both would also match a `limit`
    mentioned in a docstring.
    """
    defaults: set[int] = set()
    for path in sorted((REPO_ROOT / "src/app/mcp/tools").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            positional = [*node.args.posonlyargs, *node.args.args]
            paired = zip(
                positional[len(positional) - len(node.args.defaults) :],
                node.args.defaults,
                strict=False,
            )
            for argument, default in [
                *paired,
                *zip(node.args.kwonlyargs, node.args.kw_defaults, strict=False),
            ]:
                if (
                    argument.arg == "limit"
                    and isinstance(default, ast.Constant)
                    and isinstance(default.value, int)
                ):
                    defaults.add(default.value)
    return defaults


def _core_api_limit_defaults() -> set[int]:
    """Every `limit: int = Query(default=N, ...)` among the `/v1` list routes."""
    defaults: set[int] = set()
    for path in sorted((REPO_ROOT / "src/app").glob("*_routes.py")):
        text = path.read_text(encoding="utf-8")
        defaults.update(
            int(value)
            for value in re.findall(r"limit: int = Query\(default=(\d+)", text)
        )
    return defaults


def _annotations() -> dict[str, dict[str, bool]]:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return {name: tool["annotations"] for name, tool in snapshot.items()}


def _descriptions() -> dict[str, str]:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return {name: tool["description"] for name, tool in snapshot.items()}


# --------------------------------------------------------------------------
# AC-55: the numbers in the guide are the numbers in the code
# --------------------------------------------------------------------------


def test_the_guide_states_the_limit_defaults_the_code_uses() -> None:
    """AC-55: a reader tuning for cost must not be given the wrong baseline.

    The guide said "默认 limit=50 ... 与 /v1 一致" while every MCP tool
    defaulted to 20 and `/v1` to 50 -- so both halves were wrong, and the
    "consistent with /v1" claim was the wrong one in the dangerous direction:
    it told the reader the tool surface was as wide as the API.
    """
    guide = _guide_text()
    stated_mcp = re.search(r"MCP 工具默认 `limit=(\d+)`", guide)
    stated_api = re.search(r"`/v1` 默认 `limit=(\d+)`", guide)
    assert stated_mcp is not None, (
        "the guide no longer states the MCP default limit in the form this "
        "guard reads: 'MCP 工具默认 `limit=N`'"
    )
    assert stated_api is not None, (
        "the guide no longer states the /v1 default limit in the form this "
        "guard reads: '`/v1` 默认 `limit=N`'"
    )

    mcp_defaults = _mcp_limit_defaults()
    api_defaults = _core_api_limit_defaults()
    assert mcp_defaults, "no MCP tool declares a limit default to compare against"
    assert api_defaults, "no /v1 route declares a limit default to compare against"

    assert mcp_defaults == {int(stated_mcp.group(1))}, (
        f"the guide says the MCP tools default to {stated_mcp.group(1)}, but "
        f"they default to {sorted(mcp_defaults)}; a per-tool default that "
        "differs from the documented one is the drift this checks"
    )
    assert api_defaults == {int(stated_api.group(1))}, (
        f"the guide says /v1 defaults to {stated_api.group(1)}, but its list "
        f"routes default to {sorted(api_defaults)}"
    )


def test_the_search_tool_says_which_parameter_searches_what() -> None:
    """Issue #70: `q` searched titles while the description implied everything.

    A model that reads "Search collected items" and passes `q` gets title
    matches only, and nothing in the answer says the body was never looked at.
    The description has to draw the line itself, and name the parameter that
    crosses it.
    """
    text = _descriptions()["search_items"].lower()
    assert "title only" in text, text
    assert "full_text" in text, text
    assert "bodies" in text, text


# --------------------------------------------------------------------------
# AC-56: the guide's own numbering is navigable
# --------------------------------------------------------------------------


def test_the_guide_section_numbers_are_unique_and_increasing() -> None:
    """AC-56: a duplicate number makes every "第 N 节" reference undecidable.

    The guide had two "## 七、" sections, so "第七节" named either "命名与描述"
    or "订阅归类 (folder)". ADR 0010 cites "mcp-tool-guide 第八节" for the
    actor-attribution discussion, which is in "传输与挂载" -- so the duplicate
    did not just look untidy, it made a live cross-reference unresolvable.
    """
    numbers = [
        CN_NUMERALS.index(match.group(1)) + 1
        for line in _guide_text().splitlines()
        if (match := re.match(r"## ([一二三四五六七八九十]+)、", line))
    ]
    assert numbers, "no numbered sections found in the guide"

    duplicated = [n for n, count in Counter(numbers).items() if count > 1]
    assert not duplicated, (
        f"the guide numbers a section twice: {duplicated}. "
        f"Section numbers seen: {numbers}. Every cross-reference to a "
        "duplicated number is undecidable, and the later sections have to "
        "shift when the duplicate is fixed."
    )
    assert numbers == sorted(numbers), (
        f"the guide's section numbers are out of order: {numbers}"
    )


# --------------------------------------------------------------------------
# AC-57: an annotation is the machine-readable half of the description
# --------------------------------------------------------------------------


def test_every_tool_annotation_matches_what_its_description_claims() -> None:
    """AC-57: the three claims a description makes pin the three hints.

    The annotation is what a host reads to decide whether to add its own
    confirmation, and the description is what the model reads to choose the
    tool. If they disagree, one of the two audiences is being told a
    side-effect that does not exist -- which is why the guide puts both in the
    same contract and why this is a guard rather than a review step.
    """
    annotations = _annotations()
    descriptions = _descriptions()
    problems: list[str] = []

    for name in sorted(annotations):
        hints = annotations[name]
        description = descriptions[name]

        claims_confirmation = bool(re.search(CONFIRMATION_CLAIM, description, re.I))
        if claims_confirmation != (hints["read_only_hint"] is False):
            problems.append(
                f"{name}: read_only_hint={hints['read_only_hint']} but the "
                f"description {'does' if claims_confirmation else 'does not'} "
                "ask for confirmation"
            )

        claims_irreversible = bool(re.search(IRREVERSIBLE_CLAIM, description, re.I))
        if claims_irreversible != hints["destructive_hint"]:
            problems.append(
                f"{name}: destructive_hint={hints['destructive_hint']} but the "
                f"description {'does' if claims_irreversible else 'does not'} "
                "state an irreversible consequence"
            )

        claims_external = bool(re.search(EXTERNAL_CLAIM, description, re.I))
        if claims_external != hints["open_world_hint"]:
            problems.append(
                f"{name}: open_world_hint={hints['open_world_hint']} but the "
                f"description {'does' if claims_external else 'does not'} "
                "describe an external side effect (outbound request or provider "
                "spend)"
            )

    assert not problems, (
        "tool descriptions and their annotations disagree, so one of the two "
        "audiences is being told about a side effect that does not exist:\n  "
        + "\n  ".join(problems)
    )
