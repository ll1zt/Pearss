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
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.resolve import Elicit, Resolve
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
    """The elicited acknowledgement for a write: one value, no field-by-field echo.

    It used to carry every derived field, and the user had to read (and
    implicitly re-supply) each one. That put the whole shape of the request in
    front of the user every time, and the values were never theirs to edit on
    this path anyway: the tool executes what the server derived. One
    acknowledgement says the same thing in one round (issue #250).

    The default is `true` so that accepting the dialog without touching it means
    proceed, and only an explicit uncheck cancels. The values that execute are
    not part of this schema: they come from the tool's own `_<tool>_fields`, and
    the summary above the checkbox names them.
    """

    confirmed: bool = Field(
        default=True,
        description="Leave checked to proceed. Uncheck to cancel the operation.",
    )


@dataclass(frozen=True)
class _Editable:
    """One field the user may change, and how it travels through the dialog.

    `structured` says the value is a dict or list, which an elicitation schema
    can only carry as canonical JSON text -- so the answered text has to be
    parsed back, and parsing is what can fail. Recording the *kind* here, rather
    than reading it off the current value, keeps that honest when the value is
    absent: an unset `pipeline_chain` is still a list, so editing it must produce
    a list rather than the raw text (issue #257).
    """

    value: Any
    structured: bool = False


def _editables(editable: dict[str, Any]) -> dict[str, _Editable]:
    """Normalise a declared editable set to `_Editable` entries.

    A tool names a scalar by its value (`{"folder": "blog"}`) and a structured
    field by wrapping it (`{"pipeline_chain": _Editable(chain, structured=True)}`),
    so the ladder reads one shape either way.
    """
    return {
        name: entry if isinstance(entry, _Editable) else _Editable(entry)
        for name, entry in editable.items()
    }


class _Confirmation(Elicit[Any]):
    """An `Elicit` that also carries the fields it was derived from.

    The schema no longer echoes them (issue #250), so this is how the structural
    guard can still ask "does the confirmation cover what executes?" without a
    second, hand-written table of which function derives which tool's fields --
    the same reason the guard reads the registry instead of a list.
    """

    def __init__(
        self,
        message: str,
        schema: type[Confirmation],
        fields: dict[str, Any],
        editable: dict[str, _Editable],
    ) -> None:
        super().__init__(message, schema)
        self.fields = fields
        # Name -> editable entry for the parameters the user may change on the
        # edit path (#252). A subset of the fields by construction: it is built
        # from them, and the guard checks it stays inside the arguments.
        self.editable = editable


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
    """The elicitation schema for a write: the acknowledgement, nothing else.

    Elicitation sends a *schema*, and the client returns what the user filled
    in. Rendering every derived field meant the user read the whole request back
    on every write (issue #250); the fields that execute are named in the
    `message` above this checkbox instead, and the edit path (#252) asks about
    one field at a time.

    `fields` is still the argument the confirmation is *about*: the same dict the
    tool's `_<tool>_fields` produced, which the summary is built from and which
    the guard checks the coverage of. It is no longer rendered, and it is not
    compared field-by-field any more either -- see `_write_verdict` for why that
    comparison has nothing left to protect.

    Fields are primitives only, when they are rendered at all: MCP renders an
    elicitation schema from primitive types, and a nested object there raises
    TypeError at render time — which is why a schedule policy travels as its
    canonical JSON text.
    """
    del fields
    return Confirmation


def _asked(
    model: type[Confirmation],
    message: str,
    fields: dict[str, Any] | None = None,
    editable: dict[str, Any] | None = None,
) -> _Confirmation:
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
    return _Confirmation(message, model, dict(fields or {}), _editables(editable or {}))


def _confirm_resolver(**kwargs: Any) -> _Confirmation:
    """Ask for confirmation of one write, with the request named in words.

    Every write tool's confirmation runs through this: the summary names the
    write and the values about to execute, and the schema carries the one
    acknowledgement that accepts them. `_summary` and `_fields` are computed by
    each tool's own resolver from the tool arguments; the fields travel on the
    result (`_Confirmation`) rather than in the dialog, so a structural guard can
    still check that the summary covers everything that executes.
    """
    summary = kwargs.pop("_summary")
    fields = kwargs.pop("_fields")
    # Required, not defaulted: a tool that does not say what the user may edit
    # would have to be discovered by reading it, and the edit path (#252) would
    # silently offer nothing.
    editable = kwargs.pop("_editable")
    return _asked(_confirmation_schema(fields), summary, fields, editable)


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


