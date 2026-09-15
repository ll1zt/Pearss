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
from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from adapters.correlation import current as current_correlation_id
from adapters.repository import (
    list_plugin_rows,
    plugin_status,
    set_plugin_status,
    timestamp,
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


# The placeholders declare response_model=None: their only outcome is the
# 409 envelope `_unavailable` raises, and NoReturn is not a pydantic field.
@router.post(
    "/plugins/{plugin_id}:import",
    operation_id="importPlugin",
    status_code=409,
    response_model=None,
)
def import_plugin(plugin_id: str) -> NoReturn:
    _unavailable(plugin_id, "import")


@router.delete(
    "/plugins/{plugin_id}",
    operation_id="uninstallPlugin",
    status_code=409,
    response_model=None,
)
def uninstall_plugin(plugin_id: str) -> NoReturn:
    _unavailable(plugin_id, "uninstall")


@router.post(
    "/plugins/{plugin_id}:reload",
    operation_id="reloadPlugin",
    status_code=409,
    response_model=None,
)
def reload_plugin(plugin_id: str) -> NoReturn:
    _unavailable(plugin_id, "reload")


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


def _now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)
