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
    Given 工具面上每个工具的 description 与 annotations (契约快照)
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
    """AC-58 (issue #70): `q` searched titles while the description implied everything.

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
# 14,000 was what the surface measured after the pass that moved cross-tool
# guidance into the server instructions. 15,000 added the room the batch tool
# needed (ADR 0044, issue #162): a second trim pass cut 423 characters from the
# nine largest descriptions, leaving 13,543, and `add_sources` is expected to
# measure about 900 -- so the surface lands near 14,450 and 15,000 leaves ~550.
#
# 16,000 adds `fetch_source_items` (issue #171): the address-fetching tool, and
# its description has to keep the "addresses vs already-fetched content"
# distinction that decides whether a model reaches for this tool or for
# `submit_items` -- reaching for the wrong one either asks the site for
# nothing or asks a stranger for something twice. Its first draft measured
# 1,128 characters and asked for a deliberate raise; the trimmed version
# measures 922 and the surface lands at 15,689, so 16,000 leaves the next tool
# the same ~300 to argue for in a diff.
#
# The number matters less than the fact that growth has to be argued for in a
# diff. It is deliberately not 8,000: cutting that far means deleting the
# selection guidance -- "use delete_source_items to clear a bad crawl",
# "set_source_status instead" -- that keeps a model from reaching for the
# irreversible tool.
# 17,000 adds the read side of the work mechanism (issue #191): `get_work` and
# `work_units`, the pair that reads the handle the first handle-returning write
# tool answers with. A tool that answers with a handle and no way to read it is
# a capability removed rather than a response shape changed: the per-address
# results the submit tool used to inline had to land somewhere reachable, and
# those two tools cost 883 characters together. Trimming first cut the
# descriptions this change touched (fetch_source_items 622 -> 597, get_work
# 223 -> 183, work_units 207), so the raise is the pair itself and not prose;
# the surface lands at 16,667 and 17,000 leaves the next tool the same ~300 to
# argue for. #197/#199 (the plural kinds) revisit this number in their own diff:
# they move six macro tools onto the same mechanism, and whether that shrinks
# the face depends on how many readers they need beside it.
# 17,200 adds the two read primitives of issue #193: `search_items` gained a
# `url` parameter (exact match on an item's canonical url) and the queue summary
# went into `diagnose_source`'s existing backlog block. That is one parameter
# and no new tool, which is the shape this budget is trying to encourage: a new
# read tool would have cost ~400 characters, and the queue's four counts plus a
# derived rate ride along in an answer the tool already gives. The surface lands
# at 16,882 and 17,200 leaves the next tool the same ~300 to argue for.
#
# #195 spent most of that: `retry_enrichments` gained `enrichment_id` beside
# `source_id` (+134, to 17,016) instead of a second tool for the same intent at
# a different granularity, which is the trade this budget is for. What is left
# (~180) is not enough for a *new* tool: #196 added the read-state atoms to /v1
# without touching this surface, so whoever exposes them here (or adds the next
# tool) has to trim descriptions or argue the number up in that diff.
#
# #199 (A group) spent most of it: `delete_items`, `add_sources` and
# `submit_items` answer with a work handle now and point at `work_units`, which
# is the price of collapsing their responses to O(1) -- the descriptions have to
# say where the per-item results went. Rewriting them rather than padding cut 237
# characters, so the surface lands at 17,100 and 17,400 leaves the next tool the
# same ~300 to argue for. The B group (selection-shaped commands, #199) measures
# this again in its own diff.
#
# #199's B group (17,400 -> 17,500) converted five more tools -- `delete_items`,
# `add_sources`, `submit_items`, `backfill_source`, `delete_source_items` -- to
# answer with a handle, and every one of their descriptions has to say where the
# per-item results went. A trim pass cut ~100 characters back (the four largest
# lost only clauses that repeated something else in the same sentence), so the
# surface lands at 17,230. Trimming further would start deleting *selection*
# guidance -- which parameter searches what, when to reach for this tool rather
# than that one -- and that guidance is the thing this budget exists to protect,
# not to spend.
#
# 18,000 is the first raise that pays for a *new* tool rather than for keeping
# one (issue #218, consumption state). The read half cost nothing: `get_item`
# folds in `is_read`/`is_starred` from the /v1 atom, which is the shape this
# budget rewards -- "what is this item" and "what is my state with it" are one
# question, so they are one call and no new read tool (the same move as #193).
# The write half could not be folded: a write cannot live in a read tool, since
# the annotations and the confirmation differ and mixing them in would make
# `get_item` elicit. So it is a 29th tool, `set_item_read_state`, measured at 623
# characters (name + description + schema).
#
# A trim pass before the raise cut 197 characters from the descriptions this
# change touches (`submit_items`, `set_source_pipeline_chain`, `add_source`,
# `search_items`) and from the new one; every cut removed a clause that restated
# something else in the same description, none of them selection guidance. That
# leaves 236 to argue for. The one remaining lever was moving the "read the
# per-target outcomes with work_units" clause -- identical in seven
# handle-returning tools, ~350 characters together -- into the server
# instructions. It would fit the number without raising it, and it is not
# dishonest; but it *moves* the text rather than removing it, and the
# instructions are carried by every conversation too. A budget whose point is to
# bound the always-present surface should not be balanced by relocating text
# into another always-present surface, so the raise is the honest entry. The
# surface lands at 17,736 and 18,000 leaves the next tool the same ~260 to argue
# for in its own diff.
SURFACE_BUDGET_CHARS = 18_000


def test_the_tool_face_stays_within_its_budget() -> None:
    """AC-66 / AC-166 / AC-212: the bound is the deliverable, so it is a test.

    AC-212 (issue #199) is the same measurement applied to a change that
    collapsed three tools' responses: the surface may not grow past the bound
    while the tools start answering with handles.

    AC-71 (the #74 boundary): moving the protocol-field guidance into the
    instructions kept the surface from growing; this is the measurement.
    """
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
    """AC-70 (issue #74): the shell looks like waste, and deleting it costs something.

    The chosen route keeps the permissive schema (it is what makes the SDK emit
    `structuredContent`) and puts the protocol fields in the instructions; the
    guide has to state that reason, or the next reader removes the schema.

    A permissive outputSchema is what makes the SDK emit `structuredContent` at
    all, so "this schema carries no information" is an invitation to remove it
    and take structured output with it. The reason has to be written down
    somewhere a reader finds it.
    """
    text = _guide_text()
    assert "additionalProperties" in text, text[:200]
    assert "structuredContent" in text


def test_the_guide_records_the_naming_decision() -> None:
    """AC-72..74 (issue #75): not prefixing is a decision, and the trigger to revisit
    is named -- the disposition recorded in `SCENARIOS.md` is 已明确不做.

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
    """AC-57 / AC-266: the side-effect claims a description makes pin the hints.

    The annotation is what a host reads to decide what to surface, and the
    description is what the model reads to choose the tool. If they disagree,
    one of the two audiences is being told a side effect that does not exist --
    which is why the guide puts both in the same contract and why this is a
    guard rather than a review step. Since #270 this is also what carries the
    "this is irreversible" warning the retired confirmation dialog used to
    deliver by hand.
    """
    annotations = _annotations()
    descriptions = _descriptions()
    problems: list[str] = []

    for name in sorted(annotations):
        hints = annotations[name]
        description = descriptions[name]

        # No description may promise a confirmation any more (ADR 0058, #270):
        # the machinery is retired, so a description that still offered one
        # would describe an interaction the server does not have. The
        # write/read split is `idempotent_hint` -- only a write mints a fresh
        # idempotency key per call -- and `read_only_hint` must agree with it.
        claims_confirmation = bool(re.search(CONFIRMATION_CLAIM, description, re.I))
        if claims_confirmation:
            problems.append(
                f"{name}: the description still asks for a confirmation, which "
                "the server no longer performs (ADR 0058)"
            )

        if (hints["read_only_hint"] is False) != (hints["idempotent_hint"] is False):
            problems.append(
                f"{name}: read_only_hint={hints['read_only_hint']} disagrees with "
                f"idempotent_hint={hints['idempotent_hint']}; the write tools are "
                "exactly the ones that mint a fresh idempotency key per call"
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
    through `_write(..., method=..., path=...)`. A first version looked
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

    # `_write(core, method="PATCH", path=f"...")` -- every write tool,
    # and the only shape that names both parts as literals at the call site.
    if name == "_write":
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


# --------------------------------------------------------------------------
# `next_actions` are pointers, so they must point at a registered tool
# --------------------------------------------------------------------------
#
# Issue #75 proposed this guard alongside its naming question; the renaming was
# declined (guide section 7) but the guard is independent of it, and it stayed
# unimplemented -- four hardcoded lists, only ever spot-checked. Issue #357 is
# the audit's finding of that gap. It is a mechanical property, so it takes no
# AC number (the #87 precedent).


def _registered_tool_names() -> set[str]:
    return set(json.loads(SNAPSHOT.read_text(encoding="utf-8")))


def _next_action_lists() -> dict[str, list[str]]:
    """Every literal `next_actions` list in the MCP surface, by source location.

    Read from the AST rather than by regex so a renamed key or a reshaped value
    is not silently skipped: a scan that matches nothing would pass vacuously,
    which is the failure mode this file exists to prevent. The scan asserts
    something was found, and that each element is a plain string literal.

    Only literals are reachable this way. If a future `next_actions` is built at
    runtime, the `found` assertion below will not notice -- but the dictionaries
    are built from literals today, and the test says so rather than implying a
    completeness it does not have.
    """
    found: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "src/app/mcp").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if not (isinstance(key, ast.Constant) and key.value == "next_actions"):
                    continue
                assert isinstance(value, ast.List), (path, node.lineno)
                names: list[str] = []
                for element in value.elts:
                    assert isinstance(element, ast.Constant) and isinstance(
                        element.value, str
                    ), (path, node.lineno)
                    names.append(element.value)
                found[f"{path.relative_to(REPO_ROOT)}:{node.lineno}"] = names
    return found


def test_every_next_action_names_a_registered_tool() -> None:
    """A model follows `next_actions` straight back into `tools/call`.

    A stale name is an instruction to call a tool that does not exist, and the
    failure is silent to us: the model gets `unknown tool` rather than a server
    error. The four lists are hardcoded, so nothing but this guard connects them
    to the registry.
    """
    actions = _next_action_lists()
    assert actions, "no `next_actions` literals found -- the AST scan stopped matching"

    registered = _registered_tool_names()
    unknown = {
        location: [name for name in names if name not in registered]
        for location, names in actions.items()
    }
    unknown = {location: names for location, names in unknown.items() if names}
    assert not unknown, (
        f"next_actions point at tools that are not registered: {unknown}; "
        f"registered: {sorted(registered)}"
    )


def test_the_write_tools_are_named_for_what_they_act_on() -> None:
    """AC-341 / AC-342: the write face speaks subscription and origin.

    ADR 0046's split made policy and facts two tables; the tool names were the
    last place still calling both "source". Pinned here rather than left to the
    snapshot diff, because a rename that leaves the old name registered -- an
    alias, or a half-finished edit -- would still read as "expected renames" in
    a diff review, and two spellings for one intent is what the model would
    then have to guess between.
    """
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    renamed = {
        "add_source": "add_subscription",
        "add_sources": "add_subscriptions",
        "remove_source": "remove_subscription",
        "set_source_status": "set_subscription_status",
        "set_source_folder": "set_subscription_folder",
        "set_source_pipeline_chain": "set_subscription_pipeline_chain",
        "update_schedule_policy": "update_subscription_policy",
        "fetch_source_items": "fetch_origin_items",
        "delete_source_items": "delete_origin_items",
        "backfill_source": "backfill_subscription_chain",
    }
    missing = sorted(new for new in renamed.values() if new not in snapshot)
    assert not missing, f"these renamed tools are not registered: {missing}"

    stale = sorted(old for old in renamed if old in snapshot)
    assert not stale, (
        f"the retired names are still registered: {stale}. The write face must "
        "not carry both spellings."
    )
