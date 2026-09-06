"""End-to-end: a Zhihu source through the real collection path.

The API is served from the collector's fixtures over a MockTransport, so the
signed client, the registry's fetch hook, normalisation, dedupe and the store
all run against a temporary database — no network and no real Zhihu session.
The transport rejects unsigned requests, so the test cannot pass by accident.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Source
from app.pipeline import collect_source
from core.enums import SourceStatus, TrustLevel

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "src/plugins/collectors/zhihu/fixtures"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
POLICY = '{"type":"fixed","interval":"30m"}'

COOKIECLOUD_ENV = (
    "PEARSS_COOKIECLOUD_URL",
    "PEARSS_COOKIECLOUD_UUID",
    "PEARSS_COOKIECLOUD_TOKEN",
)


@pytest.fixture
def session_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "zhihu.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_ZHIHU_COOKIES", "d_c0=testdc0; z_c0=testsession")
    for name in COOKIECLOUD_ENV:
        monkeypatch.delenv(name, raising=False)

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


def _zhihu_client() -> httpx.Client:
    """Serve the fixtures, but only to requests the signed client produces."""
    fixture_by_suffix = {
        "answers": "answers_valid.json",
        "activities": "activities_valid.json",
        "articles": "articles_valid.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-zse-96", "").startswith("2.0_"), (
            "an unsigned request must not be served: the fixture stands in for "
            "Zhihu's own signature check"
        )
        assert "d_c0=testdc0" in request.headers.get("cookie", "")
        for suffix, name in fixture_by_suffix.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(
                    200,
                    content=(FIXTURES / name).read_bytes(),
                    headers={"content-type": "application/json"},
                )
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.integration
def test_answers_flow_from_source_url_to_stored_items(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _add_source(session, "s1", "https://www.zhihu.com/people/example/answers")
        source = session.get(Source, "s1")
        assert source is not None
        result = collect_source(
            session, source=source, client=_zhihu_client(), tenant_id="default"
        )
        session.commit()

    assert result.succeeded
    assert (result.drafts, result.inserted, result.skipped) == (2, 2, 0)


@pytest.mark.integration
def test_activities_flow_names_the_source_and_opens_the_gate(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _add_source(session, "s2", "https://www.zhihu.com/people/example/activities")
        source = session.get(Source, "s2")
        assert source is not None
        result = collect_source(
            session, source=source, client=_zhihu_client(), tenant_id="default"
        )
        session.commit()

    assert result.succeeded
    assert result.inserted == 3, "three content activities; the noise is dropped"

    with session_factory() as session:
        title = session.execute(
            sa.text("select title from source where id = 's2'")
        ).scalar()
        statuses = session.execute(
            sa.text("select publish_status from item where source_id = 's2'")
        ).fetchall()
    assert title == "示例用户的知乎动态"
    assert [row[0] for row in statuses] == ["ready", "ready", "ready"]


@pytest.mark.integration
def test_a_missing_session_is_an_auth_run_not_a_crash(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PEARSS_ZHIHU_COOKIES", raising=False)
    with session_factory() as session:
        _add_source(session, "s3", "https://www.zhihu.com/people/example/answers")
        source = session.get(Source, "s3")
        assert source is not None
        result = collect_source(
            session, source=source, client=_zhihu_client(), tenant_id="default"
        )
        session.commit()

    assert not result.succeeded
    assert result.error_class == "auth"
    assert "d_c0" in (result.detail or "")
