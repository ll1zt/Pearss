"""Core API: /v1/plugins (P2 lifecycle surface, ADR 0017).

Reads list the verified manifests; the enable/disable write flips the
lifecycle status the scheduler honours on its next tick. Import/uninstall
are explicit 409 placeholders: builtin plugins arrive with Pearss, and the
third-party loader that gives those operations meaning is P2b — saying so
beats pretending the endpoint exists.
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from adapters.correlation import current as current_correlation_id
from adapters.repository import (
    list_plugin_rows,
    plugin_status,
    set_plugin_status,
)
from app.auth import Principal, read_required, write_required
from app.collector_registry import by_name, plugins_for_surface
from app.sources_routes import api_error
from core.enums import PluginStatus

router = APIRouter(prefix="/v1", tags=["plugins"])


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
    plugins = []
    for manifest in plugins_for_surface():
        plugins.append(
            {**manifest, "status": statuses.get(str(manifest["id"]), "enabled")}
        )
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
    return {**manifest, "status": status, "correlation_id": current_correlation_id()}


@router.post("/plugins/{plugin_id}:enable", operation_id="enablePlugin")
def enable_plugin(
    request: Request,
    plugin_id: str,
    principal: Principal = Depends(write_required),
) -> JSONResponse:
    return _set_status(request, plugin_id, PluginStatus.ENABLED, principal)


@router.post("/plugins/{plugin_id}:disable", operation_id="disablePlugin")
def disable_plugin(
    request: Request,
    plugin_id: str,
    principal: Principal = Depends(write_required),
) -> JSONResponse:
    return _set_status(request, plugin_id, PluginStatus.DISABLED, principal)


def _set_status(
    request: Request, plugin_id: str, status: PluginStatus, principal: Principal
) -> JSONResponse:
    registration = by_name(plugin_id)
    if registration is None:
        raise api_error(
            status_code=404,
            code="plugin_not_found",
            message=f"No plugin with id {plugin_id!r}.",
            suggestion="List plugins to see what is registered.",
        )
    manifest = registration.manifest
    with request.app.state.session_factory() as session:
        changed = set_plugin_status(
            session,
            plugin_id,
            status=status.value,
            current_manifest_json=manifest.canonical_json(),
            current_checksum=manifest.checksum,
            trust="builtin",
            tenant_id="default",
            at=_iso_now(),
        )
        session.commit()
    if not changed:
        raise api_error(
            status_code=409,
            code="plugin_status_unchanged",
            message=f"Plugin {plugin_id!r} is already {status.value}.",
            suggestion="No action needed.",
        )
    return JSONResponse(
        status_code=200,
        content={
            "plugin_id": plugin_id,
            "status": status.value,
            "changed_by": principal.name,
            "correlation_id": current_correlation_id(),
        },
    )


@router.post(
    "/plugins/{plugin_id}:import", operation_id="importPlugin", status_code=409
)
def import_plugin(plugin_id: str) -> dict[str, str]:
    _unavailable(plugin_id, "import")
    raise AssertionError("unreachable")  # pragma: no cover


@router.delete("/plugins/{plugin_id}", operation_id="uninstallPlugin", status_code=409)
def uninstall_plugin(plugin_id: str) -> dict[str, str]:
    _unavailable(plugin_id, "uninstall")
    raise AssertionError("unreachable")  # pragma: no cover


@router.post(
    "/plugins/{plugin_id}:reload", operation_id="reloadPlugin", status_code=409
)
def reload_plugin(plugin_id: str) -> dict[str, str]:
    _unavailable(plugin_id, "reload")
    raise AssertionError("unreachable")  # pragma: no cover


def _unavailable(plugin_id: str, operation: str) -> NoReturn:
    """P2b placeholder: honest about what does not exist yet."""
    raise api_error(
        status_code=409,
        code="plugin_operation_unavailable",
        message=(
            f"{operation} is not available: the plugin loader that would give "
            "it meaning ships in P2b. All current plugins are builtin and "
            "arrive with Pearss itself."
        ),
        suggestion="Enable or disable the plugin instead.",
    )


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
