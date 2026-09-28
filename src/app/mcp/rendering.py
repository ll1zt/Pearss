"""Tool-result presentation rules (docs/api/mcp-tool-guide.md).

Compact returns, truncated strings, and no bodies in tool results (those are
Resources). These functions are the difference between a tool result that
fits the model's context and one that quietly eats it: the ceiling is the
contract, not tidiness.
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.types import ToolAnnotations

from adapters.audit import SENSITIVE_QUERY_KEYS

MAX_STRING_CHARS = 200
MAX_LIST_ITEMS = 20

# What a redacted value leaves in place of itself. The same marker the audit
# module writes, so a reader who has seen one has seen both.
_REDACTED = "<redacted>"

# Any http(s) URL inside a result string. The invariant is deliberately about
# the *value* rather than about which key holds it: enumerating keys (`url`,
# `canonical_url`, ...) has to be extended for every new field, and the field
# someone forgets is the one that leaks. A URL carrying a credential must not
# reach the model no matter what it is called or where it is nested.
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)

# Bounded because a query parameter can itself hold a URL (`?url=https://...`),
# which can in turn hold one. The bound is what makes that recursion safe; two
# levels covers every shape seen in a feed URL without accepting unbounded
# blow-up on adversarial input.
_MAX_URL_NESTING = 2

# `get_item_body` bounds itself rather than inheriting MAX_STRING_CHARS: the
# whole point of that tool is to hand over a body, so the generic shrinker would
# defeat it. These are the ceiling instead, and they exist because the tool is a
# concession to clients that cannot read Resources -- without a cap it becomes
# the easy way to blow up the context.
DEFAULT_BODY_CHARS = 4000
MAX_BODY_CHARS = 20000


def _redact_url(url: str, *, depth: int = 0) -> str:
    """Hide a URL's credentials while keeping its shape.

    Host and path stay because they identify the resource; the parameter names
    stay because "this feed carries a `token`" is not the secret, the value is.
    Dropping the whole pair -- what `adapters.audit.redact_url` does, where the
    record must not retain a private location at all -- would make a private
    feed indistinguishable from a public one here, leaving an agent no way to
    know the subscription it is reasoning about carries a credential.

    The classification of which parameters are secrets is imported rather than
    restated: two lists would drift, and the dangerous direction of drift is
    the one that stops redacting.

    A parameter whose *value* is itself a URL is redacted recursively (bounded
    by `_MAX_URL_NESTING`), because a token can hide one hop down inside a
    redirector parameter rather than at the top level.
    """
    base, sep, query = url.partition("?")
    if not sep:
        return url
    parts: list[str] = []
    for pair in query.split("&"):
        name, equals, value = pair.partition("=")
        if name.lower() in SENSITIVE_QUERY_KEYS:
            parts.append(f"{name}={_REDACTED}" if equals else name)
        elif depth < _MAX_URL_NESTING and "://" in value:
            parts.append(f"{name}={_redact_urls(value, depth=depth + 1)}")
        else:
            parts.append(pair)
    return f"{base}?{'&'.join(parts)}"


def _redact_urls(value: str, *, depth: int = 0) -> str:
    """Redact every http(s) URL in a string, whole or embedded in prose."""
    return _URL_IN_TEXT.sub(
        lambda match: _redact_url(match.group(0), depth=depth), value
    )


def _shrink(value: Any) -> Any:
    """Redact credentials, then bound strings and list lengths at any depth.

    Redaction runs before the length check so a token sitting past the truncation
    point cannot survive by being cut in half -- and the bound is applied to what
    is actually returned.
    """
    if isinstance(value, str):
        value = _redact_urls(value)
        if len(value) > MAX_STRING_CHARS:
            return f"{value[:MAX_STRING_CHARS]}…({len(value)} chars)"
        return value
    if isinstance(value, dict):
        return {key: _shrink(item) for key, item in value.items()}
    if isinstance(value, list):
        clipped = [_shrink(item) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > len(clipped):
            clipped.append({"truncated_items": len(value) - len(clipped)})
        return clipped
    return value


def _render(value: Any) -> str:
    """Canonical text form of an argument, for confirmation and comparison.

    The elicited field is a string because MCP cannot render a nested object in
    an elicitation schema. Comparing the canonical form of the executed value
    against the confirmed one is what keeps "the confirmation matched the
    execution" checkable rather than assumed.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _matches(confirmed: Any, actual: Any) -> bool:
    """Whether a confirmed value equals the value about to be executed.

    Strings compare as themselves; anything structured compares as its
    canonical JSON, which is the form the schema carries. Rendering a string
    through json.dumps would add quotes the client never sent, so every string
    comparison would fail and every write would abort.

    Booleans compare by truth value: the elicitation field is typed `bool`, so
    the client returns a real boolean, and comparing it as text ("True" against
    the JSON "true") would reject every confirmation of a flag.
    """
    if isinstance(actual, bool):
        if isinstance(confirmed, bool):
            return confirmed == actual
        return confirmed.strip().lower() in (
            ("true", "1") if actual else ("false", "0")
        )
    expected = actual if isinstance(actual, str) else _render(actual)
    return str(confirmed).strip() == expected


def _comma_join(values: list[str]) -> str:
    """Join a list into the one string an elicitation schema can carry.

    Elicitation fields are primitives only, so a list travels as text and is
    compared back in that form. The join must be deterministic or every
    confirmation of a batch would be rejected as drift.
    """
    return ",".join(values)