def _add_source_marker(
    url: str,
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    ctx: Context | None = None,
) -> _Confirmation:
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
        _editable={
            "schedule_policy": _Editable(schedule_policy, structured=True),
            "pipeline_chain": _Editable(pipeline_chain, structured=True),
            "target_lang": fields["target_lang"],
            "folder": fields["folder"],
        },
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

    The fields are the command value (issue #199): the kind, the targets as a
    count + preview + fingerprint, and the params. `type` stays as requested --
    `auto` is resolved server-side per URL by the `subscribe` executor, so the
    confirmed value is exactly the executed one. The URL list travels as count +
    preview + fingerprint, the same reconciliation `submit_items` uses for item
    bodies.
    """
    preview = "; ".join(urls[:5])
    more = "" if len(urls) <= 5 else f" (+{len(urls) - 5} more)"
    return {
        "kind": "subscribe",
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


def _add_sources_marker(
    urls: list[str],
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    trust_level: str = "standard",
    parser_plugin_id: str = "",
) -> _Confirmation:
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
            f"{fields['folder'] or DEFAULT_FOLDER}?"
            # The batch options are part of what executes, so they are part of
            # what the user is shown: a dialog that named only the count and the
            # policy asked the user to accept a chain and a trust level unseen
            # (issue #250's sweep found exactly this).
            f" Chain {fields['pipeline_chain']}, lang "
            f"{fields['target_lang'] or 'none'}, trust {fields['trust_level']}, "
            f"parser {fields['parser_plugin_id'] or 'auto'}. {fields['urls']}"
        ),
        _fields=fields,
        _editable={
            "schedule_policy": _Editable(schedule_policy, structured=True),
            "folder": fields["folder"],
            "pipeline_chain": _Editable(pipeline_chain, structured=True),
            "target_lang": fields["target_lang"],
            "trust_level": fields["trust_level"],
            "parser_plugin_id": fields["parser_plugin_id"],
        },
    )


def _fetch_work_fields(
    source_id: str,
    urls: list[str],
    batch_size: int = 0,
    min_interval_seconds: float = 0.0,
) -> dict[str, Any]:
    """What fetch_source_items confirms and executes, derived in one place.

    The dialog carries the command: the `kind`, the target list (as count +
    preview + fingerprint) and the params that shape the run. It is the same
    value the server hashes into the command fingerprint, so "the confirmation
    covers what executes" is one computation rather than two lists kept in
    step by hand.

    The list travels as count + preview + fingerprint, the reconciliation
    add_sources uses: an elicitation form carries primitives only, and a
    confirmation that pinned just the *count* would accept any list of that
    length -- which is the whole reason the fingerprint exists (#67).

    The pacing knobs are among the confirmed values because they are the
    difference between a polite request and a burst; 0 renders as "default"
    (the server's own values), so the dialog does not claim a number that has
    not been clamped yet.
    """
    preview = "; ".join(urls[:5])
    more = "" if len(urls) <= 5 else f" (+{len(urls) - 5} more)"
    return {
        "kind": "fetch",
        "source_id": source_id,
        "count": str(len(urls)),
        "urls": f"{preview}{more}",
        "urls_fingerprint": _urls_fingerprint(urls),
        "batch_size": str(batch_size) if batch_size else "default",
        "min_interval_seconds": (
            f"{min_interval_seconds:g}" if min_interval_seconds else "default"
        ),
    }


def _fetch_work_marker(
    source_id: str,
    urls: list[str],
    batch_size: int = 0,
    min_interval_seconds: float = 0.0,
) -> _Confirmation:
    """The fetch_source_items question: one dialog for the whole batch.

    The summary says *job* because that is what the caller is agreeing to: the
    answer comes back as a handle, the deployment keeps fetching after the
    client's request ends, and the per-address results live in the work's
    ledger rather than in a response whose size follows the list.
    """
    fields = _fetch_work_fields(source_id, urls, batch_size, min_interval_seconds)
    return _confirm_resolver(
        _summary=(
            f"Queue a fetch job for {fields['count']} address(es) into source "
            f"{fields['source_id']}? Pacing: {fields['batch_size']} per burst, "
            f"{fields['min_interval_seconds']}s apart. {fields['urls']}"
        ),
        _fields=fields,
        _editable={
            "batch_size": fields["batch_size"],
            "min_interval_seconds": fields["min_interval_seconds"],
        },
    )


def _update_schedule_policy_fields(
    source_id: str, schedule_policy: dict[str, Any]
) -> dict[str, Any]:
    """What update_schedule_policy confirms and executes, derived in one place."""
    return {
        "source_id": source_id,
        "schedule_policy": _render(schedule_policy),
    }


def _update_schedule_policy_marker(
    source_id: str, schedule_policy: dict[str, Any]
) -> _Confirmation:
    fields = _update_schedule_policy_fields(source_id, schedule_policy)
    return _confirm_resolver(
        _summary=f"Set source {source_id} schedule to {fields['schedule_policy']}?",
        _fields=fields,
        _editable={"schedule_policy": _Editable(schedule_policy, structured=True)},
    )


def _set_source_status_fields(source_id: str, status: str) -> dict[str, Any]:
    """What set_source_status confirms and executes, derived in one place."""
    return {"source_id": source_id, "status": status}


def _set_source_status_marker(source_id: str, status: str) -> _Confirmation:
    fields = _set_source_status_fields(source_id, status)
    return _confirm_resolver(
        _summary=f"Set source {fields['source_id']} status to {fields['status']}?",
        _fields=fields,
        _editable={"status": fields["status"]},
    )


def _set_source_folder_fields(source_id: str, folder: str) -> dict[str, Any]:
    """What set_source_folder confirms and executes, derived in one place."""
    return {"source_id": source_id, "folder": folder}


def _set_source_folder_marker(source_id: str, folder: str) -> _Confirmation:
    fields = _set_source_folder_fields(source_id, folder)
    return _confirm_resolver(
        _summary=f"Move source {fields['source_id']} to folder {fields['folder']}?",
        _fields=fields,
        _editable={"folder": fields["folder"]},
    )


def _read_state_change(
    is_read: bool | None, is_starred: bool | None
) -> dict[str, bool]:
    """The fields a read-state call actually asks to change.

    A read-state write is *partial*: an omitted field keeps its value, so the
    change is a subset of {is_read, is_starred} rather than two booleans. Both
    the confirmation and the request body derive from this one function, so
    "what was confirmed" and "what executes" cannot drift.
    """
    pairs = (("is_read", is_read), ("is_starred", is_starred))
    return {name: value for name, value in pairs if value is not None}


def _set_item_read_state_fields(
    item_id: str, is_read: bool | None, is_starred: bool | None
) -> dict[str, Any]:
    """What set_item_read_state confirms and executes, derived in one place.

    The change travels as its canonical JSON text: an elicitation schema
    renders primitives only, and `None` has no primitive spelling that does not
    also erase the difference between "leave it alone" and "set it false".
    """
    return {
        "item_id": item_id,
        "change": _render(_read_state_change(is_read, is_starred)),
    }


def _set_item_read_state_marker(
    item_id: str, is_read: bool | None, is_starred: bool | None
) -> _Confirmation:
    fields = _set_item_read_state_fields(item_id, is_read, is_starred)
    return _confirm_resolver(
        _summary=f"Set item {fields['item_id']} read state to {fields['change']}?",
        _fields=fields,
        _editable={"is_read": is_read, "is_starred": is_starred},
    )


def _trigger_job_fields(source_id: str) -> dict[str, Any]:
    """What trigger_job confirms and executes, derived in one place."""
    return {"source_id": source_id}


def _trigger_job_marker(source_id: str) -> _Confirmation:
    fields = _trigger_job_fields(source_id)
    return _confirm_resolver(
        _summary=f"Fetch source {fields['source_id']} now?",
        _fields=fields,
        _editable={},
    )


def _backfill_source_fields(source_id: str) -> dict[str, Any]:
    """What backfill_source confirms and executes, derived in one place."""
    return {"source_id": source_id}


def _backfill_source_marker(source_id: str) -> _Confirmation:
    fields = _backfill_source_fields(source_id)
    return _confirm_resolver(
        _summary=f"Backfill processing products for source {fields['source_id']}?",
        _fields=fields,
        _editable={},
    )


def _retry_enrichments_fields(
    enrichment_id: str = "", source_id: str = ""
) -> dict[str, Any]:
    """What retry_enrichments confirms and executes, derived in one place.

    Two granularities of one intent (issue #195): one row, or every failed row
    of one source. Which one this is decides the endpoint *and* the sentence,
    so both are derived from the same pair of arguments the caller passed.
    """
    return {"enrichment_id": enrichment_id, "source_id": source_id}


def _retry_enrichments_marker(
    enrichment_id: str = "", source_id: str = ""
) -> _Confirmation:
    fields = _retry_enrichments_fields(enrichment_id, source_id)
    if fields["enrichment_id"]:
        target = f"processing-chain row {fields['enrichment_id']}"
    elif fields["source_id"]:
        target = (
            f"every failed processing-chain product of source {fields['source_id']}"
        )
    else:
        # The dialog is shown before the tool body can refuse, so it says what
        # is wrong rather than claiming to retry "source ''".
        target = "nothing: no enrichment_id and no source_id were given"
    return _confirm_resolver(
        # Both ids, because both are what the tool was given: when they are, the
        # row wins and the source is ignored, and a dialog that showed only the
        # winner would let a caller accept a value it cannot see (issue #250).
        _summary=(
            f"Requeue {target}? (enrichment_id={fields['enrichment_id'] or '-'}, "
            f"source_id={fields['source_id'] or '-'}) The worker charges the "
            "provider again for whatever it retries."
        ),
        _fields=fields,
        _editable={},
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
        "kind": "ingest",
        "source_id": source_id,
        "item_count": str(len(items)),
        "items_fingerprint": _items_fingerprint(items),
        "titles": f"{titles}{more}",
    }


def ingest_targets(items: list[dict[str, Any]]) -> list[str]:
    """The submitted items as the *targets* of an `ingest` work (issue #199).

    One item per target, as canonical JSON. The executor validates each target
    with the same model the endpoint uses (`app.ingest`), so a target that is
    not a usable item fails alone instead of refusing the batch -- and the
    confirmation's fingerprint is computed over these same items, so what the
    user saw is what is queued.
    """
    return [json.dumps(item, separators=(",", ":"), sort_keys=True) for item in items]


def _submit_items_marker(source_id: str, items: list[dict[str, Any]]) -> _Confirmation:
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
            # The fingerprint is the field that covers the bodies: two batches
            # can share a count and every title, so a summary that stopped at the
            # titles would ask the user to accept content it never showed
            # (issue #250's sweep found exactly this).
            f"{fields['titles']} (fingerprint {fields['items_fingerprint']})"
        ),
        _fields=fields,
        _editable={},
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


def _set_source_pipeline_chain_marker(
    source_id: str,
    pipeline_chain: list[dict[str, Any]],
    target_lang: str = "",
) -> _Confirmation:
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
        _editable={
            "pipeline_chain": _Editable(pipeline_chain, structured=True),
            "target_lang": fields["target_lang"],
        },
    )


def _set_plugin_status_fields(plugin_id: str, status: str) -> dict[str, Any]:
    """What set_plugin_status confirms and executes, derived in one place."""
    return {"plugin_id": plugin_id, "status": status}


def _set_plugin_status_marker(plugin_id: str, status: str) -> _Confirmation:
    fields = _set_plugin_status_fields(plugin_id, status)
    return _confirm_resolver(
        _summary=f"Set plugin {fields['plugin_id']} status to {fields['status']}?",
        _fields=fields,
        _editable={"status": fields["status"]},
    )


def _remove_source_fields(source_id: str) -> dict[str, Any]:
    """What remove_source confirms and executes, derived in one place."""
    return {"source_id": source_id}


def _remove_source_marker(source_id: str) -> _Confirmation:
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
        _editable={},
    )


def _delete_items_fields(item_ids: list[str]) -> dict[str, Any]:
    """What delete_items confirms and executes, derived in one place.

    The fields are the command value (issue #199): the kind, the targets as a
    count + preview + fingerprint, and the params. The list also travels as one
    comma-joined string because an elicitation field is a primitive and the
    dialog names what is about to disappear; the join is deterministic, so a
    reordered or shortened list compares unequal.
    """
    return {
        "kind": "delete",
        "item_ids": _comma_join(item_ids),
        "count": str(len(item_ids)),
        "targets_fingerprint": _urls_fingerprint(item_ids),
    }


def _delete_items_marker(item_ids: list[str]) -> _Confirmation:
    listed = ", ".join(item_ids[:5])
    if len(item_ids) > 5:
        listed += f", and {len(item_ids) - 5} more"
    return _confirm_resolver(
        _summary=(
            f"Permanently delete {len(item_ids)} item(s): {listed}? Their "
            "bodies, read state and translations go too. This cannot be undone."
        ),
        _fields=_delete_items_fields(item_ids),
        _editable={},
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


def _delete_source_items_marker(
    source_id: str,
    since: str = "",
    until: str = "",
    include_published: bool = True,
) -> _Confirmation:
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
        _editable={
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


def _aborted() -> dict[str, Any]:
    """The one spelling of the `aborted` refusal (AC-76 keeps it to one place)."""
    return {"aborted": True, "reason": _ABORTED_REASON}


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

    There is no field comparison left on this path (issue #250): the values that
    execute are the ones the server derived, so a client cannot substitute
    different ones -- the dialog offers no field to substitute, and anything it
    invents is not read at all. `fields` stays in the signature because the
    *edit* path (#252) does compare the one value the user supplied.
    """
    if isinstance(_answer_data(confirmed), _Aborted):
        return _aborted()
    values = _confirmation_verdict(confirmed)
    if values is None:
        return {"declined": True, "reason": _DECLINED_REASON}
    del fields
    return None


# --------------------------------------------------------------------------
# The edit path (#252): choose a field, give it a value, confirm the result
# --------------------------------------------------------------------------
#
# Four resolvers per tool, because a resolver cannot see its own answer: the
# framework injects an answer into its *consumer*, so each question needs its own
# resolver and the chain is what carries the answers forward.
#
#   1. choose  -- acknowledge, and name at most one field to change
#   2. value   -- that field's new value (skipped when nothing was chosen)
#   3. confirm -- the composed request, shown and echoed (skipped likewise)
#   4. compose -- check the echo against what was shown, then build the request
#
# Stage 4 exists because the check in stage 3 needs both answers at once, and a
# resolver only sees its dependencies. The default path still costs one round.


class _NoEdit:
    """Round 1 chose to change nothing."""


class _Aborted:
    """Round 3 answered something other than the request it was shown (AC-244)."""


class _Resolved(Confirmation):
    """The effective request: the acknowledgement plus any edited values.

    A `Confirmation` so every write tool's parameter type is unchanged; the
    overrides are applied in one place (`_guarded_write`), which also checks that
    each names a field of the body about to be sent.
    """

    overrides: dict[str, Any] = Field(default_factory=dict)


def _answer_data(outcome: Any) -> Any | None:
    """The accepted data of an outcome, or None for a decline/cancel.

    `None` means "not confirmed" all the way to the tool: the dependency
    parameters are annotated as `ElicitationResult` (so a refusal arrives as a
    refusal rather than as a `ToolError`), and every stage passes it along.
    """
    if outcome is None:
        return None
    if getattr(outcome, "action", "accept") in ("decline", "cancel"):
        return None
    data = getattr(outcome, "data", None)
    if data is None:
        return outcome
    if hasattr(data, "confirmed") and not data.confirmed:
        # The box is the answer: a round where the user unchecked it is a refusal,
        # even though the client "accepted" the dialog to send it.
        return None
    return data


def _scalar_type(value: Any) -> Any:
    """The primitive type an elicitation schema can render for `value`."""
    return type(value) if isinstance(value, (bool, int, float)) else str


def _edit_choice_schema(editable: dict[str, Any]) -> type[Confirmation]:
    """Round 1's form: acknowledge, and name one field to change.

    The field names are a closed enum, never free text: a typo in a text field
    would silently change nothing, and a name that is not editable must have no
    spelling that reaches the server.
    """
    choices = ("none", *editable)
    return create_model(
        "EditChoice",
        __base__=Confirmation,
        edit=(
            Literal[tuple(choices)],
            Field(default="none", description="Field to change, or 'none'."),
        ),
    )


def _value_schema(field: str, entry: _Editable) -> type[BaseModel]:
    """Round 2's form: the chosen field, prefilled with the value it has today.

    The type follows the value: a boolean stays a checkbox, a number a number,
    and a structured value (a schedule policy, a chain) travels as its canonical
    JSON text, because an elicitation schema renders primitives only.
    """
    kind: Any = str
    shown: Any = str(entry.value)
    if entry.structured:
        shown = _render(entry.value)
    elif isinstance(entry.value, bool):
        kind, shown = bool, entry.value
    elif isinstance(entry.value, int):
        kind, shown = int, entry.value
    elif isinstance(entry.value, float):
        kind, shown = float, entry.value
    return create_model(
        "EditValue",
        field=(str, Field(default=field)),
        value=(kind, Field(default=shown)),
    )


def _edit_confirm_schema(field: str, value: Any) -> type[BaseModel]:
    """Round 3's form: the composed request, shown and echoed.

    The value is a field of the schema on purpose: the last stage compares what
    the client echoed against what this dialog displayed, so a client that
    answers something else is refused rather than trusted (AC-244).
    """
    return create_model(
        "EditConfirm",
        __base__=Confirmation,
        field=(str, Field(default=field)),
        value=(_scalar_type(value), Field(default=value)),
    )


def _parsed_edit(entry: _Editable, answered: Any) -> Any:
    """The answered text of a structured field, parsed back to its shape.

    The one place a structured edit is parsed, and therefore the one place it
    can fail: every caller maps a `ValueError` (a bad JSON text) to `_Aborted`,
    so the refusal has the one shape a client branches on (issue #257).
    """
    if entry.structured and isinstance(answered, str):
        return json.loads(answered)
    return answered


def _ask_choice(marker: _Confirmation) -> _Confirmation:
    """Round 1: the summary, the acknowledgement, and which field to change.

    A `_Confirmation` rather than a bare `Elicit` so the structural guards can
    still read the derived fields and the editable set off the round-1 resolver.
    """
    return _Confirmation(
        marker.message,
        _edit_choice_schema(marker.editable),
        marker.fields,
        marker.editable,
    )


def _ask_value(choice: Any, marker: _Confirmation) -> Any:
    """Round 2: the chosen field's new value, asked only if one was chosen."""
    picked = _answer_data(choice)
    if picked is None:
        return None
    field = str(getattr(picked, "edit", "none"))
    if field == "none":
        return _NoEdit()
    if field not in marker.editable:
        # A name the dialog never offered: the client answered a question that
        # does not exist, so the request is refused rather than half-applied.
        return _Aborted()
    return Elicit(
        f"{marker.message} New value for {field}:",
        _value_schema(field, marker.editable[field]),
    )


def _ask_confirm(value: Any, marker: _Confirmation) -> Any:
    """Round 3: show the composed request and have it acknowledged.

    A structured field's text is parsed here to decide whether a request can be
    built at all: invalid JSON is refused as `aborted` *before* the dialog, so
    the user is never asked to confirm something the server already rejects
    (issue #257). The dialog itself echoes the text the client sent, which is
    what round 4 compares against, so the parsed value is not needed yet.
    """
    if value is None:
        return None
    if isinstance(value, _Aborted):
        return value
    if isinstance(value, _NoEdit):
        return _Resolved(confirmed=True)
    answered = _answer_data(value)
    if answered is None:
        return None
    field = str(getattr(answered, "field", ""))
    raw = getattr(answered, "value", None)
    if field not in marker.editable:
        return _Aborted()
    try:
        _parsed_edit(marker.editable[field], raw)
    except ValueError:
        return _Aborted()
    return Elicit(
        f"{marker.message} Change {field} to {raw}?",
        _edit_confirm_schema(field, raw),
    )


def _compose_edit(
    choice: Any, value: Any, confirmed: Any, marker: _Confirmation
) -> Any:
    """Round 4: verify round 3 echoed the request it showed, then compose it.

    `_matches` is the check AC-244 asks for: round 3 displayed a value and the
    client echoed one, and the two must be the same. A client that answers
    something else is refused in turn rather than trusted to have sent what it
    was shown.
    """
    if isinstance(value, _Aborted):
        return _Aborted()
    picked = _answer_data(choice)
    if picked is None:
        return None
    field = str(getattr(picked, "edit", "none"))
    if field == "none":
        return _Resolved(confirmed=True)
    if field not in marker.editable:
        return _Aborted()
    answered = _answer_data(value)
    if answered is None:
        return None
    shown = getattr(answered, "value", None)
    final = _answer_data(confirmed)
    if final is None:
        return None
    if not _matches(getattr(final, "value", None), shown):
        return _Aborted()
    try:
        edited = _parsed_edit(marker.editable[field], shown)
    except ValueError:
        return _Aborted()
    return _Resolved(confirmed=True, overrides={field: edited})


def _overrides(confirmed: Any, body: dict[str, Any] | None) -> dict[str, Any]:
    """The edited values to overlay on the request body, or an empty mapping.

    The values were already parsed when the ladder showed them, and a failure
    there was answered as `aborted`; this checks only that each names a field of
    the request about to be sent. An override that landed nowhere would be a
    dialog that changed nothing (issue #252), and a structured override has to
    be the list/dict the field expects, not the text the user typed (issue #257).
    """
    data = _answer_data(confirmed)
    if data is None or body is None:
        return {}
    overrides = getattr(data, "overrides", None)
    if not isinstance(overrides, dict) or not overrides:
        return {}
    unknown = sorted(field for field in overrides if field not in body)
    if unknown:
        raise ValueError(f"edited fields {unknown!r} are not part of this request")
    return dict(overrides)


def _add_source_edit_choice(
    url: str,
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    ctx: Context | None = None,
) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(
        _add_source_marker(
            url, schedule_policy, type, pipeline_chain, target_lang, folder, ctx
        )
    )


def _add_source_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_add_source_edit_choice)],
    url: str,
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    ctx: Context | None = None,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(
        choice,
        _add_source_marker(
            url, schedule_policy, type, pipeline_chain, target_lang, folder, ctx
        ),
    )


def _add_source_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_add_source_edit_value)],
    url: str,
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    ctx: Context | None = None,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(
        value,
        _add_source_marker(
            url, schedule_policy, type, pipeline_chain, target_lang, folder, ctx
        ),
    )


def _add_source_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_add_source_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_add_source_edit_value)],
    confirmed: Annotated[ElicitationResult[Any], Resolve(_add_source_edit_confirm)],
    url: str,
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    ctx: Context | None = None,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice,
        value,
        confirmed,
        _add_source_marker(
            url, schedule_policy, type, pipeline_chain, target_lang, folder, ctx
        ),
    )


