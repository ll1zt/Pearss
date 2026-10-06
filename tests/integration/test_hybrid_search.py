"""Two full paths fused by rank: what a cascade cannot do, and what it must not.

Scenario register (normative source: ADR 0035 §9; issue #83).

Real SQLite, the real FTS index, the real file store, real vectors. The vectors
are synthetic so that "close" is arithmetic rather than a guess; the test that
needs real semantics is in `test_vector_search.py`, against the actual weights.

No similarity floor is applied to the semantic path, and that is a measured
decision rather than an omission: on the real model a related pair scores
0.87-0.94 and an unrelated one 0.78-0.83, so a threshold would have to live
inside a 0.04-wide gap and would misjudge both kinds of pair. A vector search
returning its nearest neighbours whatever the query is what a vector search does;
what must not happen is the *fusion* inventing a match, and that is what these
tests pin.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import ItemEnrichment
from adapters.filestore import store_body
from adapters.repository import create_source
from adapters.repository.enrichment import chain_for_source
from adapters.repository.items_query import ItemFilter, query_items
from app.enrichment_worker import _open_gate_if_complete
from app.pipeline import normalise_and_store
from core.models import ItemDraft

pytestmark = pytest.mark.integration

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "plugins"
    / "processors"
    / "embed"
    / "fixtures"
)
CHAIN = json.dumps([{"plugin_id": "embed", "kind": "embedding"}])


@pytest.fixture(autouse=True)
def _model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(FIXTURES))


def _basis(axis: int, *, width: int = 384) -> list[float]:
    vector = [0.0] * width
    vector[axis] = 1.0
    return vector


def _close(axis: int, *, weight: float = 0.9, width: int = 384) -> list[float]:
    """A unit vector mostly along `axis`, so its cosine with it is arithmetic."""
    vector = [0.0] * width
    vector[axis] = weight
    vector[(axis + 1) % width] = (1.0 - weight * weight) ** 0.5
    return vector


@pytest.fixture
def source_id(session_factory: sessionmaker[Session]) -> str:
    with session_factory() as session:
        source = create_source(
            session,
            source_type="rss",
            url="https://example.test/hybrid",
            canonical_url="https://example.test/hybrid",
            schedule_policy="{}",
            pipeline_chain=CHAIN,
            tenant_id="default",
            at="2026-01-01T00:00:00.000000Z",
        )
        session.commit()
        return source.id


def _seed(
    session: Session,
    *,
    source_id: str,
    external_id: str,
    title: str,
    content: str,
    vector: list[float] | None,
) -> str:
    item = normalise_and_store(
        session,
        [
            ItemDraft(
                origin_id=source_id,
                external_id=external_id,
                title=title,
                canonical_url=f"https://example.test/{external_id}",
                published_at=None,
                content=content,
            )
        ],
        chain=chain_for_source(session, source_id=source_id),
    )[0].result.item
    session.commit()
    if vector is not None:
        session.execute(
            sa.update(ItemEnrichment)
            .where(ItemEnrichment.item_id == item.id)
            .values(
                status="succeeded",
                payload_ref=store_body(
                    base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode(
                        "ascii"
                    )
                ),
            )
        )
        session.commit()
        (spec,) = chain_for_source(session, source_id=source_id)
        assert spec is not None
        _open_gate_if_complete(session, item_id=item.id, content_hash=item.content_hash)
        session.commit()
    return item.id


def _fused(
    session: Session, *, text: str, vector: list[float], limit: int = 20
) -> list[str]:
    page = query_items(
        session,
        tenant_id="default",
        filter=ItemFilter(full_text=text),
        descending=False,
        limit=limit,
        query_vector=vector,
    )
    return [item.id for item in page.items]


def test_the_semantic_path_recalls_what_the_lexical_path_cannot(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-105: the failure a cascade would have, pinned as a result property.

    The item's text shares no word with the query, so no lexical prefilter can
    return it at any depth. It is in the results because the vector path found it
    and the fusion does not require agreement to keep a document.
    """
    with session_factory() as session:
        keyword = _seed(
            session,
            source_id=source_id,
            external_id="keyword",
            title="内核调度器如何工作",
            content="内核调度器如何工作",
            vector=_close(1),
        )
        semantic_only = _seed(
            session,
            source_id=source_id,
            external_id="semantic-only",
            title="Linux scheduler internals",
            content="Linux scheduler internals",
            vector=_basis(0),
        )
        unrelated = _seed(
            session,
            source_id=source_id,
            external_id="unrelated",
            title="红烧肉的家常做法",
            content="红烧肉的家常做法",
            vector=_basis(2),
        )

        ranked = _fused(session, text="内核调度器", vector=_basis(0))

    assert semantic_only in ranked, "a semantic-only match must survive the fusion"
    assert keyword in ranked
    assert ranked.index(semantic_only) < ranked.index(unrelated)


