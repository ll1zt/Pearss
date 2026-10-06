"""Vector retrieval ranks by closeness, and never sees what the gate hides.

Scenario register (normative source: ADR 0035 §9; issue #82).

Real SQLite, the real file store, the real chain and the real visibility
predicate. The vectors are written by hand because what is under test here is
retrieval rather than encoding -- the test against the real model is where the two
meet (`test_embedding_semantics.py`).
"""

from __future__ import annotations

import base64
import json
import os
import struct
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters.db_models import Item, ItemEnrichment
from adapters.filestore import store_body
from adapters.repository import create_source
from adapters.repository.enrichment import chain_for_source
from adapters.repository.vector_search import (
    decode_vector,
    duplicates_of,
    nearest,
    related_items,
)
from app.enrichment_worker import _open_gate_if_complete
from app.pipeline import normalise_and_store
from core.models import ItemDraft
from plugins.processors.embed.encoder import Encoder
from tests import model_weights

pytestmark = pytest.mark.integration

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "plugins"
    / "processors"
    / "embed"
    / "fixtures"
)

# The chain is what keeps an item pending, and the fixture manifest is what the
# real `parse_chain` reads for the identity -- so both the state and the key come
# from the production path rather than from this test's idea of them.
CHAIN = json.dumps([{"plugin_id": "embed", "kind": "embedding"}])


