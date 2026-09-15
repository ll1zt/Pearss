"""Elicitation: the confirmation machinery behind every MCP write.

Presentation rule (docs/api/mcp-tool-guide.md): every write elicits first, and
the confirmed arguments must be identical to the executed ones. Three pieces
carry that rule:

- `Confirmation` and `_confirmation_schema` mirror the real arguments into the
  dialog, so the user confirms the actual request rather than an abstraction
- `_matches` / `_render` / `_comma_join` make "identical" checkable -- the
  canonical forms of confirmed vs executed values are compared, with the
  string/boolean coercions that keep every confirmation of a plain string or
  a flag from failing spuriously
- `_confirmation_verdict` reads the client's answer and reduces it to the one
  decision every write needs: run, or not
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

from mcp.server.mcpserver.resolve import Elicit
from pydantic import BaseModel, Field, create_model

from app.mcp.core_client import _infer_source_type
from core.folders import DEFAULT_FOLDER

LOGGER = logging.getLogger(__name__)


class Confirmation(BaseModel):
    """The elicited acknowledgement for a write.

    The fields mirror the real arguments so the confirmation is of the same
    request that gets executed. A confirmation that merely says "are you sure"
    while executing different values is a rubber stamp, not a check.
    """

    confirmed: bool = Field(
        default=False,
        description="Set true to proceed. Anything else cancels the operation.",
    )


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


def _confirmation_schema(fields: dict[str, Any]) -> type[Confirmation]:
    """Build an elicitation schema prefilled with the values about to run.

    Elicitation sends a *schema*, not values: the client returns whatever the
    user filled in. Seeding each field with the real argument means the user
    confirms or edits the actual request rather than retyping it, and the
    `_matches` check downstream still refuses anything that drifted.

    Fields are primitives only. MCP renders an elicitation schema from primitive
    types, and a nested object there raises TypeError at render time — which is
    why a schedule policy travels as its canonical JSON text.
    """
    # `confirmed` is prefilled true so that accepting the dialog without
    # editing means proceed, and unchecking it means decline. Left at the
    # base's default of false, a client that echoes the schema back — which is
    # what "the user just pressed accept" looks like — would always be read as
    # a refusal.
    schema: dict[str, Any] = {
        "confirmed": (bool, Field(default=True)),
        **{
            name: ((str if isinstance(value, str) else bool), Field(default=value))
            for name, value in fields.items()
        },
    }
    # Cast because mypy resolves `create_model`'s overloads against literal
    # keyword arguments and cannot see through `**schema`.
    return cast(
        "type[Confirmation]",
        create_model("WriteConfirmation", __base__=Confirmation, **schema),
    )


def _asked(model: type[Confirmation], message: str) -> Elicit[Any]:
    """The confirmation question, as a resolver result.

    Returned from a `Resolve`d parameter so the framework decides how to ask:
    on the stateless protocol it batches the question into an
    `InputRequiredResult` and the client retries with the answer; on the older
    handshake protocol it sends a server-initiated request mid-call. Writing
    that branching ourselves would be the same code, worse -- the SDK documents
    this marker as the supported shape.

    A resolver may run more than once per call (it re-runs on the retry round),
    so the message must derive only from the tool's arguments, which the
    framework passes to it by name. That is the reason each resolver below is a
    small function of the arguments rather than a closure over local state.
    """
    return Elicit(message, model)


def _confirm_resolver(**kwargs: Any) -> Elicit[Any]:
    """Ask for confirmation of one write, with the arguments prefilled.

    Every write tool's confirmation runs through this: the message names the
    write and the schema carries the values about to execute, so the user
    confirms the actual request rather than retyping it. `_matches` still
    refuses anything that drifted, which is what makes the dialog a
    confirmation rather than a formality.

    `_summary` and `_fields` are computed by each tool's own resolver from the
    tool arguments; they are popped here so they never reach the schema.
    """
    summary = kwargs.pop("_summary")
    fields = kwargs.pop("_fields")
    return _asked(_confirmation_schema(fields), summary)


def _add_source_confirmation(
    url: str,
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
) -> Elicit[Any]:
    """The add_source question: names the source and prefills every argument."""
    requested = type.strip().lower()
    source_type = _infer_source_type(url) if requested == "auto" else requested
    return _confirm_resolver(
        _summary=(
            f"Add {url} as a {source_type} source with policy "
            f"{_render(schedule_policy)}, chain {_render(pipeline_chain)} "
            f"(lang {target_lang or 'none'}) and folder "
            f"{folder or DEFAULT_FOLDER}?"
        ),
        _fields={
            "url": url,
            "type": source_type,
            "schedule_policy": _render(schedule_policy),
            "pipeline_chain": _render(pipeline_chain),
            "target_lang": target_lang,
            "folder": folder,
        },
    )


def _update_schedule_policy_confirmation(
    source_id: str, schedule_policy: dict[str, Any]
) -> Elicit[Any]:
    return _confirm_resolver(
        _summary=f"Set source {source_id} schedule to {_render(schedule_policy)}?",
        _fields={
            "source_id": source_id,
            "schedule_policy": _render(schedule_policy),
        },
    )


def _set_source_status_confirmation(source_id: str, status: str) -> Elicit[Any]:
    return _confirm_resolver(
        _summary=f"Set source {source_id} status to {status}?",
        _fields={"source_id": source_id, "status": status},
    )


def _set_source_folder_confirmation(source_id: str, folder: str) -> Elicit[Any]:
    return _confirm_resolver(
        _summary=f"Move source {source_id} to folder {folder}?",
        _fields={"source_id": source_id, "folder": folder},
    )


def _trigger_job_confirmation(source_id: str) -> Elicit[Any]:
    return _confirm_resolver(
        _summary=f"Fetch source {source_id} now?",
        _fields={"source_id": source_id},
    )


def _backfill_source_confirmation(source_id: str) -> Elicit[Any]:
    return _confirm_resolver(
        _summary=f"Backfill processing products for source {source_id}?",
        _fields={"source_id": source_id},
    )


def _retry_enrichments_confirmation(source_id: str) -> Elicit[Any]:
    return _confirm_resolver(
        _summary=(f"Requeue failed processing-chain products for source {source_id}?"),
        _fields={"source_id": source_id},
    )


def _submit_items_confirmation(
    source_id: str, items: list[dict[str, Any]]
) -> Elicit[Any]:
    """The submit_items question: names the target and what will land in it.

    Item titles are listed rather than bodies: a prompt the user cannot read
    is not a confirmation, and a body can be arbitrarily long. The count is
    carried separately so a truncated list is still an honest one.
    """
    titles = "; ".join(
        str(item.get("title") or "(untitled)")[:60] for item in items[:5]
    )
    more = "" if len(items) <= 5 else f" (+{len(items) - 5} more)"
    return _confirm_resolver(
        _summary=(
            f"Push {len(items)} item(s) into agent source {source_id}? {titles}{more}"
        ),
        _fields={
            "source_id": source_id,
            "item_count": str(len(items)),
            "titles": f"{titles}{more}",
        },
    )


def _set_plugin_status_confirmation(plugin_id: str, status: str) -> Elicit[Any]:
    return _confirm_resolver(
        _summary=f"Set plugin {plugin_id} status to {status}?",
        _fields={"plugin_id": plugin_id, "status": status},
    )


def _remove_source_confirmation(source_id: str) -> Elicit[Any]:
    """The source-deletion question.

    Every other confirmation here can be "do X?" with the arguments prefilled,
    because the arguments describe the effect. This one cannot: `source_id`
    says nothing about how much content it takes with it, so the message has
    to state the consequence in words. The count is deliberately not fetched
    here -- a resolver runs before the tool body and must stay a pure function
    of what the client sent, and an async resolver defined in the factory
    cannot be resolved from this module-level annotation at all (the SDK
    evaluates the annotation string against module globals).
    """
    return _confirm_resolver(
        _summary=(
            f"Delete source {source_id} and every item it collected? Their "
            "stored bodies, read state and translations are removed too; "
            "clients keep what they have already cached, and re-subscribing "
            "the same feed starts over with no history. This cannot be undone."
        ),
        _fields={"source_id": source_id},
    )


def _delete_items_confirmation(item_ids: list[str]) -> Elicit[Any]:
    listed = ", ".join(item_ids[:5])
    if len(item_ids) > 5:
        listed += f", and {len(item_ids) - 5} more"
    return _confirm_resolver(
        _summary=(
            f"Permanently delete {len(item_ids)} item(s): {listed}? Their "
            "bodies, read state and translations go too. This cannot be undone."
        ),
        _fields={"item_ids": _comma_join(item_ids)},
    )


def _delete_source_items_confirmation(
    source_id: str,
    since: str = "",
    until: str = "",
    include_published: bool = True,
) -> Elicit[Any]:
    window = ""
    if since or until:
        window = f" published between {since or 'the start'} and {until or 'now'}"
    scope = (
        "including items clients have already seen"
        if include_published
        else "excluding already-published items"
    )
    return _confirm_resolver(
        _summary=(
            f"Permanently delete source {source_id}'s items{window}, {scope}? "
            "The subscription and its crawl position are kept, so collection "
            "resumes. This cannot be undone."
        ),
        _fields={
            "source_id": source_id,
            "since": since,
            "until": until,
            "include_published": include_published,
        },
    )


def _confirmation_verdict(answer: Any) -> dict[str, Any] | None:
    """Read an elicitation outcome: the confirmed values, or None.

    `confirmed` is the explicit checkbox in the schema. A client that accepts
    the dialog but leaves it false has declined, and honouring that is the
    difference between a confirmation and a form field.

    A decline or cancel arrives as `None` (the framework unwraps the union for
    a parameter annotated with the plain model), so a falsy answer means "not
    confirmed" rather than "no answer yet". `None` is also what a client that
    cannot elicit produces -- the SDK raises before we get here in that case,
    and the tool layer reports it as a not-confirmed refusal.
    """
    if answer is None:
        return None
    if isinstance(answer, Confirmation):
        values = answer.model_dump()
    elif isinstance(answer, dict):
        values = dict(answer)
    else:
        action = getattr(answer, "action", None)
        data = getattr(answer, "data", None)
        if action != "accept" or data is None:
            LOGGER.info("write %s", action or "unanswered")
            return None
        values = data.model_dump() if hasattr(data, "model_dump") else dict(data)
    return values if values.get("confirmed") else None