def _truncate(payload: dict[str, Any]) -> dict[str, Any]:
    """Bound a tool response so one long title cannot dominate the context.

    The outer signature stays concrete so callers keep their declared return
    type; the recursion is the untyped `_shrink` above. The point is a ceiling
    on what a single call puts in front of the model, not tidiness.
    """
    shrunk = _shrink(payload)
    return shrunk if isinstance(shrunk, dict) else {}


def _error_from(payload: dict[str, Any], status: int) -> dict[str, Any]:
    """Map a Core API error envelope onto the tool error shape.

    The codes are shared rather than re-derived, so an agent seeing
    `source_not_found` from a tool and from the REST API is seeing one fact.

    The values are redacted even though this path does not go through
    `_truncate`: a suggestion is prose today and may legitimately name a URL
    tomorrow, and a credential must not reach the model because the field that
    carried it happened not to be a `url` key.
    """
    detail = payload.get("detail")
    error = detail.get("error") if isinstance(detail, dict) else None
    if not isinstance(error, dict):
        return {
            "error": f"request_failed_{status}",
            "suggestion": "Retry, then check the Core API response directly.",
            "next_actions": ["get_system_snapshot"],
        }
    suggestion = error.get("suggestion")
    return {
        "error": error.get("code", "unknown"),
        "error_class": error.get("error_class"),
        "suggestion": (
            _redact_urls(suggestion) if isinstance(suggestion, str) else suggestion
        ),
        "next_actions": ["list_sources", "get_system_snapshot"],
    }


def _attributed(payload: dict[str, Any], audit_id: str | None) -> dict[str, Any]:
    """Stamp a successful write result with the audit record it produced.

    docs/api/mcp-tool-guide.md section 3: a tool result must name the audit
    entry so the operation is attributable and traceable. The id comes from the
    Core API's `X-Audit-ID` response header, not from any tool argument -- a
    caller cannot claim an audit id, only be told which one it caused.

    Absent when the Core API did not report one (an older deployment, or a path
    that is not audited). That is a real absence and is left visible rather than
    filled with a placeholder, matching how a missing `error_class` is handled.
    """
    return {**payload, **({"audit_id": audit_id} if audit_id else {})}


def read_annotations() -> ToolAnnotations:
    """The standard MCP hint set for a read-only tool.

    Declared once rather than per tool so the seven reads cannot drift apart:
    every one of them only queries the Core API and changes nothing.

    `open_world_hint=False` because the set of things a read can return is
    bounded by this deployment's own database -- it does not reach out to
    arbitrary third parties the way a collection does.
    """
    return ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )


def write_annotations(*, destructive: bool, open_world: bool) -> ToolAnnotations:
    """The hint set for a mutating tool, with the two flags that vary.

    `idempotent_hint=False` for all of them: the tools do not take a stable
    idempotency key from the caller (each call mints a fresh one), so a retried
    *call* is a new intent by construction. That is the documented design of
    `_fresh_key`, and claiming idempotency here would contradict it.

    `destructive` marks an operation whose effect cannot be undone by another
    tool call -- the deletes. `open_world` marks one that makes a real request
    to a third party or spends provider budget, which is what a host should
    surface differently from a purely local mutation.
    """
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=False,
        open_world_hint=open_world,
    )


def _html_escape(text: str) -> str:
    import html

    return html.escape(text)


def _render_health_html(snapshot: dict[str, Any]) -> str:
    """The health panel: the section 7.7 decay signals, server-rendered.

    Laid out here, once, rather than re-invented by the model on every turn --
    the division of labour the agent-view TODO names: the agent decides what
    to look at, deterministic code decides how it looks. The markup is
    self-contained on purpose (inline styles, no scripts, no external
    resources) so the sandboxed iframe needs no network and no JS, and the
    self-containment is a pinned contract, not a style choice.

    `data-*` attributes carry the exact counts so tests (and hosts) can read
    the panel structurally instead of scraping prose.
    """
    import html as _html

    def rows(mapping: dict[str, int], attribute: str) -> str:
        return "".join(
            f'<tr data-{attribute}="{_html.escape(str(key))}">'
            f"<td>{_html.escape(str(key))}</td><td>{value}</td></tr>"
            for key, value in mapping.items()
        )

    errors = snapshot.get("runs_by_error_class", {})
    items = snapshot.get("items_by_publish_status", {})
    sources = snapshot.get("sources_by_status", {})
    enrichments = snapshot.get("enrichments_by_status", {})
    rate = snapshot.get("recent_run_success_rate")
    considered = snapshot.get("recent_runs_considered", 0)

    rate_text = "no finished runs" if rate is None else f"{rate * 100:.0f}%"
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Pearss 采集健康</title>
<style>
body{{font:14px/1.5 system-ui,sans-serif;margin:16px;color:#1a1a2e}}
h1{{font-size:18px;margin:0 0 4px}} h2{{font-size:13px;margin:16px 0 4px;color:#555}}
table{{border-collapse:collapse}} td,th{{padding:2px 12px 2px 0;text-align:left}}
td:last-child{{font-variant-numeric:tabular-nums}}
.rate{{font-size:22px}}
</style></head><body>
<h1>Pearss 采集健康</h1>
<p>近 <span data-field="considered">{considered}</span> 次采集成功率
<span class="rate" data-field="success-rate">{rate_text}</span></p>
<h2>按错误类别 (runs)</h2>
<table>{rows(errors, "error")}</table>
<h2>条目发布状态 · pending 积压是腐化信号</h2>
<table>{rows(items, "status")}</table>
<h2>处理链产品状态</h2>
<table>{rows(enrichments, "enrichment")}</table>
<h2>源状态</h2>
<table>{rows(sources, "source")}</table>
</body></html>"""