@pytest.fixture(autouse=True)
def _model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the chain at a model directory that resolves.

    The stand-in is the default because it is always present in the checkout; a
    real model already in the environment wins, because the tests that need
    meaning have nothing to measure against a stand-in with random weights. A
    configured directory that cannot be *read* is "no real model here" rather
    than a crash, which is what it used to be (issue #179).
    """
    configured = os.environ.get("PEARSS_EMBEDDING_MODEL_DIR")
    if configured is None or model_weights.readable_weights(Path(configured)) is None:
        monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(FIXTURES))


def _pack(vector: list[float]) -> str:
    """The encoding the processor uses, written out so a change to it fails here."""
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def _basis(axis: int, *, width: int = 384) -> list[float]:
    """A unit vector along one axis: closeness becomes arithmetic, not a guess."""
    vector = [0.0] * width
    vector[axis] = 1.0
    return vector


def _close(first: float, second: float, *, width: int = 384) -> list[float]:
    """A unit vector mostly along axis 0, so its cosine is arithmetic too."""
    vector = [0.0] * width
    vector[0], vector[1] = first, second
    norm = (first * first + second * second) ** 0.5
    return [value / norm for value in vector]


def _source(session: Session, *, name: str, chain: str | None) -> str:
    created = create_source(
        session,
        source_type="rss",
        url=f"https://example.test/{name}",
        canonical_url=f"https://example.test/{name}",
        schedule_policy="{}",
        pipeline_chain=chain,
        tenant_id="default",
        at="2026-01-01T00:00:00.000000Z",
    )
    session.commit()
    return created.id


def _store(session: Session, *, source_id: str, external_id: str, content: str) -> str:
    item = normalise_and_store(
        session,
        [
            ItemDraft(
                origin_id=source_id,
                external_id=external_id,
                title=content[:40],
                canonical_url=f"https://example.test/{external_id}",
                published_at=None,
                content=content,
            )
        ],
        # The chain is a parameter of the store, not a property it looks up: the
        # collector run decides it and passes it, so a test that omits it is
        # testing the P1 path (no chain, promoted straight to ready).
        chain=chain_for_source(session, source_id=source_id),
    )[0].result.item
    session.commit()
    return item.id


def _seed(
    session: Session,
    *,
    source_id: str,
    external_id: str,
    content: str,
    vector: list[float] | None,
    open_gate: bool = True,
) -> str:
    """One item of a chained source, plus the product the worker would have written.

    The row's status and payload are set directly because the worker owns them and
    running one would mean running the model. The *gate* is opened through the
    real function with the real identity, so "ready" here is the state the system
    reaches rather than a flag this test decided on.
    """
    item_id = _store(
        session, source_id=source_id, external_id=external_id, content=content
    )
    if vector is None:
        return item_id

    session.execute(
        sa.update(ItemEnrichment)
        .where(ItemEnrichment.item_id == item_id)
        .values(status="succeeded", payload_ref=store_body(_pack(vector)))
    )
    session.commit()
    if open_gate:
        item = session.get(Item, item_id)
        assert item is not None
        _open_gate_if_complete(
            session,
            item_id=item_id,
            content_hash=item.content_hash,
        )
        session.commit()
    return item_id


@pytest.fixture
def chained_source(session_factory: sessionmaker[Session]) -> str:
    with session_factory() as session:
        return _source(session, name="chained", chain=CHAIN)


@pytest.fixture
def plain_source(session_factory: sessionmaker[Session]) -> str:
    """A source with no chain: its items are visible without any product."""
    with session_factory() as session:
        return _source(session, name="plain", chain=None)


def test_the_closest_vector_comes_first(
    session_factory: sessionmaker[Session], chained_source: str
) -> None:
    """AC-120: the ranking is by closeness, and a distant candidate still comes back."""
    with session_factory() as session:
        near = _seed(
            session,
            source_id=chained_source,
            external_id="near",
            content="the near one",
            vector=_basis(0),
        )
        far = _seed(
            session,
            source_id=chained_source,
            external_id="far",
            content="the far one",
            vector=_basis(1),
        )

        ranked = nearest(session, query=_basis(0), tenant_id="default", limit=10)

    assert [item.id for item, _score in ranked] == [near, far]
    assert [score for _item, score in ranked] == [1.0, 0.0]


def test_limit_keeps_the_best(
    session_factory: sessionmaker[Session], chained_source: str
) -> None:
    with session_factory() as session:
        best = _seed(
            session,
            source_id=chained_source,
            external_id="best",
            content="the best one",
            vector=_close(0.9, 0.1),
        )
        _seed(
            session,
            source_id=chained_source,
            external_id="worse",
            content="the worse one",
            vector=_close(0.1, 0.9),
        )

        ranked = nearest(session, query=_basis(0), tenant_id="default", limit=1)

    assert [item.id for item, _score in ranked] == [best]


def test_a_pending_item_is_not_a_candidate(
    session_factory: sessionmaker[Session], chained_source: str
) -> None:
    """AC-120 (pending): the gate, in the one read path that had to be told about it.

    A search that filtered after fetching would put a pending row in memory where
    a later change could render it -- the shape `visible_item_rows` exists to
    prevent, and the reason this query carries the same predicate as every other
    read. The hidden item's vector is the *closer* of the two, so a result list
    containing it cannot be explained by ranking.
    """
    with session_factory() as session:
        hidden = _seed(
            session,
            source_id=chained_source,
            external_id="hidden",
            content="still waiting on its chain",
            vector=_basis(0),
            open_gate=False,
        )
        visible = _seed(
            session,
            source_id=chained_source,
            external_id="visible",
            content="through the gate",
            vector=_close(0.5, 0.5),
        )
        item = session.get(Item, hidden)
        assert item is not None
        assert item.publish_status == "pending", item.publish_status

        ranked = nearest(session, query=_basis(0), tenant_id="default", limit=10)

    assert [item.id for item, _score in ranked] == [visible]


def test_an_item_without_a_product_is_not_a_candidate(
    session_factory: sessionmaker[Session],
    chained_source: str,
    plain_source: str,
) -> None:
    """Visible is not enough: the row has to have a vector to be comparable."""
    with session_factory() as session:
        unembedded = _store(
            session,
            source_id=plain_source,
            external_id="unembedded",
            content="no product at all",
        )
        embedded = _seed(
            session,
            source_id=chained_source,
            external_id="embedded",
            content="has a product",
            vector=_basis(0),
        )
        item = session.get(Item, unembedded)
        assert item is not None
        assert item.publish_status == "ready", (
            "the item is visible; it just has no vector"
        )

        ranked = nearest(session, query=_basis(0), tenant_id="default", limit=10)

    assert [item.id for item, _score in ranked] == [embedded]


def test_the_item_itself_can_be_excluded(
    session_factory: sessionmaker[Session], chained_source: str
) -> None:
    """AC-120 (self-exclusion): "Articles like this one" must not return the article."""
    with session_factory() as session:
        this_one = _seed(
            session,
            source_id=chained_source,
            external_id="this-one",
            content="the article itself",
            vector=_basis(0),
        )
        neighbour = _seed(
            session,
            source_id=chained_source,
            external_id="neighbour",
            content="a neighbour",
            vector=_close(0.9, 0.1),
        )

        ranked = nearest(
            session,
            query=_basis(0),
            tenant_id="default",
            limit=10,
            exclude_item_id=this_one,
        )

    assert [item.id for item, _score in ranked] == [neighbour]


def test_a_payload_that_is_not_a_vector_is_skipped_not_fatal(
    session_factory: sessionmaker[Session], chained_source: str
) -> None:
    """AC-120 (bad payload): one unreadable product must not take down a search."""
    with session_factory() as session:
        broken = _seed(
            session,
            source_id=chained_source,
            external_id="broken",
            content="whose product is not a vector",
            vector=_basis(0),
        )
        good = _seed(
            session,
            source_id=chained_source,
            external_id="good",
            content="whose product is",
            vector=_close(0.5, 0.5),
        )
        session.execute(
            sa.update(ItemEnrichment)
            .where(ItemEnrichment.item_id == broken)
            .values(payload_ref=store_body("this is not base64 of float32"))
        )
        session.commit()

        ranked = nearest(session, query=_basis(0), tenant_id="default", limit=10)

    assert [item.id for item, _score in ranked] == [good]


def test_a_vector_of_the_wrong_width_is_skipped(
    session_factory: sessionmaker[Session], chained_source: str
) -> None:
    """AC-120 (wrong width): the identity mechanism should make this
    impossible; if it happens, say so."""
    with session_factory() as session:
        _seed(
            session,
            source_id=chained_source,
            external_id="wrong-width",
            content="a vector from another model",
            vector=[1.0, 0.0, 0.0],
        )
        right = _seed(
            session,
            source_id=chained_source,
            external_id="right-width",
            content="a vector of the right width",
            vector=_close(0.5, 0.5),
        )

        ranked = nearest(session, query=_basis(0), tenant_id="default", limit=10)

    assert [item.id for item, _score in ranked] == [right]


def test_the_decoder_reads_what_the_processor_writes() -> None:
    """The encoding has one owner and one reader, and they agree."""
    assert decode_vector(_pack([1.0, 2.5, -3.0])) == [1.0, 2.5, -3.0]
    assert decode_vector("") is None
    assert decode_vector("not base64 !!") is None
    assert decode_vector(base64.b64encode(b"12345").decode()) is None


def _real_model_or_skip() -> Path:
    """The real weights, or a skip that says why.

    The commit-time fixture is a stand-in with random weights: it exercises the
    plumbing and knows nothing about meaning. Only the real model can answer the
    question this test asks, so it says so rather than pretending.
    """
    directory = model_weights.readable_weights()
    if directory is None:
        pytest.skip(
            f"no readable embedding model at {model_weights.configured_path()}; "
            "run `just fetch-model`"
        )
    if model_weights.declared_name(directory) != "multilingual-e5-small":
        declared = model_weights.declared_name(directory)
        pytest.skip(f"{declared!r} is the test stand-in, not real weights")
    return directory


def test_a_semantic_query_recalls_an_item_that_shares_no_words_with_it(
    session_factory: sessionmaker[Session],
    chained_source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-99: what the lexical path cannot reach, and why vectors exist.

    Two items neither of which contains the query's words -- one in English, one
    paraphrasing in Chinese -- must come back before an unrelated one. A lexical
    search returns none of them (AC-105's cascade ceiling is the same failure):
    nothing here matches "操作系统进程调度" literally.
    """
    directory = _real_model_or_skip()
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(directory))
    encoder = Encoder(directory)
    with session_factory() as session:
        chinese = _seed(
            session,
            source_id=chained_source,
            external_id="chinese",
            content="Linux 内核的调度器是怎么工作的",
            vector=encoder.encode("Linux 内核的调度器是怎么工作的"),
        )
        english = _seed(
            session,
            source_id=chained_source,
            external_id="english",
            content="Operating system process scheduling",
            vector=encoder.encode("Operating system process scheduling"),
        )
        unrelated = _seed(
            session,
            source_id=chained_source,
            external_id="unrelated",
            content="红烧肉的家常做法",
            vector=encoder.encode("红烧肉的家常做法"),
        )

        ranked = nearest(
            session,
            query=encoder.encode("操作系统进程调度", as_query=True),
            tenant_id="default",
            limit=10,
        )

    order = [item.id for item, _score in ranked]
    assert set(order[:2]) == {chinese, english}, (
        "a paraphrase and a translation must be recalled by meaning; "
        f"got {order} against {{'chinese': {chinese}, 'english': {english},"
        f" 'unrelated': {unrelated}}}"
    )
    assert order[-1] == unrelated


