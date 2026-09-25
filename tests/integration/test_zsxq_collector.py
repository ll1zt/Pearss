"""End-to-end: a zsxq source through the real collection path.

The API is served from the collector's fixtures over a MockTransport, so the
credential policy, the envelope unwrap, the group+topics merge, normalisation
and the store all run against a temporary database — no network and no real
zsxq session. The transport rejects tokenless requests, so the test cannot
pass by accident.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Source
from app import collector_registry
from app.pipeline import collect_source
from core.enums import SourceStatus, TrustLevel
from tests.migrated_schema import migrated_database

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "src/plugins/collectors/zsxq/fixtures"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
POLICY = '{"type":"fixed","interval":"30m"}'
GROUP_URL = "https://wx.zsxq.com/dweb2/index/group/88855458825252"


@pytest.fixture
def session_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "zsxq.db")

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_ZSXQ_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("PEARSS_ZSXQ_COOKIE", raising=False)

    engine = create_engine(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _add_source(session: Session, source_id: str, url: str) -> None:
    session.add(
        Source(
            id=source_id,
            tenant_id="default",
            type="web",
            url=url,
            canonical_url=url,
            status=SourceStatus.ACTIVE.value,
            trust_level=TrustLevel.STANDARD.value,
            schedule_policy=POLICY,
            created_at="2026-01-01T00:00:00.000000Z",
            updated_at="2026-01-01T00:00:00.000000Z",
        )
    )
    session.commit()


def _zsxq_transport() -> httpx.MockTransport:
    """Serve the fixtures, but only to requests carrying the session token."""

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        assert "zsxq_access_token=test-token" in cookie, (
            "a tokenless request must not be served: the fixture stands in for "
            "zsxq's own session check"
        )
        if request.url.path.endswith("/topics"):
            name = "topics_valid.json"
        elif "/groups/" in request.url.path:
            name = "group.json"
        else:
            return httpx.Response(404, json={})
        return httpx.Response(
            200,
            content=(FIXTURES / name).read_bytes(),
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler)


@pytest.mark.integration
def test_topics_flow_from_source_url_to_stored_items(
    session_factory: sessionmaker[Session],
) -> None:
    collector_registry._registry()  # build children (lazy before first use)
    collector_registry.set_http_transport("zsxq", _zsxq_transport())
    with session_factory() as session:
        _add_source(session, "s1", GROUP_URL)
        source = session.get(Source, "s1")
        assert source is not None
        result = collect_source(
            session,
            source=source,
            client=httpx.Client(),  # egress via injected transport
            tenant_id="default",
        )
        session.commit()

    assert result.succeeded
    assert (result.drafts, result.inserted, result.skipped) == (6, 6, 0)


@pytest.mark.integration
def test_the_feed_name_comes_from_the_group(
    session_factory: sessionmaker[Session],
) -> None:
    collector_registry._registry()  # build children (lazy before first use)
    collector_registry.set_http_transport("zsxq", _zsxq_transport())
    with session_factory() as session:
        _add_source(session, "s2", GROUP_URL)
        source = session.get(Source, "s2")
        assert source is not None
        result = collect_source(
            session,
            source=source,
            client=httpx.Client(),  # egress via injected transport
            tenant_id="default",
        )
        session.commit()

    assert result.succeeded
    with session_factory() as session:
        title = session.execute(
            sa.text("select title from source where id = 's2'")
        ).scalar()
    assert title == "知识星球 - 示例星球"


@pytest.mark.integration
def test_a_missing_token_is_an_auth_run_not_a_crash(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.collector_registry import refresh_registry

    monkeypatch.delenv("PEARSS_ZSXQ_ACCESS_TOKEN", raising=False)
    refresh_registry()  # respawn the child so it inherits the emptied env
    with session_factory() as session:
        _add_source(session, "s3", GROUP_URL)
        source = session.get(Source, "s3")
        assert source is not None
        result = collect_source(
            session,
            source=source,
            client=httpx.Client(),  # egress via injected transport
            tenant_id="default",
        )
        session.commit()

    assert not result.succeeded
    assert result.error_class == "auth"
    assert "zsxq_access_token" in (result.detail or "")
