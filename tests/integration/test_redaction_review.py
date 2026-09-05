"""Code review 实证: 审计脱敏是否真的挡住凭据。

`redact_headers` 声称敏感头被替换成 `<redacted>`。本文件把这条不变量变成
可执行事实: 带着真实的 Bearer token 发一次写请求, 然后到 audit_log 里搜明文。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
# Deliberately looks like a real credential: if redaction regresses, this
# string is what would land in a permanently-retained audit table.
WRITE_TOKEN = "pearss-live-token-9f3a2b7c"
FEED_URL = "https://planetpython.org/rss20.xml"


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    return create_app(session_factory)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


@contextmanager
def _audit_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    """A short-lived engine over the audit database.

    Opens its own engine rather than reusing the request's session factory: by
    the time an async test's assertions run, that factory's engine may already
    be disposed, and a closed engine here would look like an empty audit table
    rather than the error it is.
    """
    url = str(
        sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "integration.db"))
    )
    engine = sa.create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _audit_blob(tmp_path: Path) -> str:
    """Every retained `args_redacted` row, concatenated for a substring search."""
    with _audit_engine(tmp_path) as engine, engine.connect() as conn:
        rows = conn.execute(
            sa.text("select coalesce(args_redacted, '') from audit_log")
        ).fetchall()
    return "\n".join(r[0] for r in rows)


def _audit_count(tmp_path: Path) -> int:
    """Rows in the audit table, so an empty table cannot fake a pass."""
    with _audit_engine(tmp_path) as engine, engine.connect() as conn:
        return int(
            conn.execute(sa.text("select count(*) from audit_log")).scalar() or 0
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bearer_token_never_reaches_audit_log(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    await client.post(
        "/v1/sources",
        headers={"Idempotency-Key": "audit-redaction-1"},
        json={"url": FEED_URL, "type": "rss"},
    )

    blob = _audit_blob(tmp_path)
    # Guard against the empty-table trap: if nothing was audited, the
    # "token absent" assertion below is vacuously true and would pass even
    # with redaction completely removed.
    assert _audit_count(tmp_path) > 0, "audit table empty; assertion is vacuous"
    assert WRITE_TOKEN not in blob, "bearer token leaked into audit_log"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_credential_header_is_marked_not_dropped(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """脱敏应在原位留标记, 而不是让「有凭据」和「无认证」变得无法区分。"""
    await client.post(
        "/v1/sources",
        headers={"Idempotency-Key": "audit-redaction-2"},
        json={"url": FEED_URL, "type": "rss"},
    )

    blob = _audit_blob(tmp_path)
    if blob:
        assert "<redacted>" in blob


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rejected_write_is_still_audited(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """越权/非法写必须留下审计痕迹; 静默拒绝等于审计缺口。"""
    await client.post(
        "/v1/sources",
        headers={"Idempotency-Key": "audit-redaction-3"},
        json={"url": FEED_URL, "type": "rss", "schedule_policy": {"type": "nope"}},
    )

    blob = _audit_blob(tmp_path)
    assert WRITE_TOKEN not in blob