# --------------------------------------------------------------------------
# AC-108..109: neighbours, and copies that are only ever a hint
#
# The floors are measured (see `vector_search`), so these pin the mechanics with
# synthetic vectors at known cosines; the tests against the real model below pin
# the values themselves.
# --------------------------------------------------------------------------


def test_related_items_are_the_ones_above_the_floor(
    session_factory: sessionmaker[Session], chained_source: str
) -> None:
    """AC-108: a neighbour comes back, an unrelated item does not, and neither
    does the item itself -- a vector search always has a nearest neighbour, so the
    floor is what makes the list mean something."""
    with session_factory() as session:
        this_one = _seed(
            session,
            source_id=chained_source,
            external_id="this-one",
            content="the item asked about",
            vector=_basis(0),
        )
        neighbour = _seed(
            session,
            source_id=chained_source,
            external_id="neighbour",
            content="a neighbour",
            vector=_close(0.9, 0.1),
        )
        _seed(
            session,
            source_id=chained_source,
            external_id="unrelated",
            content="nothing to do with it",
            vector=_basis(1),
        )

        found = related_items(session, item_id=this_one, tenant_id="default", limit=10)

    assert found is not None
    assert [item.id for item, _score in found] == [neighbour]


def test_related_items_of_an_item_without_a_vector_is_none(
    session_factory: sessionmaker[Session], plain_source: str
) -> None:
    """ "No product" and "no neighbours" are different facts, so this says which."""
    with session_factory() as session:
        unembedded = _seed(
            session,
            source_id=plain_source,
            external_id="unembedded",
            content="no vector here",
            vector=None,
        )

        found = related_items(
            session, item_id=unembedded, tenant_id="default", limit=10
        )

    assert found is None