def _add_sources_edit_choice(
    urls: list[str],
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    trust_level: str = "standard",
    parser_plugin_id: str = "",
) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(
        _add_sources_marker(
            urls,
            schedule_policy,
            type,
            pipeline_chain,
            target_lang,
            folder,
            trust_level,
            parser_plugin_id,
        )
    )


def _add_sources_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_add_sources_edit_choice)],
    urls: list[str],
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    trust_level: str = "standard",
    parser_plugin_id: str = "",
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(
        choice,
        _add_sources_marker(
            urls,
            schedule_policy,
            type,
            pipeline_chain,
            target_lang,
            folder,
            trust_level,
            parser_plugin_id,
        ),
    )


def _add_sources_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_add_sources_edit_value)],
    urls: list[str],
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    trust_level: str = "standard",
    parser_plugin_id: str = "",
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(
        value,
        _add_sources_marker(
            urls,
            schedule_policy,
            type,
            pipeline_chain,
            target_lang,
            folder,
            trust_level,
            parser_plugin_id,
        ),
    )


def _add_sources_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_add_sources_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_add_sources_edit_value)],
    confirmed: Annotated[ElicitationResult[Any], Resolve(_add_sources_edit_confirm)],
    urls: list[str],
    schedule_policy: dict[str, Any],
    type: str = "auto",
    pipeline_chain: list[dict[str, Any]] | None = None,
    target_lang: str = "",
    folder: str = "",
    trust_level: str = "standard",
    parser_plugin_id: str = "",
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice,
        value,
        confirmed,
        _add_sources_marker(
            urls,
            schedule_policy,
            type,
            pipeline_chain,
            target_lang,
            folder,
            trust_level,
            parser_plugin_id,
        ),
    )


