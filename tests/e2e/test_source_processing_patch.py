"""User changes processing configuration without resetting omitted fields (A04)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item, ItemEnrichment, Subscription
from tests.e2e.conftest import API_TOKEN

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

CHAIN = [{"plugin_id": "translate", "kind": "translation", "required": True}]
EXPLICIT_CHAIN = [
    {"plugin_id": "translate", "kind": "translation", "lang": "fr", "required": True}
]


@pytest_asyncio.fixture
async def writer(served: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=served,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=30,
    ) as client:
        yield client


async def _create_source(writer: httpx.AsyncClient, origin_url: str) -> str:
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": "create-processing-source"},
        json={
            "url": origin_url,
            "schedule_policy": {"type": "fixed", "interval": "30m"},
            "pipeline_chain": CHAIN,
            "target_lang": "en",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.parametrize(
    ("patch", "expected_chain", "expected_lang", "product_lang"),
    [
        pytest.param({"target_lang": "de"}, CHAIN, "de", "de", id="language-only"),
        pytest.param(
            {"pipeline_chain": EXPLICIT_CHAIN},
            EXPLICIT_CHAIN,
            "en",
            "fr",
            id="chain-only",
        ),
        pytest.param(
            {"pipeline_chain": None}, None, "en", None, id="explicit-null-chain"
        ),
        pytest.param({"pipeline_chain": []}, [], "en", None, id="empty-chain"),
        pytest.param(
            {"target_lang": None}, CHAIN, None, None, id="explicit-null-language"
        ),
        pytest.param(
            {"pipeline_chain": None, "target_lang": None},
            None,
            None,
            None,
            id="explicit-clear-both",
        ),
        pytest.param(
            {"pipeline_chain": EXPLICIT_CHAIN, "target_lang": "de"},
            EXPLICIT_CHAIN,
            "de",
            "fr",
            id="replace-both",
        ),
    ],
)
async def test_user_patches_only_intended_processing_fields_and_collection_obeys_gate(
    writer: httpx.AsyncClient,
    origin_url: str,
    session_factory: sessionmaker[Session],
    patch: dict[str, object],
    expected_chain: list[dict[str, object]] | None,
    expected_lang: str | None,
    product_lang: str | None,
) -> None:
    """Given a configured source, when user PATCHes, then only sent fields change.

    Newly collected items still wait for the retained chain, or become ready
    when the user explicitly clears it. No provider or background worker runs.
    """
    source_id = await _create_source(writer, origin_url)
    with session_factory() as session:
        original_chain = session.execute(
            select(Subscription.pipeline_chain).where(Subscription.id == source_id)
        ).scalar_one()

    response = await writer.patch(
        f"/v1/sources/{source_id}",
        headers={"Idempotency-Key": "change-processing"},
        json=patch,
    )
    assert response.status_code == 200, response.text
    assert response.json()["pipeline_chain"] == expected_chain
    assert response.json()["target_lang"] == expected_lang

    stored = await writer.get(f"/v1/sources/{source_id}")
    assert stored.status_code == 200
    assert stored.json()["pipeline_chain"] == expected_chain
    assert stored.json()["target_lang"] == expected_lang
    with session_factory() as session:
        row = session.execute(
            select(Subscription).where(Subscription.id == source_id)
        ).scalar_one()
        assert row.target_lang == expected_lang
        assert (json.loads(row.pipeline_chain) if row.pipeline_chain else None) == (
            expected_chain
        )
        if "pipeline_chain" not in patch:
            assert row.pipeline_chain == original_chain

    collected = await writer.post(
        f"/v1/sources/{source_id}:trigger",
        headers={"Idempotency-Key": "collect-after-processing-change"},
    )
    assert collected.status_code == 200, collected.text
    assert collected.json()["inserted"] == 3
    with session_factory() as session:
        items = session.scalars(select(Item).where(Item.origin_id == source_id)).all()
        assert len(items) == 3
        assert {item.publish_status for item in items} == (
            {"pending"} if expected_chain else {"ready"}
        )
        products = session.scalars(
            select(ItemEnrichment).where(
                ItemEnrichment.item_id.in_([item.id for item in items])
            )
        ).all()
        assert len(products) == (3 if expected_chain else 0)
        assert {(p.plugin_id, p.kind, p.lang, p.status) for p in products} == (
            {("translate", "translation", product_lang, "queued")}
            if expected_chain
            else set()
        )


@pytest.mark.parametrize(
    "bad_chain",
    [
        [{"plugin_id": "translate", "kind": "invalid-kind"}],
        [{"plugin_id": "unregistered", "kind": "translation"}],
    ],
    ids=["invalid-kind", "unknown-processor"],
)
async def test_user_invalid_processing_change_leaves_entire_source_unchanged(
    writer: httpx.AsyncClient,
    origin_url: str,
    session_factory: sessionmaker[Session],
    bad_chain: list[dict[str, str]],
) -> None:
    """Given a source, when user sends an invalid pair, then no fields persist."""
    source_id = await _create_source(writer, origin_url)
    before = (await writer.get(f"/v1/sources/{source_id}")).json()
    response = await writer.patch(
        f"/v1/sources/{source_id}",
        headers={"Idempotency-Key": "invalid-processing-change"},
        json={
            "pipeline_chain": bad_chain,
            "target_lang": "de",
            "folder": "must-not-persist",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"]["code"] == "invalid_pipeline_chain"
    after = (await writer.get(f"/v1/sources/{source_id}")).json()
    before.pop("correlation_id")
    after.pop("correlation_id")
    assert after == before
    with session_factory() as session:
        row = session.execute(
            select(Subscription).where(Subscription.id == source_id)
        ).scalar_one()
        assert row.pipeline_chain is not None
        assert json.loads(row.pipeline_chain) == CHAIN
        assert row.target_lang == "en"
        assert row.folder == "default"
        assert row.updated_at == before["updated_at"]