def test_duplicates_are_only_the_ones_above_the_higher_threshold(
    session_factory: sessionmaker[Session], chained_source: str
) -> None:
    """AC-109's first half: a copy is reported, a paraphrase is not.

    0.99 is a copy and 0.98 is a paraphrase, and the threshold sits between them
    at 0.985 -- the measured 0.014-wide gap. The second half is the assertion
    below: nothing was merged, deleted or rewritten to produce the hint.
    """
    with session_factory() as session:
        original = _seed(
            session,
            source_id=chained_source,
            external_id="original",
            content="the original text",
            vector=_basis(0),
        )
        copy = _seed(
            session,
            source_id=chained_source,
            external_id="copy",
            content="the same text, republished",
            vector=_close(0.99, 0.141),
        )
        paraphrase = _seed(
            session,
            source_id=chained_source,
            external_id="paraphrase",
            content="the same meaning, different words",
            vector=_close(0.98, 0.199),
        )

        found = duplicates_of(session, item_id=original, tenant_id="default", limit=10)
        still_there = session.scalars(sa.select(Item.id)).all()

    assert found is not None
    assert [item.id for item, _score in found] == [copy]
    assert paraphrase not in [item.id for item, _score in found]
    assert set(still_there) == {original, copy, paraphrase}, (
        "a hint must not have changed anything: ADR 0021 leaves deletion to the user"
    )


