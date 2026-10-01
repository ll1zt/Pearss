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
    _add_sources_confirmation,
    _add_sources_fields,
    _backfill_source_confirmation,
    _backfill_source_fields,
    _delete_items_confirmation,
    _delete_items_fields,
    _delete_source_items_confirmation,
    _delete_source_items_fields,
    _fetch_work_confirmation,
    _fetch_work_fields,
    _remove_source_confirmation,
    _remove_source_fields,
    _retry_enrichments_confirmation,
    _retry_enrichments_fields,
    _set_plugin_status_confirmation,
    _set_plugin_status_fields,
    _set_source_folder_confirmation,
    _set_source_folder_fields,
    _set_source_pipeline_chain_confirmation,
    _set_source_pipeline_chain_fields,
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
    # The application's collector registry, read once so the tool body derives
    # the same fields the confirmation resolver derives (issue #84).
    registry = getattr(core, "registry", None)

    # ---------------------------------------------------------------- writes

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Add a source. Requires user confirmation: this makes real "
            "outbound requests to a third party. schedule_policy is "
            "{type: fixed, interval: '30m'} or "
            "{type: adaptive, min: '15m', max: '24h'}. type is 'rss' (a feed "
            "URL), 'web' (a page a login-type collector serves, e.g. "
            "zhihu/telegram) or 'auto' (default: inferred from the URL "
            "against the collectors; pass it explicitly when auto would guess "
            "wrong). pipeline_chain plus target_lang gate items until the "
            "chain completes -- see set_source_pipeline_chain for the spec "
            "shape."
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
            url,
            schedule_policy,
            type,
            pipeline_chain,
            target_lang,
            folder,
            registry=registry,
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
            "Add several sources in one call. Requires user confirmation: this "
            "makes real outbound requests to a third party for every URL. "
            "Options apply to the whole batch, and `type` 'auto' is resolved per "
            "URL exactly as add_source resolves it. Results are per item: a URL "
            "that cannot be used fails alone and the rest are still created. "
            "Use add_source for a single source, where the dialog names it "
            "exactly."
        ),
    )
    async def add_sources(
        urls: list[str],
        schedule_policy: dict[str, Any],
        ctx: Context,
        confirmed: Annotated[Confirmation | None, Resolve(_add_sources_confirmation)],
        type: str = "auto",
        folder: str = "",
        pipeline_chain: list[dict[str, Any]] | None = None,
        target_lang: str = "",
        trust_level: str = "standard",
        parser_plugin_id: str = "",
    ) -> dict[str, Any]:
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
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path="/v1/sources:batch",
            body={
                "urls": urls,
                "type": fields["type"],
                "schedule_policy": schedule_policy,
                "trust_level": trust_level,
                "pipeline_chain": pipeline_chain,
                "target_lang": target_lang or None,
                "parser_plugin_id": parser_plugin_id or None,
                # Empty means "the API's default folder", not "no folder".
                **({"folder": folder} if folder else {}),
            },
            extra=lambda _status: {"audit": "recorded by the Core API"},
        )

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Fetch a list of article addresses into an existing source. "
            "Requires confirmation: the deployment makes real outbound requests "
            "for every address. Returns a job handle (work_id, status, counts) "
            "immediately -- the work keeps running after this call, and the "
            "response does not grow with the list. Read per-address results "
            "with work_units. Each address must be on the source's own host "
            "(off-host ones come back `rejected`), and the source's collector "
            "must be able to fetch one address (rss can). Use this when you "
            "have addresses to fetch; use submit_items when you already "
            "fetched the content yourself."
        ),
    )
    async def fetch_source_items(
        source_id: str,
        urls: list[str],
        ctx: Context,
        confirmed: Annotated[Confirmation | None, Resolve(_fetch_work_confirmation)],
        batch_size: int = 0,
        min_interval_seconds: float = 0.0,
    ) -> dict[str, Any]:
        fields = _fetch_work_fields(source_id, urls, batch_size, min_interval_seconds)
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path="/v1/works",
            body={
                "kind": fields["kind"],
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
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Set a source's processing chain, and its target language with it. "
            "Requires confirmation: items are held at the gate until the chain "
            "completes, and its processors can make real outbound requests and "
            "spend provider budget per item. An empty list clears the chain, so "
            "items publish as soon as they are stored; passing no target_lang "
            "sets it to none, which is what the confirmation says. Run "
            "backfill_source afterwards for already-stored items; use "
            "add_source to set a chain on a new source."
        ),
    )
    async def set_source_pipeline_chain(
        source_id: str,
        pipeline_chain: list[dict[str, Any]],
        ctx: Context,
        confirmed: Annotated[
            Confirmation | None, Resolve(_set_source_pipeline_chain_confirmation)
        ],
        target_lang: str = "",
    ) -> dict[str, Any]:
        """Give an existing source a chain, or take its chain away.

        `add_source` sets a chain only at creation, and `backfill_source`
        requires one to exist already -- so before this tool, translating the
        items of a source that was subscribed without a chain meant deleting the
        source and starting over, which throws away its history and its read
        state (issue #71).
        """
        fields = _set_source_pipeline_chain_fields(
            source_id, pipeline_chain, target_lang
        )
        # Both fields are always sent: the endpoint's three-state semantics mean
        # an omitted `target_lang` would keep whatever was stored, and the
        # confirmation says "none". Sending what was confirmed is the rule.
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="PATCH",
            path=f"/v1/sources/{source_id}",
            body={"pipeline_chain": pipeline_chain, "target_lang": target_lang},
        )

    @server.tool(
        annotations=write_annotations(destructive=True, open_world=False),
        description=(
            "Delete a source and everything it collected. Requires "
            "confirmation. Permanent: the source row, its items, stored bodies, "
            "read state and translations are removed, and re-subscribing the "
            "same feed starts a new source with no history. Check the size "
            "first with search_items (its `total` counts the source's items). "
            "Use delete_source_items to clear a bad crawl and keep the "
            "subscription, or set_source_status to disable it instead."
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
            "Requeue failed processing-chain rows (e.g. after fixing the "
            "provider key). Requires confirmation: the attempt budget restarts "
            "and the provider charges again. Pass enrichment_id to retry one "
            "row, or source_id for every failed row of that source."
        ),
    )
    async def retry_enrichments(
        ctx: Context,
        confirmed: Annotated[
            Confirmation | None, Resolve(_retry_enrichments_confirmation)
        ],
        enrichment_id: str = "",
        source_id: str = "",
    ) -> dict[str, Any]:
        """One row, or one source's failed rows.

        Two granularities of one intent, so one tool: a source can hold 23
        permanently failed products while the caller wants three retried, and
        `list_enrichments` already returns the ids that name one row.
        """
        fields = _retry_enrichments_fields(enrichment_id, source_id)
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
        return await _guarded_write(
            core,
            confirmed=confirmed,
            fields=fields,
            method="POST",
            path=path,
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
            "Push items into an agent source (A3, ADR 0022): pass each item's "
            "title, url, published_at and content; Pearss stores it through the "
            "same pipeline as collected items. The target must be a source of "
            "type 'agent' — create one with add_source and a url like "
            "'agent://newsletter-intake'. Pushing into an rss or web source is "
            "refused: that would mix one origin's content into another's "
            "provenance. Items are visible to readers as soon as stored, unless "
            "the source has a chain. Requires confirmation: this adds content "
            "to the corpus, and the chain can make real outbound requests to "
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