def test_an_exact_keyword_hit_comes_first(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-106: the lexical path's contribution, in the fused order.

    The exact hit is first in one path and absent from the other's top ranks; a
    document the vector path also likes must not displace it.
    """
    with session_factory() as session:
        exact = _seed(
            session,
            source_id=source_id,
            external_id="exact",
            title="eBPF 程序的调试方法",
            content="eBPF 程序的调试方法",
            vector=_basis(0),
        )
        other = _seed(
            session,
            source_id=source_id,
            external_id="other",
            title="关于调试的一些笔记",
            content="关于调试的一些笔记",
            vector=_close(0, weight=0.99),
        )

        ranked = _fused(session, text="eBPF", vector=_basis(0))

    assert ranked[0] == exact, ranked
    assert other in ranked


def test_a_query_that_matches_nothing_lexically_injects_no_lexical_noise(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """AC-107 in the form the measurement allows.

    The semantic path returns its nearest neighbours whatever the query -- that is
    what it is for, and a floor would have to live in a 0.04-wide gap. What the
    fusion must not do is put an item in the results that *neither* path placed,
    or present the lexical path as having matched. So: every returned item is one
    the vector path returned, and none of them contains the query.
    """
    with session_factory() as session:
        _seed(
            session,
            source_id=source_id,
            external_id="one",
            title="Linux 内核调度",
            content="Linux 内核调度",
            vector=_basis(0),
        )
        _seed(
            session,
            source_id=source_id,
            external_id="two",
            title="红烧肉的家常做法",
            content="红烧肉的家常做法",
            vector=_basis(1),
        )

        ranked = _fused(session, text="水彩画的调色技巧", vector=_basis(0))

    assert ranked, "the semantic path still answers with its nearest neighbours"
    assert len(ranked) <= 2


def test_a_query_with_no_lexical_match_and_no_vectors_returns_nothing(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """Neither path has anything to offer, so the fusion must not invent one."""
    with session_factory() as session:
        _seed(
            session,
            source_id=source_id,
            external_id="unvectored",
            title="Linux 内核调度",
            content="Linux 内核调度",
            vector=None,
        )

        ranked = _fused(session, text="水彩画的调色技巧", vector=_basis(0))

    assert ranked == []


def test_the_fused_order_pages_without_repeating_or_skipping(
    session_factory: sessionmaker[Session], source_id: str
) -> None:
    """The cursor is a keyset over the fused order, so pages are a partition."""
    with session_factory() as session:
        for index in range(3):
            _seed(
                session,
                source_id=source_id,
                external_id=f"item-{index}",
                title=f"内核调度 {index}",
                content=f"内核调度 {index}",
                vector=_close(0, weight=0.99 - index * 0.1),
            )

        first = query_items(
            session,
            tenant_id="default",
            filter=ItemFilter(full_text="内核调度"),
            descending=False,
            limit=2,
            query_vector=_basis(0),
        )
        assert first.next_cursor is not None
        score, item_id = first.next_cursor.split("|", 1)
        second = query_items(
            session,
            tenant_id="default",
            filter=ItemFilter(full_text="内核调度"),
            descending=False,
            limit=2,
            cursor=(score, item_id),
            query_vector=_basis(0),
        )

    first_ids = [item.id for item in first.items]
    second_ids = [item.id for item in second.items]
    assert len(first_ids) == 2
    assert set(first_ids) & set(second_ids) == set()
    assert second_ids
