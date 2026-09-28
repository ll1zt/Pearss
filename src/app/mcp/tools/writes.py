"""The write tools: every mutating operation on the MCP surface.

Each one elicits first (the confirmation machinery in app.mcp.confirmation),
refuses when the user declines or cannot be asked, checks the confirmed
arguments against the ones about to execute, and only then calls the Core API
under a fresh Idempotency-Key. The order is the contract: confirmation is of
the same request that executes, never a rubber stamp (ADR 0010, and
docs/api/mcp-tool-guide.md's presentation rules).

The bodies are the closures that lived in build_mcp_server; `register_writes`
is what build_mcp_server calls instead of inlining them. Each one is now three
things: derive the fields it confirms, hand them to `_guarded_write`, and
describe the request. The confirm/compare/execute/attribute sequence lives in
that one helper because writing it out twelve times is how a field went
uncompared (issue #67, and then #76 for the shape itself).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.resolve import Resolve

from app.mcp.confirmation import (
    Confirmation,
    _add_source_confirmation,
    _add_source_fields,
    _backfill_source_confirmation,
    _backfill_source_fields,
    _delete_items_confirmation,
    _delete_items_fields,
    _delete_source_items_confirmation,
    _delete_source_items_fields,
    _remove_source_confirmation,
    _remove_source_fields,
    _retry_enrichments_confirmation,
    _retry_enrichments_fields,
    _set_plugin_status_confirmation,
    _set_plugin_status_fields,
    _set_source_folder_confirmation,
    _set_source_folder_fields,
    _set_source_status_confirmation,
    _set_source_status_fields,
    _submit_items_confirmation,
    _submit_items_fields,
    _trigger_job_confirmation,
    _trigger_job_fields,
    _update_schedule_policy_confirmation,
    _update_schedule_policy_fields,
    _write_verdict,
)
from app.mcp.core_client import CoreAPIClient, _fresh_key
from app.mcp.rendering import (
    _attributed,
    _error_from,
    _truncate,
    write_annotations,
)


async def _guarded_write(
    core: CoreAPIClient,
    *,
    confirmed: Any,
    fields: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    ok: tuple[int, ...] = (200,),
    extra: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Confirm, compare, execute and attribute one write.

    Every write tool is this sequence, so it is written once. `confirmed` and
    `fields` are the whole contract between a tool and its confirmation: the
    tool hands over the same `fields` its resolver put in the dialog, so
    "the confirmation covers what executes" is one computation used twice
    rather than two lists kept in step by hand. That is the difference #76 is
    about -- twelve hand-written comparisons are twelve chances to miss a
    field, and #67 was one of them.

    `ok` is the statuses that are *outcomes* rather than errors: a 409 from an
    already-enabled plugin and a 502 from a source that failed while
    triggering are answers, and reporting them as errors would hide the
    diagnosis. `extra` builds the keys a response carries beyond the Core API
    payload, from the status -- the audit note for add_source, the status code
    for set_plugin_status (which a caller needs precisely because a 409 is an
    outcome).
    """
    verdict = _write_verdict(confirmed, fields)
    if verdict is not None:
        return verdict
    status, payload, audit_id = await core.request(
        method, path, json_body=body, idempotency_key=_fresh_key()
    )
    if status not in ok:
        return _error_from(payload, status)
    extras = extra(status) if extra is not None else {}
    return _truncate(_attributed({**payload, **extras}, audit_id))