def _backfill_source_edit_choice(source_id: str) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_backfill_source_marker(source_id))


def _backfill_source_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_backfill_source_edit_choice)],
    source_id: str,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _backfill_source_marker(source_id))


def _backfill_source_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_backfill_source_edit_value)],
    source_id: str,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _backfill_source_marker(source_id))


def _backfill_source_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_backfill_source_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_backfill_source_edit_value)],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_backfill_source_edit_confirm)
    ],
    source_id: str,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(choice, value, confirmed, _backfill_source_marker(source_id))


def _delete_items_edit_choice(item_ids: list[str]) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_delete_items_marker(item_ids))


def _delete_items_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_delete_items_edit_choice)],
    item_ids: list[str],
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _delete_items_marker(item_ids))


def _delete_items_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_delete_items_edit_value)],
    item_ids: list[str],
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _delete_items_marker(item_ids))


def _delete_items_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_delete_items_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_delete_items_edit_value)],
    confirmed: Annotated[ElicitationResult[Any], Resolve(_delete_items_edit_confirm)],
    item_ids: list[str],
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(choice, value, confirmed, _delete_items_marker(item_ids))


def _delete_source_items_edit_choice(
    source_id: str,
    since: str = "",
    until: str = "",
    include_published: bool = True,
) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(
        _delete_source_items_marker(source_id, since, until, include_published)
    )


