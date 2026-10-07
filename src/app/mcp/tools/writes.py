"""The write tools: every mutating operation on the MCP surface.

Each one builds the request body from its arguments and hands it to `_write`,
which calls the Core API under a fresh Idempotency-Key and attaches the audit
id. That is the whole path: there is no confirmation step.

The elicitation requirement was retired (ADR 0058, issue #270). It was never a
security boundary -- the boundary is the token, and a client that wants to
write can answer its own dialog -- while its cost fell on *every* client that
cannot answer an elicitation at all, which is most of them: those clients could
discover sixteen write tools and call none of them. What replaced it is in
`docs/api/mcp-tool-guide.md` section 3: the token scope, the descriptions and
annotations below (destructive tools say so in words *and* in
`destructiveHint`), the Idempotency-Key every call mints, and the audit row
that makes each write attributable afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.collector_registry import infer_source_type
from app.mcp.core_client import CoreAPIClient, _fresh_key
from app.mcp.rendering import (
    _attributed,
    _error_from,
    _truncate,
    write_annotations,
)


async def _write(
    core: CoreAPIClient,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    ok: tuple[int, ...] = (200,),
    extra: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute and attribute one write.

    Every write tool is this sequence, so it is written once.

    `ok` is the statuses that are *outcomes* rather than errors: a 409 from an
    already-enabled plugin and a 502 from a source that failed while triggering
    are answers, and reporting them as errors would hide the diagnosis. `extra`
    builds the keys a response carries beyond the Core API payload, from the
    status -- the audit note for add_subscription, the status code for
    set_plugin_status (which a caller needs precisely because a 409 is an
    outcome).
    """
    status, payload, audit_id = await core.request(
        method, path, json_body=body, params=params, idempotency_key=_fresh_key()
    )
    if status not in ok:
        return _error_from(payload, status)
    extras = extra(status) if extra is not None else {}
    return _truncate(_attributed({**payload, **extras}, audit_id))


def _read_state_change(
    is_read: bool | None, is_starred: bool | None
) -> dict[str, bool]:
    """The fields a read-state call actually asks to change.

    A read-state write is *partial*: an omitted field keeps its value, so the
    change is a subset of {is_read, is_starred} rather than two booleans.
    """
    pairs = (("is_read", is_read), ("is_starred", is_starred))
    return {name: value for name, value in pairs if value is not None}


def ingest_targets(items: list[dict[str, Any]]) -> list[str]:
    """The submitted items as the *targets* of an `ingest` work (issue #199).

    One item per target, as canonical JSON. The executor validates each target
    with the same model the endpoint uses (`app.ingest`), so a target that is
    not a usable item fails alone instead of refusing the batch.
    """
    return [json.dumps(item, separators=(",", ":"), sort_keys=True) for item in items]