def register_writes(server: MCPServer, core: CoreAPIClient) -> None:
    """Register the mutating tools on `server`, bound to `core`."""
    # ---------------------------------------------------------------- writes

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Add a source. Requires user confirmation: this makes real "
            "outbound requests to a third party. schedule_policy is "
            "{type: fixed, interval: '30m'} or "
            "{type: adaptive, min: '15m', max: '24h'}. source type is "
            "'rss' (a feed URL), 'web' (a page a login-type collector "
            "serves, e.g. zhihu/telegram), or 'auto' (default): the URL is "
            "checked against the collectors and a non-rss match suggests "
            "'web'. Pass type explicitly when auto would guess wrong. "
            "pipeline_chain (a list of {plugin_id, kind, lang?, required?}) "
            "plus target_lang gates items until the chain completes: e.g. "
            "one translate/translation spec with target_lang 'en', or "
            "one fulltext/fulltext spec to fetch missing article bodies."
        ),
    )
    async def add_source(
        url: str,
        schedule_policy: dict[str, Any],
        ctx: Context,
        confirmed: Annotated[Confirmation | None, Resolve(_add_source_confirmation)],
        type: str = "auto",
        pipeline_chain: list[dict[str, Any]] | None = None,
        target_lang: str = "",
        folder: str = "",
    ) -> dict[str, Any]:
        fields = _add_source_fields(
            url, schedule_policy, type, pipeline_chain, target_lang, folder
        )
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path="/v1/sources",
            body={
                "url": url,
                "type": fields["type"],
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
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Change a source's schedule policy. Requires confirmation: an "
            "aggressive interval puts real load on the origin site."
        ),
    )
    async def update_schedule_policy(
        source_id: str,
        schedule_policy: dict[str, Any],
        ctx: Context,
        confirmed: Annotated[
            Confirmation | None, Resolve(_update_schedule_policy_confirmation)
        ],
    ) -> dict[str, Any]:
        fields = _update_schedule_policy_fields(source_id, schedule_policy)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="PATCH",
            path=f"/v1/sources/{source_id}",
            body={"schedule_policy": schedule_policy},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=False),
        description=(
            "Pause or resume a source. Requires confirmation. Pausing stops "
            "collection without deleting anything already gathered."
        ),
    )
    async def set_source_status(
        source_id: str,
        status: str,
        ctx: Context,
        confirmed: Annotated[
            Confirmation | None, Resolve(_set_source_status_confirmation)
        ],
    ) -> dict[str, Any]:
        fields = _set_source_status_fields(source_id, status)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="PATCH",
            path=f"/v1/sources/{source_id}",
            body={"status": status},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=False),
        description=(
            "Move a source to a sidebar folder (one folder per source; "
            "'default' is the unclassified home). Requires confirmation. "
            "Reflected in the Reader API's subscription/list, so the client "
            "sidebar follows on its next refresh."
        ),
    )
    async def set_source_folder(
        source_id: str,
        folder: str,
        ctx: Context,
        confirmed: Annotated[
            Confirmation | None, Resolve(_set_source_folder_confirmation)
        ],
    ) -> dict[str, Any]:
        fields = _set_source_folder_fields(source_id, folder)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="PATCH",
            path=f"/v1/sources/{source_id}",
            body={"folder": folder},
        )

    @server.tool(
        annotations=write_annotations(destructive=True, open_world=False),
        description=(
            "Delete a source and everything it collected. Requires "
            "confirmation. Permanent: the source row, its items, their stored "
            "bodies, read state and translations are all removed, and "
            "re-subscribing the same feed starts a new source with no history. "
            "Check the size first with search_items (its `total` counts the "
            "source's items). Use delete_source_items to clear a bad crawl and "
            "keep the subscription, or set_source_status to disable it "
            "instead."
        ),
    )
    async def remove_source(
        source_id: str,
        ctx: Context,
        confirmed: Annotated[Confirmation | None, Resolve(_remove_source_confirmation)],
    ) -> dict[str, Any]:
        fields = _remove_source_fields(source_id)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="DELETE",
            path=f"/v1/sources/{source_id}",
        )

    @server.tool(
        annotations=write_annotations(destructive=True, open_world=False),
        description=(
            "Permanently delete specific items by id. Requires confirmation. "
            "The bodies, read state and processor products go with them. Use "
            "this to clean up items that were fetched wrongly or are no "
            "longer wanted; ids come from search_items. Not reversible."
        ),
    )
    async def delete_items(
        item_ids: list[str],
        ctx: Context,
        confirmed: Annotated[Confirmation | None, Resolve(_delete_items_confirmation)],
    ) -> dict[str, Any]:
        fields = _delete_items_fields(item_ids)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path="/v1/items:delete",
            body={"item_ids": item_ids},
        )

    @server.tool(
        annotations=write_annotations(destructive=True, open_world=False),
        description=(
            "Delete a source's items while keeping the subscription. Requires "
            "confirmation. Use this after a bad crawl: the source keeps its "
            "crawl position, so collection resumes instead of re-reading "
            "history. Optionally narrow by since/until; pass "
            "include_published=false to spare items a client has already "
            "seen. Not reversible."
        ),
    )
    async def delete_source_items(
        source_id: str,
        ctx: Context,
        since: str = "",
        until: str = "",
        include_published: bool = True,
        confirmed: Annotated[
            Confirmation | None, Resolve(_delete_source_items_confirmation)
        ] = None,
    ) -> dict[str, Any]:
        fields = _delete_source_items_fields(source_id, since, until, include_published)
        body: dict[str, Any] = {"include_published": include_published}
        if since:
            body["since"] = since
        if until:
            body["until"] = until
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path=f"/v1/sources/{source_id}/items:delete",
            body=body,
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Fetch a source now, outside its schedule. Requires confirmation: "
            "this issues a real request to the origin."
        ),
    )
    async def trigger_job(
        source_id: str,
        ctx: Context,
        confirmed: Annotated[Confirmation | None, Resolve(_trigger_job_confirmation)],
    ) -> dict[str, Any]:
        fields = _trigger_job_fields(source_id)
        # A 502 here is a real outcome to report, not a transport failure: the
        # run happened and the source failed, with the error_class explaining
        # which. Returning it as an error would hide the diagnosis.
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path=f"/v1/sources/{source_id}:trigger",
            ok=(200, 502),
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Queue processing-chain products (e.g. translations) for items "
            "stored before the chain was enabled. Requires confirmation: it "
            "spends provider budget per item. The source must have a "
            "pipeline_chain configured."
        ),
    )
    async def backfill_source(
        source_id: str,
        ctx: Context,
        confirmed: Annotated[
            Confirmation | None, Resolve(_backfill_source_confirmation)
        ],
    ) -> dict[str, Any]:
        fields = _backfill_source_fields(source_id)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path=f"/v1/sources/{source_id}:backfill-chain",
            ok=(200, 201),
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Requeue failed processing-chain rows for one source (e.g. after "
            "fixing the provider key). Requires confirmation: rows restart "
            "the attempt budget and the provider charges again."
        ),
    )
    async def retry_enrichments(
        source_id: str,
        ctx: Context,
        confirmed: Annotated[
            Confirmation | None, Resolve(_retry_enrichments_confirmation)
        ],
    ) -> dict[str, Any]:
        fields = _retry_enrichments_fields(source_id)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path=f"/v1/sources/{source_id}:retry-enrichments",
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Enable or disable a collector plugin. Requires confirmation: a "
            "disabled plugin stops collecting every source it serves, and "
            "enabling one resumes real outbound requests to the sites those "
            "sources point at."
        ),
    )
    async def set_plugin_status(
        plugin_id: str,
        status: str,
        ctx: Context,
        confirmed: Annotated[
            Confirmation | None, Resolve(_set_plugin_status_confirmation)
        ],
    ) -> dict[str, Any]:
        fields = _set_plugin_status_fields(plugin_id, status)
        operation = ":enable" if status == "enabled" else ":disable"
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
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
            "Push items into an agent source (A3, ADR 0022). Use this to store "
            "content you have gathered elsewhere: pass the title, the source "
            "URL, the publication time and the body, and Pearss stores it "
            "through the same pipeline as collected items.\n\n"
            "The target must be a source of type 'agent' -- create one first "
            "with add_source using a url like 'agent://newsletter-intake'. "
            "Pushing into an rss or web source is refused, because that would "
            "mix content from one origin into another's provenance.\n\n"
            "`source_id` is the path, never an argument: an item's source is "
            "not yours to choose. Items are visible to readers as soon as they "
            "are stored unless the source has a processing chain. Submitted "
            "HTML is sanitised on the way in, so it does not need pre-cleaning, "
            "and what you send is treated as data rather than trusted.\n\n"
            "The response reports what happened per item: branch on each "
            "result's `action`, not on the status code. A 201 means the "
            "submission was accepted, not that every item was new -- "
            "`skip_same_source` and `suppress` mean an identical item already "
            "existed. Two items differing only in external_id still count as "
            "one when their content is identical, so an empty body does not "
            "distinguish them.\n\n"
            "Requires confirmation: this adds content to the corpus, where "
            "the source's processing chain can make real outbound requests to "
            "the submitted URLs and spend provider budget on it."
        ),
    )
    async def submit_items(
        source_id: str,
        items: list[dict[str, Any]],
        ctx: Context,
        confirmed: Annotated[Confirmation | None, Resolve(_submit_items_confirmation)],
    ) -> dict[str, Any]:
        fields = _submit_items_fields(source_id, items)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path=f"/v1/sources/{source_id}:ingest",
            body={"items": items},
            ok=(200, 201),
        )