def _delete_source_items_edit_value(
    choice: Annotated[
        ElicitationResult[Any], Resolve(_delete_source_items_edit_choice)
    ],
    source_id: str,
    since: str = "",
    until: str = "",
    include_published: bool = True,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(
        choice, _delete_source_items_marker(source_id, since, until, include_published)
    )


def _delete_source_items_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_delete_source_items_edit_value)],
    source_id: str,
    since: str = "",
    until: str = "",
    include_published: bool = True,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(
        value, _delete_source_items_marker(source_id, since, until, include_published)
    )


def _delete_source_items_confirmation(
    choice: Annotated[
        ElicitationResult[Any], Resolve(_delete_source_items_edit_choice)
    ],
    value: Annotated[ElicitationResult[Any], Resolve(_delete_source_items_edit_value)],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_delete_source_items_edit_confirm)
    ],
    source_id: str,
    since: str = "",
    until: str = "",
    include_published: bool = True,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice,
        value,
        confirmed,
        _delete_source_items_marker(source_id, since, until, include_published),
    )


def _fetch_work_edit_choice(
    source_id: str,
    urls: list[str],
    batch_size: int = 0,
    min_interval_seconds: float = 0.0,
) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(
        _fetch_work_marker(source_id, urls, batch_size, min_interval_seconds)
    )


