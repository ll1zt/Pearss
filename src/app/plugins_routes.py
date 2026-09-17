"""Core API: /v1/plugins (P2 lifecycle surface, ADR 0017).

Reads list the verified manifests; the enable/disable write flips the
lifecycle status the collection path honours. Import/uninstall/reload are
explicit 409 placeholders: builtin plugins arrive with Pearss, and the
third-party loader that gives those operations meaning is P2b — saying so
beats pretending the endpoint exists.

The writes follow the API-wide idempotency contract (AGENTS.md 42): an
Idempotency-Key is required, the first result is stored, and a retry with
the same key replays it. The 409 `plugin_status_unchanged` is reserved for
a *deliberate* no-op — the caller asked without a replayable key, or with a
fresh one, to do what is already done.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from adapters.correlation import current as current_correlation_id
from adapters.repository import (
    list_plugin_rows,
    plugin_status,
    set_plugin_status,
    timestamp,
    upsert_plugin,
)
from app.api_conventions import api_error, idempotency_key, remember, replay_or_none
from app.auth import Principal, read_required, write_required
from app.collector_registry import by_name, plugins_for_surface
from core.enums import PluginStatus

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import sessionmaker

router = APIRouter(prefix="/v1", tags=["plugins"])


def _plugin_representation(manifest: Any, status: str) -> dict[str, Any]:
    """The resource representation every plugin response carries.

    Writes return the same shape reads do (write-after-read consistency):
    the client needs the checksum to know which manifest the decision
    applied to, without a second request.
    """
    return {
        **manifest,
        "status": status,
        "correlation_id": current_correlation_id(),
    }


@router.get("/plugins", operation_id="listPlugins")
def read_plugins(
    request: Request,
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    """Every registered collector with its manifest and lifecycle status."""
    del _
    with request.app.state.session_factory() as session:
        statuses = {
            row.id: row.status for row in list_plugin_rows(session, tenant_id="default")
        }
    plugins = [
        {**manifest, "status": statuses.get(str(manifest["id"]), "enabled")}
        for manifest in plugins_for_surface()
    ]
    return {
        "data": plugins,
        "correlation_id": current_correlation_id(),
    }


@router.get("/plugins/{plugin_id}", operation_id="getPlugin")
def read_plugin(
    request: Request,
    plugin_id: str,
    _: Principal = Depends(read_required),
) -> dict[str, Any]:
    del _
    manifest = next((m for m in plugins_for_surface() if m["id"] == plugin_id), None)
    if manifest is None:
        raise api_error(
            status_code=404,
            code="plugin_not_found",
            message=f"No plugin with id {plugin_id!r}.",
            suggestion="List plugins to see what is registered.",
        )
    with request.app.state.session_factory() as session:
        status = plugin_status(session, plugin_id, tenant_id="default")
    return _plugin_representation(manifest, status)


@router.post("/plugins/{plugin_id}:enable", operation_id="enablePlugin")
def enable_plugin(
    request: Request,
    plugin_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    return _set_status(request, plugin_id, PluginStatus.ENABLED, principal, key)


@router.post("/plugins/{plugin_id}:disable", operation_id="disablePlugin")
def disable_plugin(
    request: Request,
    plugin_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    return _set_status(request, plugin_id, PluginStatus.DISABLED, principal, key)


def _set_status(
    request: Request,
    plugin_id: str,
    status: PluginStatus,
    principal: Principal,
    key: str,
) -> JSONResponse:
    registration = by_name(plugin_id)
    if registration is None:
        raise api_error(
            status_code=404,
            code="plugin_not_found",
            message=f"No plugin with id {plugin_id!r}.",
            suggestion="List plugins to see what is registered.",
        )
    surface = registration.manifest

    # The API-wide idempotency contract: a retried write replays the first
    # result instead of colliding with the 409 below. A key the caller has
    # never used means a deliberate request — whose no-op outcome is the
    # caller's to see as 409, not silently rewritten into a replay.
    endpoint = f"POST /v1/plugins/{plugin_id}:{status.value}"
    factory: sessionmaker[Any] = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    with factory() as session:
        changed = set_plugin_status(
            session,
            plugin_id,
            status=status.value,
            current_manifest_json=json.dumps(
                registration.manifest, sort_keys=True, ensure_ascii=False
            ),
            current_checksum=registration.checksum,
            trust=registration.trust,
            tenant_id="default",
            at=timestamp(_now()),
        )
        session.commit()

    if not changed:
        # A deliberate no-op: the key was fresh (or absent), so there is no
        # earlier result to replay — the caller asked to do what is done.
        raise api_error(
            status_code=409,
            code="plugin_status_unchanged",
            message=f"Plugin {plugin_id!r} is already {status.value}.",
            suggestion="No action needed. Replay the original key to see its result.",
        )

    body = _plugin_representation(surface, status.value)
    body["correlation_id"] = current_correlation_id()
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=body,
    )
    return JSONResponse(status_code=200, content=body)


# --------------------------------------------------------------------------
# Installing and removing plugins (ADR 0023)
# --------------------------------------------------------------------------
#
# These three used to be 409 placeholders. What fills them is a loader that
# treats the operator as the trust decision: it validates a plugin's structure
# and checks that its code provides what its manifest claims, and it refuses
# nothing on the grounds that the plugin might be unsafe. Isolation (the child
# process, the narrowed environment, the proxied egress) bounds the blast
# radius; it is not a claim about intent.


@router.post("/plugins/{plugin_id}:import", operation_id="importPlugin")
def import_plugin(
    request: Request,
    plugin_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Install a plugin from the plugin directory.

    The work, in order: read and validate the manifest, then verify *in a
    child process* that the plugin's code provides what the manifest
    declares. Running the check in a child is what keeps the host from
    executing a plugin's top-level code, and it is also the only way to know
    the plugin is importable before it is recorded as installed.
    """
    endpoint = f"POST /v1/plugins/{plugin_id}:import"
    factory: sessionmaker[Any] = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    from app import collector_registry, plugin_loader

    if plugin_id in {name for name, _, _ in collector_registry._DECLARATIONS}:
        raise api_error(
            status_code=409,
            code="plugin_id_taken",
            message=f"{plugin_id!r} is a collector that ships with Pearss.",
            suggestion="Install the plugin under a different id.",
        )

    try:
        on_disk = plugin_loader.read_manifest(plugin_id)
        impls = plugin_loader.verify_in_child(on_disk)
        plugin_loader.verify_declarations(on_disk, impls)
    except plugin_loader.PluginLoadError as exc:
        # Everything the operator needs to fix it: which plugin, what was
        # wrong, and (for a missing directory) where the loader looked.
        raise api_error(
            status_code=404 if "no plugin directory" in exc.detail else 400,
            code="plugin_load_failed",
            message=exc.detail,
            suggestion=(
                f"Place the plugin under {plugin_loader.plugin_directory(plugin_id)}, "
                "with manifest.json and collector.py, then import again."
            ),
        ) from exc

    manifest = dict(on_disk.manifest)
    manifest["checksum"] = on_disk.checksum

    # Recorded before the registry rebuild, so a crash between the two leaves
    # a row that `list_plugins` shows and a later import can correct, rather
    # than a live plugin the operator cannot see.
    with factory() as session:
        upsert_plugin(
            session,
            plugin_id=plugin_id,
            manifest_json=json.dumps(manifest, sort_keys=True, ensure_ascii=False),
            checksum=on_disk.checksum,
            trust="third-party",
            tenant_id="default",
            at=timestamp(_now()),
        )
        session.commit()

    collector_registry.install_plugin(plugin_id, manifest)

    body = _plugin_representation(manifest, PluginStatus.ENABLED.value)
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=body,
    )
    return JSONResponse(status_code=200, content=body)


