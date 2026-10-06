"""Reader output respects the processing gate of a revised body."""

from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from adapters import reader_api
from adapters.db_models import Item as ItemRow
from adapters.repository import create_source
from app.pipeline import normalise_and_store
from core.enrichment import EnrichmentKind, EnrichmentSpec
from core.models import ItemDraft
from tests.e2e.conftest import READER_PASSWORD, READER_USERNAME

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


async def test_user_cannot_fetch_a_revision_before_its_processing_finishes(
    served: str, session_factory: sessionmaker[Session]
) -> None:
    """Given a ready item, revising it behind a required chain hides ids and body."""
    with session_factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://example.test/revisions",
            canonical_url="https://example.test/revisions",
            schedule_policy="{}",
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        draft = ItemDraft(
            origin_id=source.id,
            external_id="revision",
            title="Article",
            canonical_url="https://example.test/article",
            published_at=None,
            content="<p>Original</p>",
        )
        item = normalise_and_store(session, [draft])[0].result.item
    async with httpx.AsyncClient(base_url=served) as client:
        login = await client.post(
            "/api/greader.php/accounts/ClientLogin",
            data={"Email": READER_USERNAME, "Passwd": READER_PASSWORD},
        )
        assert login.status_code == 200
        token = next(
            line[5:] for line in login.text.splitlines() if line.startswith("Auth=")
        )
        headers = {"Authorization": f"GoogleLogin auth={token}"}
        endpoint = "/api/greader.php/reader/api/0/stream/items/ids"
        initial = await client.get(endpoint, headers=headers)
        assert [ref["id"] for ref in initial.json()["itemRefs"]] == [
            str(reader_api.reader_id(item.id))
        ]
        with session_factory() as session:
            normalise_and_store(
                session,
                [replace(draft, content="<p>Revision waiting for summary</p>")],
                chain=(
                    EnrichmentSpec(plugin_id="summary", kind=EnrichmentKind.SUMMARY),
                ),
            )
        hidden = await client.get(endpoint, headers=headers)
        assert hidden.status_code == 200
        assert hidden.json()["itemRefs"] == []
        body = await client.post(
            "/api/greader.php/reader/api/0/stream/items/contents",
            data={"T": token, "i": str(reader_api.reader_id(item.id))},
        )
        assert body.status_code == 200
        assert body.json()["items"] == []
        with session_factory() as session:
            row = session.execute(select(ItemRow.__table__)).one()
            assert row.publish_status == "pending"
            assert row.published_to_feed_at is None