def _fetch_work_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_fetch_work_edit_choice)],
    source_id: str,
    urls: list[str],
    batch_size: int = 0,
    min_interval_seconds: float = 0.0,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(
        choice, _fetch_work_marker(source_id, urls, batch_size, min_interval_seconds)
    )


def _fetch_work_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_fetch_work_edit_value)],
    source_id: str,
    urls: list[str],
    batch_size: int = 0,
    min_interval_seconds: float = 0.0,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(
        value, _fetch_work_marker(source_id, urls, batch_size, min_interval_seconds)
    )


def _fetch_work_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_fetch_work_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_fetch_work_edit_value)],
    confirmed: Annotated[ElicitationResult[Any], Resolve(_fetch_work_edit_confirm)],
    source_id: str,
    urls: list[str],
    batch_size: int = 0,
    min_interval_seconds: float = 0.0,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice,
        value,
        confirmed,
        _fetch_work_marker(source_id, urls, batch_size, min_interval_seconds),
    )


def _remove_source_edit_choice(source_id: str) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_remove_source_marker(source_id))


def _remove_source_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_remove_source_edit_choice)],
    source_id: str,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _remove_source_marker(source_id))


def _remove_source_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_remove_source_edit_value)],
    source_id: str,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _remove_source_marker(source_id))


