"""The write tools: every mutating operation on the MCP surface.

Each one elicits first (the confirmation machinery in app.mcp.confirmation),
refuses when the user declines or cannot be asked, checks the confirmed
arguments against the ones about to execute, and only then calls the Core API
under a fresh Idempotency-Key. The order is the contract: confirmation is of
the same request that executes, never a rubber stamp (ADR 0010, and
docs/api/mcp-tool-guide.md's presentation rules).

The bodies are the closures that lived in build_mcp_server, moved verbatim;
`register_writes` is what build_mcp_server calls instead of inlining them.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.resolve import Resolve

from app.mcp.confirmation import (
    Confirmation,
    _add_source_confirmation,
    _backfill_source_confirmation,
    _comma_join,
    _confirmation_verdict,
    _delete_items_confirmation,
    _delete_source_items_confirmation,
    _matches,
    _remove_source_confirmation,
    _retry_enrichments_confirmation,
    _set_plugin_status_confirmation,
    _set_source_folder_confirmation,
    _set_source_status_confirmation,
    _submit_items_confirmation,
    _trigger_job_confirmation,
    _update_schedule_policy_confirmation,
)
from app.mcp.core_client import CoreAPIClient, _fresh_key, _infer_source_type
from app.mcp.rendering import (
    _attributed,
    _error_from,
    _truncate,
    write_annotations,
)


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
        requested = type.strip().lower()
        source_type = _infer_source_type(url) if requested == "auto" else requested
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        # The confirmed values must be the ones executed. A confirmation of one
        # request followed by execution of another would make the dialog a
        # rubber stamp.
        if not (
            _matches(data["url"], url)
            and _matches(data["type"], source_type)
            and _matches(data["schedule_policy"], schedule_policy)
            and _matches(data["pipeline_chain"], pipeline_chain)
            and _matches(data["target_lang"], target_lang)
            and _matches(data["folder"], folder)
        ):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        status, payload = await core.request(
            "POST",
            "/v1/sources",
            json_body={
                "url": url,
                "type": source_type,
                "schedule_policy": schedule_policy,
                "pipeline_chain": pipeline_chain,
                "target_lang": target_lang or None,
                # Empty means "the API's default folder", not "no folder":
                # omitting the key keeps the API's own default in charge.
                **({"folder": folder} if folder else {}),
            },
            idempotency_key=_fresh_key(),
        )
        if status not in (200, 201):
            return _error_from(payload, status)
        return _truncate(
            _attributed(
                {**payload, "audit": "recorded by the Core API"}, core.last_audit_id
            )
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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not (
            _matches(data["source_id"], source_id)
            and _matches(data["schedule_policy"], schedule_policy)
        ):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        status, payload = await core.request(
            "PATCH",
            f"/v1/sources/{source_id}",
            json_body={"schedule_policy": schedule_policy},
            idempotency_key=_fresh_key(),
        )
        if status != 200:
            return _error_from(payload, status)
        return _truncate(_attributed(payload, core.last_audit_id))

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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not (
            _matches(data["source_id"], source_id) and _matches(data["status"], status)
        ):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        code, payload = await core.request(
            "PATCH",
            f"/v1/sources/{source_id}",
            json_body={"status": status},
            idempotency_key=_fresh_key(),
        )
        if code != 200:
            return _error_from(payload, code)
        return _truncate(_attributed(payload, core.last_audit_id))

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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not (
            _matches(data["source_id"], source_id) and _matches(data["folder"], folder)
        ):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        code, payload = await core.request(
            "PATCH",
            f"/v1/sources/{source_id}",
            json_body={"folder": folder},
            idempotency_key=_fresh_key(),
        )
        if code != 200:
            return _error_from(payload, code)
        return _truncate(_attributed(payload, core.last_audit_id))

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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not _matches(data["source_id"], source_id):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        status, payload = await core.request(
            "DELETE",
            f"/v1/sources/{source_id}",
            idempotency_key=_fresh_key(),
        )
        if status != 200:
            return _error_from(payload, status)
        return _truncate(_attributed(payload, core.last_audit_id))

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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        # The schema carries one string, so the list arrives as a comma-joined
        # value. Comparing the parse back to the request is what keeps the
        # dialog a confirmation rather than a formality.
        if not _matches(data["item_ids"], _comma_join(item_ids)):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        status, payload = await core.request(
            "POST",
            "/v1/items:delete",
            json_body={"item_ids": item_ids},
            idempotency_key=_fresh_key(),
        )
        if status != 200:
            return _error_from(payload, status)
        return _truncate(_attributed(payload, core.last_audit_id))

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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not (
            _matches(data["source_id"], source_id)
            and _matches(data["since"], since)
            and _matches(data["until"], until)
            and _matches(data["include_published"], include_published)
        ):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        body: dict[str, Any] = {"include_published": include_published}
        if since:
            body["since"] = since
        if until:
            body["until"] = until
        status, payload = await core.request(
            "POST",
            f"/v1/sources/{source_id}/items:delete",
            json_body=body,
            idempotency_key=_fresh_key(),
        )
        if status != 200:
            return _error_from(payload, status)
        return _truncate(_attributed(payload, core.last_audit_id))

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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not _matches(data["source_id"], source_id):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        # A 502 here is a real outcome to report, not a transport failure: the
        # run happened and the source failed, with the error_class explaining
        # which. Returning it as an error would hide the diagnosis.
        status, payload = await core.request(
            "POST",
            f"/v1/sources/{source_id}:trigger",
            idempotency_key=_fresh_key(),
        )
        if status not in (200, 502):
            return _error_from(payload, status)
        return _truncate(_attributed(payload, core.last_audit_id))

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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not _matches(data["source_id"], source_id):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        status, payload = await core.request(
            "POST",
            f"/v1/sources/{source_id}:backfill-chain",
            idempotency_key=_fresh_key(),
        )
        if status not in (200, 201):
            return _error_from(payload, status)
        return _truncate(_attributed(payload, core.last_audit_id))

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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not _matches(data["source_id"], source_id):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        status, payload = await core.request(
            "POST",
            f"/v1/sources/{source_id}:retry-enrichments",
            idempotency_key=_fresh_key(),
        )
        if status != 200:
            return _error_from(payload, status)
        return _truncate(_attributed(payload, core.last_audit_id))

    @server.tool(
        annotations=write_annotations(destructive=False, open_world=True),
        description=(
            "Enable or disable a collector plugin. Requires confirmation: a "
            "disabled plugin stops collecting every source it serves."
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
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        if not (
            _matches(data["plugin_id"], plugin_id) and _matches(data["status"], status)
        ):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        operation = ":enable" if status == "enabled" else ":disable"
        core_status, payload = await core.request(
            "POST",
            f"/v1/plugins/{plugin_id}{operation}",
            idempotency_key=_fresh_key(),
        )
        if core_status not in (200, 409):
            return _error_from(payload, core_status)
        return _truncate(
            _attributed({**payload, "status_code": core_status}, core.last_audit_id)
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
            "Requires confirmation: this adds content to the corpus."
        ),
    )
    async def submit_items(
        source_id: str,
        items: list[dict[str, Any]],
        ctx: Context,
        confirmed: Annotated[Confirmation | None, Resolve(_submit_items_confirmation)],
    ) -> dict[str, Any]:
        data = _confirmation_verdict(confirmed)
        if data is None:
            return {
                "declined": True,
                "reason": (
                    "not confirmed: the user declined, or this client does not "
                    "support elicitation, which writes require"
                ),
            }
        # The count is what the user saw; comparing it catches a list that
        # changed between the prompt and the call, which the titles alone
        # would not if two items were swapped.
        if not (
            _matches(data["source_id"], source_id)
            and _matches(data["item_count"], str(len(items)))
        ):
            return {
                "aborted": True,
                "reason": "confirmed arguments differ from requested",
            }
        status, payload = await core.request(
            "POST",
            f"/v1/sources/{source_id}:ingest",
            json_body={"items": items},
            idempotency_key=_fresh_key(),
        )
        if status not in (200, 201):
            return _error_from(payload, status)
        return _truncate(_attributed(payload, core.last_audit_id))
