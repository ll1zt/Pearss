"""Plugin table persistence (ADR 0017)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from adapters.db_models import Plugin as PluginRow


def upsert_plugin(
    session: Session,
    *,
    plugin_id: str,
    manifest_json: str,
    checksum: str,
    trust: str,
    tenant_id: str,
    at: str,
) -> None:
    """Record a collector's verified manifest at startup.

    Status is deliberately not touched here: startup discovers what exists,
    it does not override an operator's enable/disable decision.
    """
    row = session.get(PluginRow, plugin_id)
    if row is None:
        session.add(
            PluginRow(
                id=plugin_id,
                manifest_json=manifest_json,
                checksum=checksum,
                status="enabled",
                trust=trust,
                tenant_id=tenant_id,
                created_at=at,
                updated_at=at,
            )
        )
    else:
        row.manifest_json = manifest_json
        row.checksum = checksum
        row.updated_at = at


def plugin_status(session: Session, plugin_id: str, *, tenant_id: str) -> str:
    """The operator's lifecycle decision for a plugin ('enabled'/'disabled').

    Rows the registry has not recorded yet read as enabled — the P2 default —
    so a fresh database collects until the operator says otherwise.
    """
    row = session.get(PluginRow, plugin_id)
    if row is None or row.tenant_id != tenant_id:
        return "enabled"
    return row.status


def set_plugin_status(
    session: Session,
    plugin_id: str,
    *,
    status: str,
    current_manifest_json: str,
    current_checksum: str,
    trust: str,
    tenant_id: str,
    at: str,
) -> bool:
    """Persist an operator's enable/disable decision. True when changed.

    A missing row (the operator acted before the startup sync, or the sync
    was skipped) is created in the target state rather than rejected: the
    registry holds the authoritative manifest, so there is nothing to lose.
    """
    row = session.get(PluginRow, plugin_id)
    if row is None or row.tenant_id != tenant_id:
        session.add(
            PluginRow(
                id=plugin_id,
                manifest_json=current_manifest_json,
                checksum=current_checksum,
                status=status,
                trust=trust,
                tenant_id=tenant_id,
                created_at=at,
                updated_at=at,
            )
        )
        return True
    if row.status == status:
        return False
    row.status = status
    row.updated_at = at
    return True


def list_plugin_rows(session: Session, *, tenant_id: str) -> list[PluginRow]:
    """All recorded plugins for a tenant, by id."""
    return list(
        session.execute(
            select(PluginRow)
            .where(PluginRow.tenant_id == tenant_id)
            .order_by(PluginRow.id)
        ).scalars()
    )