def _remove_source_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_remove_source_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_remove_source_edit_value)],
    confirmed: Annotated[ElicitationResult[Any], Resolve(_remove_source_edit_confirm)],
    source_id: str,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(choice, value, confirmed, _remove_source_marker(source_id))


def _retry_enrichments_edit_choice(enrichment_id: str = "", source_id: str = "") -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_retry_enrichments_marker(enrichment_id, source_id))


def _retry_enrichments_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_retry_enrichments_edit_choice)],
    enrichment_id: str = "",
    source_id: str = "",
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _retry_enrichments_marker(enrichment_id, source_id))


def _retry_enrichments_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_retry_enrichments_edit_value)],
    enrichment_id: str = "",
    source_id: str = "",
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _retry_enrichments_marker(enrichment_id, source_id))


def _retry_enrichments_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_retry_enrichments_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_retry_enrichments_edit_value)],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_retry_enrichments_edit_confirm)
    ],
    enrichment_id: str = "",
    source_id: str = "",
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice, value, confirmed, _retry_enrichments_marker(enrichment_id, source_id)
    )


def _set_item_read_state_edit_choice(
    item_id: str, is_read: bool | None, is_starred: bool | None
) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_set_item_read_state_marker(item_id, is_read, is_starred))


def _set_item_read_state_edit_value(
    choice: Annotated[
        ElicitationResult[Any], Resolve(_set_item_read_state_edit_choice)
    ],
    item_id: str,
    is_read: bool | None,
    is_starred: bool | None,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _set_item_read_state_marker(item_id, is_read, is_starred))


def _set_item_read_state_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_set_item_read_state_edit_value)],
    item_id: str,
    is_read: bool | None,
    is_starred: bool | None,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(
        value, _set_item_read_state_marker(item_id, is_read, is_starred)
    )


def _set_item_read_state_confirmation(
    choice: Annotated[
        ElicitationResult[Any], Resolve(_set_item_read_state_edit_choice)
    ],
    value: Annotated[ElicitationResult[Any], Resolve(_set_item_read_state_edit_value)],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_set_item_read_state_edit_confirm)
    ],
    item_id: str,
    is_read: bool | None,
    is_starred: bool | None,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice,
        value,
        confirmed,
        _set_item_read_state_marker(item_id, is_read, is_starred),
    )


def _set_plugin_status_edit_choice(plugin_id: str, status: str) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_set_plugin_status_marker(plugin_id, status))


def _set_plugin_status_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_set_plugin_status_edit_choice)],
    plugin_id: str,
    status: str,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _set_plugin_status_marker(plugin_id, status))


def _set_plugin_status_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_set_plugin_status_edit_value)],
    plugin_id: str,
    status: str,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _set_plugin_status_marker(plugin_id, status))


def _set_plugin_status_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_set_plugin_status_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_set_plugin_status_edit_value)],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_set_plugin_status_edit_confirm)
    ],
    plugin_id: str,
    status: str,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice, value, confirmed, _set_plugin_status_marker(plugin_id, status)
    )


def _set_source_folder_edit_choice(source_id: str, folder: str) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_set_source_folder_marker(source_id, folder))


def _set_source_folder_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_set_source_folder_edit_choice)],
    source_id: str,
    folder: str,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _set_source_folder_marker(source_id, folder))


def _set_source_folder_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_set_source_folder_edit_value)],
    source_id: str,
    folder: str,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _set_source_folder_marker(source_id, folder))


def _set_source_folder_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_set_source_folder_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_set_source_folder_edit_value)],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_set_source_folder_edit_confirm)
    ],
    source_id: str,
    folder: str,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice, value, confirmed, _set_source_folder_marker(source_id, folder)
    )


def _set_source_pipeline_chain_edit_choice(
    source_id: str,
    pipeline_chain: list[dict[str, Any]],
    target_lang: str = "",
) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(
        _set_source_pipeline_chain_marker(source_id, pipeline_chain, target_lang)
    )