@router.delete("/plugins/{plugin_id}", operation_id="uninstallPlugin")
def uninstall_plugin(
    request: Request,
    plugin_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Remove an installed plugin from the registry, leaving its files.

    The files are the operator's, not the host's: deleting them would fuse a
    reversible act (uninstall) with an irreversible one (deleting data).

    Refused while the plugin still serves an active source -- pulling a
    collector out from under its sources would make them fail on the next
    tick with a classification that blamed the origin.
    """
    from app import collector_registry

    endpoint = f"DELETE /v1/plugins/{plugin_id}"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    if plugin_id not in collector_registry.installed_plugin_ids():
        raise api_error(
            status_code=404,
            code="plugin_not_installed",
            message=f"No installed plugin with id {plugin_id!r}.",
            suggestion="List plugins to see what is loaded.",
        )

    with factory() as session:
        # Matched the way routing matches, not by `source.type`: a third-party
        # collector serves `type=web` sources and is selected by its own
        # `match_url`. Querying for `type = <plugin_id>` would find nothing and
        # let an uninstall pull the collector out from under a live source.
        candidates = [
            (str(row[0]), str(row[1]))
            for row in session.execute(
                sa_text("select id, url from source where status = 'active'")
            ).fetchall()
        ]
    in_use = [
        source_id
        for source_id, url in candidates
        if (registration := collector_registry.by_name(plugin_id)) is not None
        and registration.collector.match_url(url)
    ]
    if in_use:
        raise api_error(
            status_code=409,
            code="plugin_in_use",
            message=(
                f"{plugin_id!r} still serves {len(in_use)} active source(s): "
                f"{', '.join(in_use)}."
            ),
            suggestion=(
                "Pause or delete those sources first, then uninstall. The "
                "plugin's files are left in place either way."
            ),
        )

    with factory() as session:
        session.execute(sa_text("delete from plugin where id = :id"), {"id": plugin_id})
        session.commit()

    collector_registry.uninstall_plugin(plugin_id)

    body = {"id": plugin_id, "uninstalled": True, "files": "left in place"}
    body["correlation_id"] = current_correlation_id()
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=body,
    )
    return JSONResponse(status_code=200, content=body)


@router.post("/plugins/{plugin_id}:reload", operation_id="reloadPlugin")
def reload_plugin(
    request: Request,
    plugin_id: str,
    principal: Principal = Depends(write_required),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    """Pick up edited plugin code without restarting the service.

    A child is killed and respawned rather than reloaded in place, which is
    what makes this safe where the on-disk reload of a shipped collector is
    not (ADR 0017): the plugin's state lives in its own process, and the host
    keeps none.
    """
    from app import collector_registry, plugin_loader

    endpoint = f"POST /v1/plugins/{plugin_id}:reload"
    factory = request.app.state.session_factory
    replayed = replay_or_none(factory, actor=principal.name, endpoint=endpoint, key=key)
    if replayed is not None:
        return replayed

    if plugin_id not in collector_registry.installed_plugin_ids():
        raise api_error(
            status_code=404,
            code="plugin_not_installed",
            message=f"No installed plugin with id {plugin_id!r}.",
            suggestion="List plugins to see what is loaded.",
        )

    try:
        on_disk = plugin_loader.read_manifest(plugin_id)
        impls = plugin_loader.verify_in_child(on_disk)
        plugin_loader.verify_declarations(on_disk, impls)
    except plugin_loader.PluginLoadError as exc:
        # The edited plugin is broken. The old registration is left alone
        # rather than half-replaced: a reload that fails should change
        # nothing, so the operator can fix the file and try again.
        raise api_error(
            status_code=400,
            code="plugin_load_failed",
            message=exc.detail,
            suggestion="Fix the plugin, then reload again. Nothing was changed.",
        ) from exc

    manifest = dict(on_disk.manifest)
    manifest["checksum"] = on_disk.checksum
    collector_registry.install_plugin(plugin_id, manifest)

    body = _plugin_representation(manifest, PluginStatus.ENABLED.value)
    body["reloaded"] = True
    body["correlation_id"] = current_correlation_id()
    remember(
        factory,
        actor=principal.name,
        endpoint=endpoint,
        key=key,
        status_code=200,
        body=body,
    )
    return JSONResponse(status_code=200, content=body)


def _now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)
