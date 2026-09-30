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
- one `_<tool>_fields(...)` per write tool is the *single* list of what that
  tool confirms and executes. Its resolver puts those fields in the dialog and
  its body hands the same dict to the guard, so the two cannot disagree -- the
  twelve hand-written comparison lists these replaced are exactly the shape
  that had missed a field (issue #67, then #76)
- `_confirmation_verdict` reads the client's answer, and `_write_verdict` turns
  it into the one decision every write needs: run, or refuse as `declined` /
  `aborted`
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, cast

from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.resolve import Elicit
from pydantic import BaseModel, Field, create_model

from app.collector_registry import infer_source_type
from core.folders import DEFAULT_FOLDER

LOGGER = logging.getLogger(__name__)

# The two things a write can answer instead of running. Spelled once, because
# they are the contract a client branches on rather than twelve tool-specific
# wordings that happen to have matched.
_DECLINED_REASON = (
    "not confirmed: the user declined, or this client does not support "
    "elicitation, which writes require"
)
_ABORTED_REASON = "confirmed arguments differ from requested"


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


def _items_fingerprint(items: list[dict[str, Any]]) -> str:
    """A stable digest of a submitted batch, in order.

    The confirmation must be *of the items that execute*, but the items can
    carry article bodies -- too large for an elicitation form, which lists
    titles instead because a prompt the user cannot read is not a
    confirmation. This is what reconciles the two: a short string that changes
    if any field of any item changes, so `_matches` can compare the batch the
    user was shown against the batch about to run without the bodies travelling
    through the dialog.

    Order is significant and deliberately preserved: the user was shown the
    titles in this order, the response reports results in this order, and a
    batch's own dedupe is first-wins -- so a permutation is a different request.
    Only the dicts *inside* each item are canonicalised (via `sort_keys`), never
    the list, which would erase exactly the distinction above.

    Titles alone are not enough, which is the measured reason this exists: two
    batches with identical titles and identical counts but different bodies
    digest differently, and comparing counts alone accepted the second batch
    (see issue #67).
    """
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _registry_from_context(ctx: Context | None) -> Any:
    """The running application's collector registry, from the MCP request.

    A confirmation resolver runs before the tool body, so it cannot read the
    core client's registry directly. The MCP request carries the ASGI scope,
    whose `app` is the FastAPI application that mounted the MCP sub-app, and
    the application's registry is on its state. `None` (a direct call with no
    request) falls back to the process default in `infer_source_type`.
    """
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    scope = getattr(request, "scope", None)
    app = scope.get("app") if isinstance(scope, dict) else None
    return getattr(getattr(app, "state", None), "collector_registry", None)


def _add_source_fields(
    url: str,
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    registry: Any = None,
) -> dict[str, Any]:
    """What add_source confirms and executes, derived in one place.

    `type` is resolved to the type that will actually be sent, so the user is
    asked about a concrete source rather than about the literal "auto".

    `registry` is the application's collector registry, passed through to the
    type inference so the confirmation and the executing request see the same
    collectors (issue #84).
    """
    requested = type.strip().lower()
    source_type = infer_source_type(url, registry) if requested == "auto" else requested
    return {
        "url": url,
        "type": source_type,
        "schedule_policy": _render(schedule_policy),
        "pipeline_chain": _render(pipeline_chain),
        "target_lang": target_lang,
        "folder": folder,
    }


def _add_source_confirmation(
    url: str,
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    ctx: Context | None = None,
) -> Elicit[Any]:
    """The add_source question: names the source and prefills every argument.

    `ctx` is injected by the SDK, and is how the "auto" type inference asks the
    running application's collector registry rather than the process default
    (issue #84). It is optional so a direct call -- the write-guard sweep that
    drives resolvers without a request -- still works, falling back to the
    default.
    """
    fields = _add_source_fields(
        url,
        schedule_policy,
        type,
        pipeline_chain,
        target_lang,
        folder,
        registry=_registry_from_context(ctx),
    )
    return _confirm_resolver(
        _summary=(
            f"Add {fields['url']} as a {fields['type']} source with policy "
            f"{fields['schedule_policy']}, chain {fields['pipeline_chain']} "
            f"(lang {fields['target_lang'] or 'none'}) and folder "
            f"{fields['folder'] or DEFAULT_FOLDER}?"
        ),
        _fields=fields,
    )


def _urls_fingerprint(urls: list[str]) -> str:
    """A stable digest of a batch's URL list, in order.

    The list cannot travel through an elicitation form (a form carries
    primitives), so the dialog shows a count and a preview and this covers the
    rest. Order is significant: the response reports results in this order and
    the preview was shown in it, so a permutation is a different request.
    """
    canonical = json.dumps(urls, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _add_sources_fields(
    urls: list[str],
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    trust_level: str = "standard",
    parser_plugin_id: str = "",
) -> dict[str, Any]:
    """What add_sources confirms and executes, derived in one place.

    `type` stays as requested -- `auto` is resolved server-side per URL
    (`/v1/sources:batch`), so the confirmed value is exactly the executed one.
    The URL list travels as count + preview + fingerprint, the same
    reconciliation `submit_items` uses for item bodies.
    """
    preview = "; ".join(urls[:5])
    more = "" if len(urls) <= 5 else f" (+{len(urls) - 5} more)"
    return {
        "count": str(len(urls)),
        "urls": f"{preview}{more}",
        "urls_fingerprint": _urls_fingerprint(urls),
        "type": type.strip().lower() or "auto",
        "schedule_policy": _render(schedule_policy),
        "pipeline_chain": _render(pipeline_chain),
        "target_lang": target_lang,
        "folder": folder,
        "trust_level": trust_level,
        "parser_plugin_id": parser_plugin_id,
    }


def _add_sources_confirmation(
    urls: list[str],
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    trust_level: str = "standard",
    parser_plugin_id: str = "",
) -> Elicit[Any]:
    """The add_sources question: one dialog for the whole batch."""
    fields = _add_sources_fields(
        urls,
        schedule_policy,
        type,
        pipeline_chain,
        target_lang,
        folder,
        trust_level,
        parser_plugin_id,
    )
    return _confirm_resolver(
        _summary=(
            f"Add {fields['count']} source(s) as {fields['type']} with policy "
            f"{fields['schedule_policy']} and folder "
            f"{fields['folder'] or DEFAULT_FOLDER}? {fields['urls']}"
        ),
        _fields=fields,
    )


def _fetch_items_fields(
    source_id: str,
    urls: list[str],
    batch_size: int = 0,
    min_interval_seconds: float = 0.0,
) -> dict[str, Any]:
    """What fetch_source_items confirms and executes, derived in one place.

    The URL list travels as count + preview + fingerprint, the same
    reconciliation add_sources uses: an elicitation form carries primitives
    only, and a confirmation that pinned just the *count* would accept any list
    of that length -- which is the whole reason the fingerprint exists (#67).

    The pacing knobs are part of the confirmed values because they are the
    difference between a polite request and a burst; 0 renders as "default"
    (the API's own values), so the dialog does not claim a number the server
    has not clamped yet.
    """
    preview = "; ".join(urls[:5])
    more = "" if len(urls) <= 5 else f" (+{len(urls) - 5} more)"
    return {
        "source_id": source_id,
        "count": str(len(urls)),
        "urls": f"{preview}{more}",
        "urls_fingerprint": _urls_fingerprint(urls),
        "batch_size": str(batch_size) if batch_size else "default",
        "min_interval_seconds": (
            f"{min_interval_seconds:g}" if min_interval_seconds else "default"
        ),
    }


def _fetch_items_confirmation(
    source_id: str,
    urls: list[str],
    batch_size: int = 0,
    min_interval_seconds: float = 0.0,
) -> Elicit[Any]:
    """The fetch_source_items question: one dialog for the whole list."""
    fields = _fetch_items_fields(source_id, urls, batch_size, min_interval_seconds)
    return _confirm_resolver(
        _summary=(
            f"Fetch {fields['count']} address(es) into source "
            f"{fields['source_id']}? Pacing: {fields['batch_size']} per burst, "
            f"{fields['min_interval_seconds']}s apart. {fields['urls']}"
        ),
        _fields=fields,
    )


def _update_schedule_policy_fields(
    source_id: str, schedule_policy: dict[str, Any]
) -> dict[str, Any]:
    """What update_schedule_policy confirms and executes, derived in one place."""
    return {
        "source_id": source_id,
        "schedule_policy": _render(schedule_policy),
    }


def _update_schedule_policy_confirmation(
    source_id: str, schedule_policy: dict[str, Any]
) -> Elicit[Any]:
    fields = _update_schedule_policy_fields(source_id, schedule_policy)
    return _confirm_resolver(
        _summary=f"Set source {source_id} schedule to {fields['schedule_policy']}?",
        _fields=fields,
    )


def _set_source_status_fields(source_id: str, status: str) -> dict[str, Any]:
    """What set_source_status confirms and executes, derived in one place."""
    return {"source_id": source_id, "status": status}


def _set_source_status_confirmation(source_id: str, status: str) -> Elicit[Any]:
    fields = _set_source_status_fields(source_id, status)
    return _confirm_resolver(
        _summary=f"Set source {fields['source_id']} status to {fields['status']}?",
        _fields=fields,
    )


def _set_source_folder_fields(source_id: str, folder: str) -> dict[str, Any]:
    """What set_source_folder confirms and executes, derived in one place."""
    return {"source_id": source_id, "folder": folder}


def _set_source_folder_confirmation(source_id: str, folder: str) -> Elicit[Any]:
    fields = _set_source_folder_fields(source_id, folder)
    return _confirm_resolver(
        _summary=f"Move source {fields['source_id']} to folder {fields['folder']}?",
        _fields=fields,
    )


def _trigger_job_fields(source_id: str) -> dict[str, Any]:
    """What trigger_job confirms and executes, derived in one place."""
    return {"source_id": source_id}


def _trigger_job_confirmation(source_id: str) -> Elicit[Any]:
    fields = _trigger_job_fields(source_id)
    return _confirm_resolver(
        _summary=f"Fetch source {fields['source_id']} now?",
        _fields=fields,
    )


def _backfill_source_fields(source_id: str) -> dict[str, Any]:
    """What backfill_source confirms and executes, derived in one place."""
    return {"source_id": source_id}


def _backfill_source_confirmation(source_id: str) -> Elicit[Any]:
    fields = _backfill_source_fields(source_id)
    return _confirm_resolver(
        _summary=f"Backfill processing products for source {fields['source_id']}?",
        _fields=fields,
    )


def _retry_enrichments_fields(source_id: str) -> dict[str, Any]:
    """What retry_enrichments confirms and executes, derived in one place."""
    return {"source_id": source_id}


def _retry_enrichments_confirmation(source_id: str) -> Elicit[Any]:
    fields = _retry_enrichments_fields(source_id)
    source = fields["source_id"]
    return _confirm_resolver(
        _summary=f"Requeue failed processing-chain products for source {source}?",
        _fields=fields,
    )


def _submit_items_fields(source_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """What submit_items confirms and executes, derived in one place.

    The fingerprint is the field that covers what the dialog cannot show: two
    batches can share a count and even every title while differing in every
    body, so `item_count` alone accepted the second batch (issue #67). Titles
    stay in the list for the human reading the prompt.
    """
    titles = "; ".join(
        str(item.get("title") or "(untitled)")[:60] for item in items[:5]
    )
    more = "" if len(items) <= 5 else f" (+{len(items) - 5} more)"
    return {
        "source_id": source_id,
        "item_count": str(len(items)),
        "items_fingerprint": _items_fingerprint(items),
        "titles": f"{titles}{more}",
    }


def _submit_items_confirmation(
    source_id: str, items: list[dict[str, Any]]
) -> Elicit[Any]:
    """The submit_items question: names the target and what will land in it.

    Item titles are listed rather than bodies: a prompt the user cannot read
    is not a confirmation, and a body can be arbitrarily long. The count is
    carried separately so a truncated list is still an honest one, and the
    fingerprint covers what the titles cannot -- two batches can share titles
    and a count while differing in every body.
    """
    fields = _submit_items_fields(source_id, items)
    return _confirm_resolver(
        _summary=(
            f"Push {len(items)} item(s) into agent source {source_id}? "
            f"{fields['titles']}"
        ),
        _fields=fields,
    )


def _set_source_pipeline_chain_fields(
    source_id: str,
    pipeline_chain: list[dict[str, Any]],
    target_lang: str = "",
) -> dict[str, Any]:
    """What set_source_pipeline_chain confirms and executes, derived in one place.

    The chain travels as its canonical JSON text because an elicitation field is
    a primitive; an empty list is a real value here -- it clears the chain -- so
    it is confirmed as `[]` rather than as an empty string.
    """
    return {
        "source_id": source_id,
        "pipeline_chain": _render(pipeline_chain),
        "target_lang": target_lang,
    }


def _set_source_pipeline_chain_confirmation(
    source_id: str,
    pipeline_chain: list[dict[str, Any]],
    target_lang: str = "",
) -> Elicit[Any]:
    """The chain question: says what will run, because that is what it costs.

    Unlike a status or a folder, this decides whether items are held at the gate
    and whether a provider is called for each of them, so the message names the
    chain rather than saying "update source X".
    """
    fields = _set_source_pipeline_chain_fields(source_id, pipeline_chain, target_lang)
    chain = fields["pipeline_chain"]
    return _confirm_resolver(
        _summary=(
            f"Set source {source_id} processing chain to {chain}"
            f" (lang {fields['target_lang'] or 'none'})? New items are held "
            "until it completes, and its processors can spend provider budget."
        ),
        _fields=fields,
    )


def _set_plugin_status_fields(plugin_id: str, status: str) -> dict[str, Any]:
    """What set_plugin_status confirms and executes, derived in one place."""
    return {"plugin_id": plugin_id, "status": status}


def _set_plugin_status_confirmation(plugin_id: str, status: str) -> Elicit[Any]:
    fields = _set_plugin_status_fields(plugin_id, status)
    return _confirm_resolver(
        _summary=f"Set plugin {fields['plugin_id']} status to {fields['status']}?",
        _fields=fields,
    )


def _remove_source_fields(source_id: str) -> dict[str, Any]:
    """What remove_source confirms and executes, derived in one place."""
    return {"source_id": source_id}


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
        _fields=_remove_source_fields(source_id),
    )


def _delete_items_fields(item_ids: list[str]) -> dict[str, Any]:
    """What delete_items confirms and executes, derived in one place.

    The list travels as one comma-joined string because an elicitation field is
    a primitive; the join is deterministic, so a reordered or shortened list
    compares unequal.
    """
    return {"item_ids": _comma_join(item_ids)}


def _delete_items_confirmation(item_ids: list[str]) -> Elicit[Any]:
    listed = ", ".join(item_ids[:5])
    if len(item_ids) > 5:
        listed += f", and {len(item_ids) - 5} more"
    return _confirm_resolver(
        _summary=(
            f"Permanently delete {len(item_ids)} item(s): {listed}? Their "
            "bodies, read state and translations go too. This cannot be undone."
        ),
        _fields=_delete_items_fields(item_ids),
    )


def _delete_source_items_fields(
    source_id: str,
    since: str = "",
    until: str = "",
    include_published: bool = True,
) -> dict[str, Any]:
    """What delete_source_items confirms and executes, derived in one place."""
    return {
        "source_id": source_id,
        "since": since,
        "until": until,
        "include_published": include_published,
    }


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
        _fields=_delete_source_items_fields(source_id, since, until, include_published),
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


def _write_verdict(confirmed: Any, fields: dict[str, Any]) -> dict[str, Any] | None:
    """The refusal to return instead of running, or None to proceed.

    This is the second gate, and the one this module owns. The first is the
    protocol's: the SDK seals `request_state` with a digest of the whole
    arguments, so a retry that drifted is refused before any tool body runs.
    This gate is what covers the remaining case -- a client that answers the
    form with values other than the ones it was prefilled with.

    `fields` is the same dict the resolver put in the dialog, so the comparison
    covers exactly the values about to execute. There is no second list to keep
    in step, which is what the twelve hand-written comparisons got wrong once
    (issue #67) and had twelve chances to get wrong again (issue #76).

    A missing or null confirmed field counts as drift rather than raising: the
    answer came from a client, and a client that omits a field has not confirmed
    it.
    """
    values = _confirmation_verdict(confirmed)
    if values is None:
        return {"declined": True, "reason": _DECLINED_REASON}
    for name, executed in fields.items():
        confirmed_value = values.get(name)
        if confirmed_value is None or not _matches(confirmed_value, executed):
            return {"aborted": True, "reason": _ABORTED_REASON}
    return None