def _set_source_pipeline_chain_edit_value(
    choice: Annotated[
        ElicitationResult[Any], Resolve(_set_source_pipeline_chain_edit_choice)
    ],
    source_id: str,
    pipeline_chain: list[dict[str, Any]],
    target_lang: str = "",
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(
        choice,
        _set_source_pipeline_chain_marker(source_id, pipeline_chain, target_lang),
    )


def _set_source_pipeline_chain_edit_confirm(
    value: Annotated[
        ElicitationResult[Any], Resolve(_set_source_pipeline_chain_edit_value)
    ],
    source_id: str,
    pipeline_chain: list[dict[str, Any]],
    target_lang: str = "",
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(
        value, _set_source_pipeline_chain_marker(source_id, pipeline_chain, target_lang)
    )


def _set_source_pipeline_chain_confirmation(
    choice: Annotated[
        ElicitationResult[Any], Resolve(_set_source_pipeline_chain_edit_choice)
    ],
    value: Annotated[
        ElicitationResult[Any], Resolve(_set_source_pipeline_chain_edit_value)
    ],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_set_source_pipeline_chain_edit_confirm)
    ],
    source_id: str,
    pipeline_chain: list[dict[str, Any]],
    target_lang: str = "",
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice,
        value,
        confirmed,
        _set_source_pipeline_chain_marker(source_id, pipeline_chain, target_lang),
    )


def _set_source_status_edit_choice(source_id: str, status: str) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_set_source_status_marker(source_id, status))


def _set_source_status_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_set_source_status_edit_choice)],
    source_id: str,
    status: str,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _set_source_status_marker(source_id, status))


def _set_source_status_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_set_source_status_edit_value)],
    source_id: str,
    status: str,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _set_source_status_marker(source_id, status))


def _set_source_status_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_set_source_status_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_set_source_status_edit_value)],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_set_source_status_edit_confirm)
    ],
    source_id: str,
    status: str,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice, value, confirmed, _set_source_status_marker(source_id, status)
    )


def _submit_items_edit_choice(source_id: str, items: list[dict[str, Any]]) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_submit_items_marker(source_id, items))


def _submit_items_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_submit_items_edit_choice)],
    source_id: str,
    items: list[dict[str, Any]],
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _submit_items_marker(source_id, items))


def _submit_items_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_submit_items_edit_value)],
    source_id: str,
    items: list[dict[str, Any]],
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _submit_items_marker(source_id, items))


def _submit_items_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_submit_items_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_submit_items_edit_value)],
    confirmed: Annotated[ElicitationResult[Any], Resolve(_submit_items_edit_confirm)],
    source_id: str,
    items: list[dict[str, Any]],
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice, value, confirmed, _submit_items_marker(source_id, items)
    )


def _trigger_job_edit_choice(source_id: str) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_trigger_job_marker(source_id))


def _trigger_job_edit_value(
    choice: Annotated[ElicitationResult[Any], Resolve(_trigger_job_edit_choice)],
    source_id: str,
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(choice, _trigger_job_marker(source_id))


def _trigger_job_edit_confirm(
    value: Annotated[ElicitationResult[Any], Resolve(_trigger_job_edit_value)],
    source_id: str,
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(value, _trigger_job_marker(source_id))


def _trigger_job_confirmation(
    choice: Annotated[ElicitationResult[Any], Resolve(_trigger_job_edit_choice)],
    value: Annotated[ElicitationResult[Any], Resolve(_trigger_job_edit_value)],
    confirmed: Annotated[ElicitationResult[Any], Resolve(_trigger_job_edit_confirm)],
    source_id: str,
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(choice, value, confirmed, _trigger_job_marker(source_id))


def _update_schedule_policy_edit_choice(
    source_id: str, schedule_policy: dict[str, Any]
) -> Any:
    """Round 1: acknowledge, and say which field (if any) to change."""
    return _ask_choice(_update_schedule_policy_marker(source_id, schedule_policy))


def _update_schedule_policy_edit_value(
    choice: Annotated[
        ElicitationResult[Any], Resolve(_update_schedule_policy_edit_choice)
    ],
    source_id: str,
    schedule_policy: dict[str, Any],
) -> Any:
    """Round 2: the chosen field's new value -- asked only if one was chosen."""
    return _ask_value(
        choice, _update_schedule_policy_marker(source_id, schedule_policy)
    )


def _update_schedule_policy_edit_confirm(
    value: Annotated[
        ElicitationResult[Any], Resolve(_update_schedule_policy_edit_value)
    ],
    source_id: str,
    schedule_policy: dict[str, Any],
) -> Any:
    """Round 3: the composed request, shown and echoed -- asked only if editing."""
    return _ask_confirm(
        value, _update_schedule_policy_marker(source_id, schedule_policy)
    )


def _update_schedule_policy_confirmation(
    choice: Annotated[
        ElicitationResult[Any], Resolve(_update_schedule_policy_edit_choice)
    ],
    value: Annotated[
        ElicitationResult[Any], Resolve(_update_schedule_policy_edit_value)
    ],
    confirmed: Annotated[
        ElicitationResult[Any], Resolve(_update_schedule_policy_edit_confirm)
    ],
    source_id: str,
    schedule_policy: dict[str, Any],
) -> Any:
    """Round 4: verify the echo, then compose what executes (AC-243..245)."""
    return _compose_edit(
        choice,
        value,
        confirmed,
        _update_schedule_policy_marker(source_id, schedule_policy),
    )
