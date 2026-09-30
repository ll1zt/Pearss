"""End-to-end: a Zhihu source through the real collection path.

The API is served from the collector's fixtures over a MockTransport, so the
signed client, the registry's fetch hook, normalisation, dedupe and the store
all run against a temporary database — no network and no real Zhihu session.
The transport rejects unsigned requests, so the test cannot pass by accident.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    url = migrated_database(tmp_path, "zhihu.db")

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


def _zhihu_transport() -> httpx.MockTransport:
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

    return httpx.MockTransport(handler)


@pytest.mark.integration
def test_answers_flow_from_source_url_to_stored_items(
    session_factory: sessionmaker[Session],
) -> None:
    collector_registry._registry()  # force build (spawns children lazily)
    collector_registry.set_http_transport("zhihu", _zhihu_transport())
    with session_factory() as session:
        _add_source(session, "s1", "https://www.zhihu.com/people/example/answers")
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
    assert (result.drafts, result.inserted, result.skipped) == (2, 2, 0)


@pytest.mark.integration
def test_activities_flow_names_the_source_and_opens_the_gate(
    session_factory: sessionmaker[Session],
) -> None:
    collector_registry._registry()  # force build (spawns children lazily)
    collector_registry.set_http_transport("zhihu", _zhihu_transport())
    with session_factory() as session:
        _add_source(session, "s2", "https://www.zhihu.com/people/example/activities")
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
    from app.collector_registry import refresh_registry

    refresh_registry()  # respawn the child so it inherits the emptied env
    collector_registry.set_http_transport("zhihu", _zhihu_transport())
    with session_factory() as session:
        _add_source(session, "s3", "https://www.zhihu.com/people/example/answers")
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
    assert "d_c0" in (result.detail or "")


@pytest.mark.integration
def test_a_transport_failure_stays_network_and_keeps_the_child(
    session_factory: sessionmaker[Session],
) -> None:
    """An unreachable origin is a classified value, not a child fault.

    The egress transport failing must not cost the child its life: the child
    is healthy, the origin (or the network) is not, and a restart would only
    reproduce the failure — after putting the source's run slot in a state
    only the reaper could clear.
    """

    collector_registry._registry()
    registration = collector_registry.by_name("zhihu")
    assert registration is not None
    # `start` is on the sandboxed proxy, not the `Collector` Protocol the
    # registry declares; the tests drive the real child directly.
    cast("Any", registration.collector).start()
    child = collector_registry.default_registry()._sandbox_children["zhihu"]
    assert child._proc is not None
    pid = child._proc.pid
    collector_registry.set_http_transport(
        "zhihu",
        httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))
        ),
    )

    with session_factory() as session:
        _add_source(session, "s4", "https://www.zhihu.com/people/example/answers")
        source = session.get(Source, "s4")
        assert source is not None
        result = collect_source(session, source=source, client=httpx.Client())
        session.commit()

    assert not result.succeeded
    assert result.error_class == "network"
    assert child._proc is not None and child._proc.pid == pid, (
        "a transport failure must not restart a healthy child"
    )


@pytest.mark.integration
def test_a_policy_refusal_keeps_its_blocked_classification(
    session_factory: sessionmaker[Session],
) -> None:
    """The egress transport's SSRF refusal survives the RPC round trip."""
    from adapters.http import FetchError
    from core.enums import ErrorClass

    collector_registry._registry()
    registration = collector_registry.by_name("zhihu")
    assert registration is not None
    # `start` is on the sandboxed proxy, not the `Collector` Protocol the
    # registry declares; the tests drive the real child directly.
    cast("Any", registration.collector).start()
    child = collector_registry.default_registry()._sandbox_children["zhihu"]
    assert child._proc is not None
    pid = child._proc.pid

    def refuse(request: httpx.Request) -> httpx.Response:
        raise FetchError(ErrorClass.BLOCKED, "private-address fetch refused")

    collector_registry.set_http_transport("zhihu", httpx.MockTransport(refuse))

    with session_factory() as session:
        _add_source(session, "s5", "https://www.zhihu.com/people/example/answers")
        source = session.get(Source, "s5")
        assert source is not None
        result = collect_source(session, source=source, client=httpx.Client())
        session.commit()

    assert not result.succeeded
    assert result.error_class == "blocked"
    assert child._proc is not None and child._proc.pid == pid


@pytest.mark.integration
def test_an_unknown_target_type_loses_only_itself(
    session_factory: sessionmaker[Session],
) -> None:
    """A content type the dispatcher cannot render must not take the rest down.

    The unit tests prove the drop is logged. This proves the *consequence* at
    the layer a reader sees: the known activities still reach the database and
    the gate still opens. An unknown target type is the shape Zhihu adds
    without warning, so the failure to guard against is a run that stores
    nothing and reports success.
    """
    fixture = json.loads((FIXTURES / "activities_valid.json").read_text())
    # Splice in a content type the dispatcher does not know, keeping every
    # recorded activity beside it so the payload stays realistic.
    fixture["data"].append(
        {
            "id": 99999,
            "action_text": "发布了想法",
            "verb": "",
            "actor": {"name": "示例用户", "url_token": "example"},
            "target": {"type": "ebook", "id": 7008, "title": "示例电子书"},
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("activities"):
            return httpx.Response(
                200,
                content=json.dumps(fixture).encode(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404, json={})

    collector_registry._registry()
    collector_registry.set_http_transport("zhihu", httpx.MockTransport(handler))
    with session_factory() as session:
        _add_source(session, "s6", "https://www.zhihu.com/people/example/activities")
        source = session.get(Source, "s6")
        assert source is not None
        result = collect_source(
            session,
            source=source,
            client=httpx.Client(),
            tenant_id="default",
        )
        session.commit()

    # The recorded activities still land; only the ebook is missing.
    assert result.succeeded
    assert result.inserted == 3, "the unknown target took its neighbours with it"

    with session_factory() as session:
        stored = session.execute(
            sa.text("select count(*) from item where source_id = 's6'")
        ).scalar()
    assert stored == 3
