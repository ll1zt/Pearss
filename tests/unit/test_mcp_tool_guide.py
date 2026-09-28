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
# AC-66 / AC-67: the standing surface has a bound, and a task has one tool
# --------------------------------------------------------------------------

# Names + descriptions + input schemas: the part of the tool face that is in
# every conversation whether the tools are used or not. A *budget* rather than a
# measurement, because a number nobody enforces is how this grew past 3,500
# tokens in the first place (AC-66).
#
# 14,000 is what the surface measured after the pass that moved cross-tool
# guidance into the server instructions. The number matters less than the fact
# that growth now has to be argued for in a diff. It is deliberately not 8,000:
# cutting that far means deleting the selection guidance -- "use
# delete_source_items to clear a bad crawl", "set_source_status instead" -- that
# keeps a model from reaching for the irreversible tool.
SURFACE_BUDGET_CHARS = 14_000


def test_the_tool_face_stays_within_its_budget() -> None:
    """AC-66: the bound is the deliverable, so it is a test, not a paragraph."""
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    sizes = {
        name: len(name)
        + len(tool["description"])
        + len(json.dumps(tool.get("input_schema", {}), ensure_ascii=False))
        for name, tool in snapshot.items()
    }
    biggest = sorted(sizes.items(), key=lambda pair: -pair[1])[:5]
    assert sum(sizes.values()) <= SURFACE_BUDGET_CHARS, (
        f"the tool face is {sum(sizes.values())} characters against a "
        f"{SURFACE_BUDGET_CHARS} budget, and every conversation carries it "
        f"whether or not these tools are used. Largest contributors: {biggest}. "
        "Tighten them, move the shared part into the server instructions, or "
        "raise the budget deliberately in this file."
    )


def test_a_task_level_tool_exists_for_the_question_that_needed_one() -> None:
    """AC-67: a question always answered by the same several calls gets one tool.

    The proof that `diagnose_source` is that tool rather than another projection
    is that its description can name the calls it replaces. If it could not name
    them, it would be a projection with a longer name.
    """
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    description = snapshot["diagnose_source"]["description"]
    for replaced in ("list_jobs", "list_runs", "list_enrichments"):
        assert replaced in description, (replaced, description)


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


def test_the_guide_records_why_the_output_schema_is_permissive() -> None:
    """Issue #74: the shell looks like waste, and deleting it costs something.

    A permissive outputSchema is what makes the SDK emit `structuredContent` at
    all, so "this schema carries no information" is an invitation to remove it
    and take structured output with it. The reason has to be written down
    somewhere a reader finds it.
    """
    text = _guide_text()
    assert "additionalProperties" in text, text[:200]
    assert "structuredContent" in text


def test_the_guide_records_the_naming_decision() -> None:
    """Issue #75: not prefixing is a decision, and the trigger to revisit is named.

    "There is no prefix" reads as an oversight; the measurement that makes it
    deliberate -- the host namespaces by server, so the client sees
    `pearss_list_sources` either way -- is what stops the next reader from
    "fixing" it.
    """
    text = _guide_text()
    assert "不带前缀" in text
    assert "触发条件" in text


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


# --------------------------------------------------------------------------
# ADR 0036's reverse constraint: every tool lands on an endpoint that exists
# --------------------------------------------------------------------------