def test_a_copy_is_found_and_a_paraphrase_is_not(
    session_factory: sessionmaker[Session],
    chained_source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-109 with the real model: the measured gap is what the threshold sits in."""
    directory = _real_model_or_skip()
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(directory))
    encoder = Encoder(directory)
    base = (
        "Linux 内核的调度器是怎么工作的:本文从 CFS 的红黑树讲起,"
        "说明 vruntime 如何决定下一个被调度的任务."
    )
    with session_factory() as session:
        original = _seed(
            session,
            source_id=chained_source,
            external_id="original",
            content=base,
            vector=encoder.encode(base),
        )
        copy = _seed(
            session,
            source_id=chained_source,
            external_id="copy",
            content=base.replace("是怎么工作的", "如何工作"),
            vector=encoder.encode(base.replace("是怎么工作的", "如何工作")),
        )
        paraphrase = _seed(
            session,
            source_id=chained_source,
            external_id="paraphrase",
            content=(
                "本文讲解 Linux 内核调度器的工作原理,从 CFS 的红黑树数据结构出发,"
                "解释 vruntime 怎样选出下一个运行的任务."
            ),
            vector=encoder.encode(
                "本文讲解 Linux 内核调度器的工作原理,从 CFS 的红黑树数据结构出发,"
                "解释 vruntime 怎样选出下一个运行的任务."
            ),
        )

        found = duplicates_of(session, item_id=original, tenant_id="default", limit=10)

    assert found is not None
    assert [item.id for item, _score in found] == [copy], (
        f"a light edit is a copy and a paraphrase is not; got "
        f"{[(item.id, round(score, 4)) for item, score in found]} against "
        f"paraphrase {paraphrase}"
    )


def test_a_semantic_neighbour_is_related_and_an_unrelated_item_is_not(
    session_factory: sessionmaker[Session],
    chained_source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-108 with the real model, where "related" is a fact about meaning."""
    directory = _real_model_or_skip()
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(directory))
    encoder = Encoder(directory)
    with session_factory() as session:
        this_one = _seed(
            session,
            source_id=chained_source,
            external_id="scheduler",
            content="Linux 内核的调度器是怎么工作的",
            vector=encoder.encode("Linux 内核的调度器是怎么工作的"),
        )
        neighbour = _seed(
            session,
            source_id=chained_source,
            external_id="scheduler-paraphrase",
            content="内核调度器的负载均衡与 NUMA 亲和性",
            vector=encoder.encode("内核调度器的负载均衡与 NUMA 亲和性"),
        )
        unrelated = _seed(
            session,
            source_id=chained_source,
            external_id="recipe",
            content="红烧肉的家常做法",
            vector=encoder.encode("红烧肉的家常做法"),
        )

        found = related_items(session, item_id=this_one, tenant_id="default", limit=10)

    assert found is not None
    ids = [item.id for item, _score in found]
    assert neighbour in ids, (
        f"a passage on the same topic must be a neighbour; got "
        f"{[(item.id, round(score, 4)) for item, score in found]}"
    )
    assert unrelated not in ids, "0.847 is not related, and the floor says so"
    assert this_one not in ids, "an item is not its own neighbour"
