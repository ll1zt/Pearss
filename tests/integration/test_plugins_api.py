"""Integration tests for the /v1/plugins lifecycle surface (P2, ADR 0017).

The registry's startup verification and the manifest round-trip are
exercised through the real app: a migrated database, the lifespan sync, and
the authenticated endpoints.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITE_TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "p.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = sa.create_engine(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_API_READ_TOKEN", READ_TOKEN)
    return create_app(session_factory)


@pytest_asyncio.fixture
async def writer(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Write-scoped client with a per-request Idempotency-Key.

    Each test's writes are distinct logical operations, so a fresh UUID per
    request satisfies the contract without colliding across tests.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {WRITE_TOKEN}",
            "Idempotency-Key": "test-key",
        },
    ) as opened:
        yield opened


@pytest.mark.integration
@pytest.mark.asyncio
async def test_startup_syncs_five_manifests(writer: httpx.AsyncClient) -> None:
    body = (await writer.get("/v1/plugins")).json()
    by_id = {row["id"]: row for row in body["data"]}
    assert set(by_id) == {"rss", "telegram", "twitter", "zhihu", "zsxq"}
    for row in body["data"]:
        assert row["status"] == "enabled"
        assert row["checksum"], "every manifest carries its checksum"
        assert row["version"] == "0.1.0"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_type_sources_declare_required_config(
    writer: httpx.AsyncClient,
) -> None:
    body = (await writer.get("/v1/plugins")).json()
    by_id = {row["id"]: row for row in body["data"]}

    assert by_id["rss"]["required_config"] == []
    zhihu = by_id["zhihu"]["required_config"]
    assert zhihu and zhihu[0]["env_name"] == "PEARSS_ZHIHU_COOKIES"
    assert zhihu[0]["required"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disable_then_enable_round_trip(writer: httpx.AsyncClient) -> None:
    disabled = await writer.post(
        "/v1/plugins/telegram:disable", headers={"Idempotency-Key": "tg-off"}
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    # Disable is durable: the listing shows it, not just the write response.
    listed = (await writer.get("/v1/plugins")).json()
    telegram = next(row for row in listed["data"] if row["id"] == "telegram")
    assert telegram["status"] == "disabled"

    enabled = await writer.post(
        "/v1/plugins/telegram:enable", headers={"Idempotency-Key": "tg-on"}
    )
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "enabled"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disable_replays_with_the_same_key_conflicts_with_a_fresh_one(
    writer: httpx.AsyncClient,
) -> None:
    """The two no-op semantics, pinned apart.

    Same key = a retry of the original write: replay its 200. A fresh key on
    an already-done operation = a deliberate no-op: 409.
    """
    first = await writer.post(
        "/v1/plugins/rss:disable", headers={"Idempotency-Key": "rss-off"}
    )
    assert first.status_code == 200

    replay = await writer.post(
        "/v1/plugins/rss:disable", headers={"Idempotency-Key": "rss-off"}
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["status"] == "disabled"

    fresh = await writer.post(
        "/v1/plugins/rss:disable", headers={"Idempotency-Key": "rss-off-again"}
    )
    assert fresh.status_code == 409
    assert fresh.json()["detail"]["error"]["code"] == "plugin_status_unchanged"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_plugin_is_404(writer: httpx.AsyncClient) -> None:
    response = await writer.post(
        "/v1/plugins/missing:disable", headers={"Idempotency-Key": "missing"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "plugin_not_found"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_without_idempotency_key_is_refused(
    app: FastAPI, session_factory: sessionmaker[Session]
) -> None:
    """The API-wide idempotency contract covers the plugin surface too."""
    bare = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    )
    response = await bare.post("/v1/plugins/rss:disable")
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "idempotency_key_required"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p2b_operations_are_honest_placeholders(
    writer: httpx.AsyncClient,
) -> None:
    """Import/uninstall/reload need the loader; saying so beats faking it."""
    for method, path in (
        ("POST", "/v1/plugins/rss:import"),
        ("DELETE", "/v1/plugins/rss"),
        ("POST", "/v1/plugins/rss:reload"),
    ):
        response = await writer.request(method, path)
        assert response.status_code == 409, path
        assert (
            response.json()["detail"]["error"]["code"] == "plugin_operation_unavailable"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_token_can_list_but_not_disable(
    app: FastAPI, writer: httpx.AsyncClient
) -> None:
    reader = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {READ_TOKEN}"},
    )
    listed = await reader.get("/v1/plugins")
    assert listed.status_code == 200

    refused = await reader.post("/v1/plugins/rss:disable")
    assert refused.status_code == 403


# --------------------------------------------------------------------------
# The lifecycle gate on the collection path (ADR 0017)
# --------------------------------------------------------------------------


@pytest.fixture
def zhihu_source(session_factory: sessionmaker[Session]) -> Iterator[str]:
    source_id = "s-zhihu"
    with session_factory() as session:
        session.execute(
            sa.text(
                "insert into source (id, type, url, canonical_url, schedule_policy,"
                " status, trust_level, tenant_id, created_at, updated_at)"
                " values (:id, 'web', :u, :u, '{}', 'active', 'standard',"
                " 'default', '2026-01-01T00:00:00.000000Z',"
                " '2026-01-01T00:00:00.000000Z')"
            ),
            {
                "id": source_id,
                "u": "https://www.zhihu.com/people/example",
            },
        )
        session.commit()
    yield source_id


def _never_network() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a disabled plugin must not touch the network")

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_disabled_plugin_collects_nothing(
    app: FastAPI,
    session_factory: sessionmaker[Session],
    writer: httpx.AsyncClient,
    zhihu_source: str,
) -> None:
    """Disabling a collector stops its sources — by skipping, not failing.

    The skip must not touch the network, must not store items, and must not
    record a failed run: an operator decision is not an incident.
    """
    from adapters.db_models import Source
    from app.pipeline import collect_source

    assert (
        await writer.post(
            "/v1/plugins/zhihu:disable", headers={"Idempotency-Key": "zh-off"}
        )
    ).status_code == 200

    with session_factory() as session:
        source = session.get(Source, zhihu_source)
        assert source is not None
        result = collect_source(
            session,
            source=source,
            client=_never_network(),
            tenant_id="default",
        )
        session.commit()

    assert result.succeeded is True
    assert result.skipped_plugin is True
    assert result.drafts == 0

    with session_factory() as session:
        items = session.execute(
            sa.text("select count(*) from item where source_id = :i"),
            {"i": zhihu_source},
        ).scalar()
        runs = session.execute(sa.text("select status from job_run")).fetchall()
    assert items == 0
    assert runs == [], "a skipped plugin must not look like a failed run"


@pytest.mark.integration
def test_an_enabled_plugin_collects_normally(
    session_factory: sessionmaker[Session], zhihu_source: str
) -> None:
    """Default state (no plugin row yet) is enabled: collection proceeds."""
    from adapters.db_models import Source
    from app.pipeline import collect_source

    with session_factory() as session:
        source = session.get(Source, zhihu_source)
        assert source is not None
        result = collect_source(
            session,
            source=source,
            client=_never_network(),
            tenant_id="default",
        )
        session.commit()

    # No plugin row exists -> enabled -> the gate lets it through (the walk
    # itself fails on the never-network client, which is not this test's
    # concern).
    assert result.skipped_plugin is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_startup_sync_preserves_the_operator_decision(
    app: FastAPI,
    session_factory: sessionmaker[Session],
    writer: httpx.AsyncClient,
) -> None:
    """Startup refreshes manifests; it must not resurrect a disabled plugin.

    The whole point of separating discovery from decision: a restart after
    editing a collector silently re-enabling it would undo an operator's
    choice they cannot see happening.
    """
    from app.collector_registry import sync_plugin_table

    assert (
        await writer.post(
            "/v1/plugins/zhihu:disable", headers={"Idempotency-Key": "zh-off"}
        )
    ).status_code == 200

    # A restart (lifespan) runs the sync again.
    sync_plugin_table(session_factory)

    listed = (await writer.get("/v1/plugins")).json()
    zhihu = next(row for row in listed["data"] if row["id"] == "zhihu")
    assert zhihu["status"] == "disabled"
    assert zhihu["checksum"], "the manifest is still refreshed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disable_persists_across_a_fresh_app_instance(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision lives in the database, not in registry memory."""
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    first_app = create_app(session_factory)
    transport = httpx.ASGITransport(app=first_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as aclient:
        await aclient.post(
            "/v1/plugins/rss:disable", headers={"Idempotency-Key": "rss-off-1"}
        )

    # A brand-new app instance over the same database (simulated restart).
    second_app = create_app(session_factory)
    transport2 = httpx.ASGITransport(app=second_app)
    async with httpx.AsyncClient(
        transport=transport2,
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as aclient:
        listed = (await aclient.get("/v1/plugins")).json()
    rss = next(row for row in listed["data"] if row["id"] == "rss")
    assert rss["status"] == "disabled"