def _endpoint_calls() -> dict[str, set[tuple[str, str]]]:
    """The (method, path) each MCP tool function calls, as route templates.

    Read from the source rather than from a list here, because the constraint is
    about the code: ADR 0036 allows one endpoint to back several tools, and
    requires the other direction -- a tool may not call an operation `/v1` does
    not have. That is exactly the kind of statement a list can agree with while
    the code does not.

    Two shapes count, and the second one is why this had to be checked rather
    than assumed: a read calls `core.get(...)` directly, while every write goes
    through `_guarded_write(..., method=..., path=...)`. A first version looked
    only for `core.<verb>` and silently saw none of the thirteen write tools --
    found by injecting a nonexistent endpoint into `trigger_job` and watching the
    guard stay green.
    """
    calls: dict[str, set[tuple[str, str]]] = {}
    tools_dir = REPO_ROOT / "src/app/mcp/tools"
    for path in sorted(tools_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, owner: str) -> None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                owner = node.name
            if isinstance(node, ast.Call):
                found = _endpoint_of(node)
                if found is not None:
                    calls.setdefault(owner, set()).add(found)
            for child in ast.iter_child_nodes(node):
                visit(child, owner)

        visit(tree, path.stem)

    return {name: found for name, found in calls.items() if found}


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _endpoint_of(call: ast.Call) -> tuple[str, str] | None:
    """The (method, path template) of an endpoint call, or None."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

    # `_guarded_write(core, method="PATCH", path=f"...")` -- every write tool,
    # and the only shape that names both parts as literals at the call site.
    if name == "_guarded_write":
        method = _keyword(call, "method")
        path = _keyword(call, "path")
        if isinstance(method, ast.Constant) and isinstance(method.value, str):
            rendered = _render_path(path) if path is not None else None
            if rendered is not None:
                return (method.value.upper(), rendered)
        return None

    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    if func.value.id != "core":
        return None

    if func.attr == "request":
        # `core.request(method, path, ...)`. Only a literal method is usable
        # here: the one call with variables is inside `_guarded_write` itself,
        # and its concrete pairs come from that helper's call sites.
        if len(call.args) < 2 or not isinstance(call.args[0], ast.Constant):
            return None
        if not isinstance(call.args[0].value, str):
            return None
        rendered = _render_path(call.args[1])
        if rendered is None:
            return None
        return (call.args[0].value.upper(), rendered)

    if func.attr in {"get", "post", "patch", "delete"}:
        if not call.args:
            return None
        rendered = _render_path(call.args[0])
        if rendered is None:
            return None
        return (func.attr.upper(), rendered)

    return None


def _render_path(node: ast.expr) -> str | None:
    """An f-string path as the template OpenAPI uses: `{name}` for each hole."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        rendered = ""
        for part in node.values:
            if isinstance(part, ast.Constant):
                rendered += str(part.value)
            elif isinstance(part, ast.FormattedValue):
                rendered += "{" + ast.unparse(part.value) + "}"
        return rendered
    return None


# One tool assembles its path from literals at the call site: `set_plugin_status`
# picks between `:enable` and `:disable` by argument, so the f-string has a hole
# outside a parameter position and there is no static template to compare. Named
# rather than pattern-skipped, so the exemption is visible and the next one has
# to be justified; the prefix is still checked above.
_ASSEMBLED_PATHS = {"set_plugin_status"}


def test_every_tool_calls_an_endpoint_that_exists() -> None:
    """ADR 0036, reverse direction (issue #71, AC-62).

    The ADR allows an endpoint to back several tools; it does not allow a tool
    to call an operation `/v1` does not have. That is the half that keeps the
    tool face a *projection* rather than a second implementation, and it is
    checkable: every endpoint a tool calls has to be a method and path pair the
    committed OpenAPI document declares.
    """
    spec = json.loads(
        (REPO_ROOT / "tests/contract/openapi.json").read_text(encoding="utf-8")
    )
    known = {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
    }
    calls = _endpoint_calls()
    assert calls, "no endpoint calls found; is the tools layout intact?"
    # Both halves of the surface have to be represented, or the guard is only
    # watching part of it.
    assert len(calls) >= 20, sorted(calls)

    resolved = {
        (tool, method, path) for tool, found in calls.items() for method, path in found
    }
    unresolved = {
        (tool, method, path)
        for tool, method, path in resolved
        if (method, path) not in known
    }

    unknown = sorted(
        f"{tool} -> {method} {path}"
        for tool, method, path in unresolved
        if tool not in _ASSEMBLED_PATHS
    )
    assert not unknown, (
        "these tools call /v1 operations the OpenAPI document does not declare, "
        f"so they are not projections of anything: {unknown}"
    )

    # An exemption that stops hiding anything is either a fixed bug or a guard
    # that went blind, and both are worth failing over.
    idle = sorted(_ASSEMBLED_PATHS - {tool for tool, _m, _p in unresolved})
    assert not idle, f"exemptions that now hide nothing: {idle}"

    # Exemptions still have to be anchored: the static part of an assembled path
    # must have a declared endpoint in front of it, so a typo in the prefix
    # fails even though the suffix cannot be resolved.
    declared_paths = sorted(spec["paths"])
    for tool in sorted(_ASSEMBLED_PATHS):
        for _method, path in sorted(calls[tool]):
            prefix = path.split("{", 1)[0]
            assert any(declared.startswith(prefix) for declared in declared_paths), (
                f"{tool}'s assembled path {path!r} has no declared endpoint "
                f"under {prefix!r}"
            )