def register_writes(server: MCPServer, core: CoreAPIClient) -> None:
    """Register the mutating tools on `server`, bound to `core`."""
    # The application's collector registry, for the same type inference the
    # executing request relies on (issue #84).
    registry = getattr(core, "registry", None)

    # ---------------------------------------------------------------- writes

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Add a subscription to a feed or page (and the origin it points "
            "at). This makes real outbound requests to a third party. "
            "schedule_policy is {type: fixed, interval: '30m'} or "
            "{type: adaptive, min: '15m', max: '24h'}. type is 'rss' (a feed "
            "URL), 'web' (a page a login-type collector serves, e.g. "
            "zhihu/telegram) or 'auto' (default: inferred from the URL; "
            "pass it when auto would guess wrong). "
            "pipeline_chain plus target_lang gate items until the chain "
            "completes -- see set_subscription_pipeline_chain."
        ),
    )
    async def add_subscription(
        url: str,
        schedule_policy: dict[str, Any],
        type: str = "auto",
        pipeline_chain: list[dict[str, Any]] | None = None,
        target_lang: str = "",
        folder: str = "",
    ) -> dict[str, Any]:
        requested = type.strip().lower()
        source_type = (
            infer_source_type(url, registry) if requested == "auto" else requested
        )
        return await _write(
            core,
            method="POST",
            path="/v1/sources",
            body={
                "url": url,
                "type": source_type,
                "schedule_policy": schedule_policy,
                "pipeline_chain": pipeline_chain,
                "target_lang": target_lang or None,
                # Empty means "the API's default folder", not "no folder":
                # omitting the key keeps the API's own default in charge.
                **({"folder": folder} if folder else {}),
            },
            ok=(200, 201),
            extra=lambda _status: {"audit": "recorded by the Core API"},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=False),
        description=(
            "Add a second subscription to an origin that already exists: the "
            "same facts under another policy -- a different folder, chain or "
            "language -- so one feed can be read untranslated in one place and "
            "translated in another. The origin id comes from list_sources. "
            "Idempotent per (origin, folder), and nothing new is collected: "
            "the origin is already subscribed. Items are not duplicated."
        ),
    )
    async def add_subscription_to_origin(
        origin_id: str,
        schedule_policy: dict[str, Any],
        folder: str = "",
        pipeline_chain: list[dict[str, Any]] | None = None,
        target_lang: str = "",
    ) -> dict[str, Any]:
        return await _write(
            core,
            method="POST",
            path=f"/v1/origins/{origin_id}/subscriptions",
            body={
                "schedule_policy": schedule_policy,
                "pipeline_chain": pipeline_chain,
                "target_lang": target_lang or None,
                # Empty means "the API's default folder", as in
                # add_subscription: omitting the key keeps the API in charge.
                **({"folder": folder} if folder else {}),
            },
            ok=(200, 201),
            extra=lambda _status: {"audit": "recorded by the Core API"},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Add several subscriptions in one call. They make real outbound "
            "requests to a third party. Options apply to the whole batch; "
            "`type` 'auto' resolves per URL as add_subscription does. Returns a job "
            "handle -- read each URL's outcome with work_units; an unusable "
            "one fails alone. Use add_subscription for a single subscription."
        ),
    )
    async def add_subscriptions(
        urls: list[str],
        schedule_policy: dict[str, Any],
        type: str = "auto",
        folder: str = "",
        pipeline_chain: list[dict[str, Any]] | None = None,
        target_lang: str = "",
        trust_level: str = "standard",
        parser_plugin_id: str = "",
    ) -> dict[str, Any]:
        return await _write(
            core,
            method="POST",
            path="/v1/works",
            body={
                "kind": "subscribe",
                "targets": urls,
                "params": {
                    # `auto` is resolved server-side per URL by the `subscribe`
                    # executor, so it travels as requested.
                    "type": type.strip().lower() or "auto",
                    "schedule_policy": schedule_policy,
                    "trust_level": trust_level,
                    "pipeline_chain": pipeline_chain,
                    "target_lang": target_lang or None,
                    "parser_plugin_id": parser_plugin_id or None,
                    # Empty means "the API's default folder", not "no folder".
                    **({"folder": folder} if folder else {}),
                },
            },
            # 202: the subscriptions are queued, not created. The handle is the
            # answer, and work_units is where each URL's outcome lands.
            ok=(202,),
            extra=lambda _status: {"audit": "recorded by the Core API"},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Fetch a list of article addresses into an existing origin. The "
            "deployment makes real outbound requests for every address. "
            "Returns a job handle (work_id, status, counts) immediately: the "
            "work keeps running, and the response does not grow with the list. "
            "Read per-address results with work_units. Each address must be on "
            "the origin's own host (off-host ones come back `rejected`); the "
            "collector must be able to fetch one address (rss can). Use this "
            "for addresses to fetch; submit_items for content you already "
            "fetched."
        ),
    )
    async def fetch_origin_items(
        source_id: str,
        urls: list[str],
        batch_size: int = 0,
        min_interval_seconds: float = 0.0,
    ) -> dict[str, Any]:
        return await _write(
            core,
            method="POST",
            path="/v1/works",
            body={
                "kind": "fetch",
                "targets": urls,
                "params": {
                    "source_id": source_id,
                    # Omitted rather than sent as 0: the server's own defaults
                    # and clamps are its business, and 0 is not a value it
                    # would accept (batch_size >= 1).
                    **({"batch_size": batch_size} if batch_size else {}),
                    **(
                        {"min_interval_seconds": min_interval_seconds}
                        if min_interval_seconds
                        else {}
                    ),
                },
            },
            # 202: the work is queued, not done. The handle is the answer.
            ok=(202,),
            extra=lambda _status: {"audit": "recorded by the Core API"},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Change a subscription's schedule policy. An aggressive interval "
            "puts real load on the origin site."
        ),
    )
    async def update_subscription_policy(
        source_id: str,
        schedule_policy: dict[str, Any],
    ) -> dict[str, Any]:
        return await _write(
            core,
            method="PATCH",
            path=f"/v1/sources/{source_id}",
            body={"schedule_policy": schedule_policy},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=False),
        description=(
            "Pause or resume a subscription. Pausing stops collection without "
            "deleting anything already gathered."
        ),
    )
    async def set_subscription_status(
        source_id: str,
        status: str,
    ) -> dict[str, Any]:
        return await _write(
            core,
            method="PATCH",
            path=f"/v1/sources/{source_id}",
            body={"status": status},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=False),
        description=(
            "Move a subscription to a sidebar folder (one folder per "
            "subscription; "
            "'default' is the unclassified home). Reflected in the Reader API's "
            "subscription/list, so the client sidebar follows on its next "
            "refresh."
        ),
    )
    async def set_subscription_folder(
        source_id: str,
        folder: str,
    ) -> dict[str, Any]:
        return await _write(
            core,
            method="PATCH",
            path=f"/v1/sources/{source_id}",
            body={"folder": folder},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Set a subscription's processing chain, and its target language "
            "with it. "
            "Items are held at the gate until the chain completes, and its "
            "processors can make real outbound requests and spend provider "
            "budget per item. An empty list clears the chain, so items publish "
            "as soon as they are stored; passing no target_lang sets it to "
            "none. Run backfill_subscription_chain afterwards for "
            "already-stored items; use add_subscription to set a chain on a "
            "new subscription."
        ),
    )
    async def set_subscription_pipeline_chain(
        source_id: str,
        pipeline_chain: list[dict[str, Any]],
        target_lang: str = "",
    ) -> dict[str, Any]:
        """Give an existing subscription a chain, or take its chain away.

        `add_subscription` sets a chain only at creation, and
        `backfill_subscription_chain` requires one to exist already -- so
        before this tool, translating the
        items of an origin that was subscribed without a chain meant deleting the
        subscription and starting over, which throws away its history and its read
        state (issue #71).
        """
        # Both fields are always sent: the endpoint's three-state semantics mean
        # an omitted `target_lang` would keep whatever was stored, while this
        # tool's contract is "an empty target_lang means none".
        return await _write(
            core,
            method="PATCH",
            path=f"/v1/sources/{source_id}",
            body={"pipeline_chain": pipeline_chain, "target_lang": target_lang},
        )

    @server.tool(
        # Not destructive: it removes nothing (ADR 0063). The annotation has to
        # say so, or a host surfaces an unsubscribe as an irreversible act.
        annotations=write_annotations(destructive=False, open_world=False),
        description=(
            "Stop collecting a subscription and keep what it collected. Nothing "
            "is deleted: the items, bodies and read state stay, the channel "
            "leaves the client, and subscribing to the same feed again brings it "
            "back under the same item ids. Use purge_origin to destroy content."
        ),
    )
    async def remove_subscription(source_id: str) -> dict[str, Any]:
        return await _write(
            core,
            method="DELETE",
            path=f"/v1/sources/{source_id}",
        )

    @server.tool(
        annotations=write_annotations(destructive=True, open_world=False),
        description=(
            "Destroy an origin and everything it collected, permanently: items, "
            "bodies, read state, translations, fetch records and the collect job. "
            "Re-subscribing starts over. `reason` is required and stays in the "
            "audit record; remove_subscription is the reversible answer."
        ),
    )
    async def purge_origin(origin_id: str, reason: str) -> dict[str, Any]:
        return await _write(
            core,
            method="DELETE",
            path=f"/v1/origins/{origin_id}:purge",
            params={"reason": reason},
        )

    @server.tool(
        annotations=write_annotations(destructive=True, open_world=False),
        description=(
            "Permanently delete specific items by id. The bodies, read state "
            "and processor products go with them. Use this to clean up items "
            "that were fetched wrongly or are no longer wanted; ids come from "
            "search_items. Not reversible. Returns a job handle -- read each "
            "id's outcome with work_units; one already gone is `skipped`."
        ),
    )
    async def delete_items(item_ids: list[str]) -> dict[str, Any]:
        return await _write(
            core,
            method="POST",
            path="/v1/works",
            body={"kind": "delete", "targets": item_ids, "params": {}},
            # 202: the deletion is queued, not done. The handle is the answer.
            ok=(202,),
        )

    @server.tool(
        annotations=write_annotations(destructive=True, open_world=False),
        description=(
            "Delete an origin's items while keeping the subscription. Use this "
            "after a bad crawl: the origin keeps its crawl position, so "
            "collection resumes instead of re-reading history. Optionally "
            "narrow by since/until; pass include_published=false to spare items "
            "a client has already seen. Not reversible. Returns a job handle -- "
            "read each item's outcome with work_units."
        ),
    )
    async def delete_origin_items(
        source_id: str,
        since: str = "",
        until: str = "",
        include_published: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"include_published": include_published}
        if since:
            body["since"] = since
        if until:
            body["until"] = until
        return await _write(
            core,
            method="POST",
            path=f"/v1/sources/{source_id}/items:delete",
            body=body,
            # 200 when nothing matches the selection (no job to submit) and 202
            # when something does; both are answers.
            ok=(200, 202),
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=False),
        description=(
            "Mark one item read/unread or starred/unstarred. Pass at least one "
            "of is_read/is_starred; an omitted field keeps its value. The "
            "item's content, guid and publish status are untouched, and the "
            "Reader API reports the same state."
        ),
    )
    async def set_item_read_state(
        item_id: str,
        is_read: bool | None = None,
        is_starred: bool | None = None,
    ) -> dict[str, Any]:
        change = _read_state_change(is_read, is_starred)
        if not change:
            # Refused before the call: a no-op spends a round trip to learn
            # what `/v1` would say anyway. Same code, so an agent branches on
            # one vocabulary (issue #218).
            return {
                "error": "empty_patch",
                "message": (
                    "Nothing to change: is_read and is_starred are both absent."
                ),
                "suggestion": (
                    "Pass is_read, is_starred, or both. An omitted field keeps "
                    "its current value."
                ),
            }
        return await _write(
            core,
            method="PATCH",
            path=f"/v1/items/{item_id}/read-state",
            body=change,
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Fetch a source now, outside its schedule. This issues a real "
            "request to the origin."
        ),
    )
    async def trigger_job(source_id: str) -> dict[str, Any]:
        # A 502 here is a real outcome to report, not a transport failure: the
        # run happened and the source failed, with the error_class explaining
        # which. Returning it as an error would hide the diagnosis.
        return await _write(
            core,
            method="POST",
            path=f"/v1/sources/{source_id}:trigger",
            ok=(200, 502),
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Queue processing-chain products (e.g. translations) for the "
            "origin's items stored before the chain was enabled. It spends "
            "provider budget per item, and the subscription must have a "
            "pipeline_chain configured. "
            "Returns a job handle -- read each item's outcome with work_units."
        ),
    )
    async def backfill_subscription_chain(source_id: str) -> dict[str, Any]:
        return await _write(
            core,
            method="POST",
            path=f"/v1/sources/{source_id}:backfill-chain",
            # 200 when the source has nothing stored (no job to submit) and 202
            # when it does; both are answers.
            ok=(200, 202),
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Requeue failed processing-chain rows (e.g. after fixing the "
            "provider key). The attempt budget restarts and the provider "
            "charges again. Pass enrichment_id to retry one row and get a "
            "receipt back, or source_id to retry every failed row of that "
            "source and get a job handle (read the per-row outcomes with "
            "work_units)."
        ),
    )
    async def retry_enrichments(
        enrichment_id: str = "",
        source_id: str = "",
    ) -> dict[str, Any]:
        """One row, or one source's failed rows.

        Two granularities of one intent, so one tool: a source can hold 23
        permanently failed products while the caller wants three retried, and
        `list_enrichments` already returns the ids that name one row.
        """
        if enrichment_id:
            path = f"/v1/enrichments/{enrichment_id}:retry"
        elif source_id:
            path = f"/v1/sources/{source_id}:retry-enrichments"
        else:
            # Not a protocol error: the model can fix this with a tool it
            # already has, and the message says which call returns the id.
            return {
                "error": "missing_target",
                "suggestion": (
                    "Pass enrichment_id (one row) or source_id (every failed "
                    "row of that source)."
                ),
                "next_actions": ["list_enrichments", "list_sources"],
            }
        return await _write(
            core,
            method="POST",
            path=path,
            # 200 when the source has nothing to requeue (no job to submit) and
            # 202 when it does; both are answers.
            ok=(200, 202),
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Enable or disable a collector plugin. A disabled plugin stops "
            "collecting every source it serves, and enabling one resumes real "
            "outbound requests to the sites those sources point at."
        ),
    )
    async def set_plugin_status(plugin_id: str, status: str) -> dict[str, Any]:
        operation = ":enable" if status == "enabled" else ":disable"
        return await _write(
            core,
            method="POST",
            path=f"/v1/plugins/{plugin_id}{operation}",
            # A 409 means "already in that state", which is an outcome; the
            # status code travels with the result so the caller can tell.
            ok=(200, 409),
            extra=lambda core_status: {"status_code": core_status},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Push items into an agent source (A3, ADR 0022): pass each item's "
            "title, url, published_at and content; Pearss stores it through the "
            "same pipeline as collected items. The target must be a source of "
            "type 'agent' — create one with add_subscription and a url like "
            "'agent://newsletter-intake'; pushing into an rss or web source is "
            "refused, which would mix one origin's content into another's. This "
            "adds content to the corpus, and its chain spends provider budget "
            "on the submitted URLs. Returns a job handle; read each item's "
            "outcome with work_units."
        ),
    )
    async def submit_items(
        source_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await _write(
            core,
            method="POST",
            path="/v1/works",
            body={
                "kind": "ingest",
                "targets": ingest_targets(items),
                "params": {"source_id": source_id},
            },
            # 202: the items are queued, not stored. The handle is the answer,
            # and work_units is where each one's outcome lands.
            ok=(202,),
        )
