"""The processor chain's internal dataflow: what a later spec consumes.

Scenario register (chain-level; normative source ADR 0030). These are not
plugin scenarios, so they have no entry in a plugin's SCENARIOS.md:

  AC-14 链后序处理器消费前序产物
    Given 源的处理链为 [fulltext, translate], 二者都 required
    When 两者执行完成
    Then translate 的输入是 fulltext 的产物(全文), 不是条目原始摘要
    And 该行记录它消费的是哪个产物(上游插件 + 产物摘要)

  AC-15 单独 translate 的行为不变(向后兼容)
    Given 源的处理链只有 [translate]
    When 执行
    Then translate 的输入是条目原始正文, 且不记录任何前序产物依赖

  AC-16 前序产物内容变化 -> 后序消费新版本, 不消费旧产物
    Given 条目正文修订(此前入库、仍在闸门后), 链为 [fulltext, translate]
    When 新一轮 sweep 执行
    Then translate 的输入是*新* fulltext 产物, 其记录的依赖摘要随之改变

  AC-17 前序产物失败或尚未完成 -> 后序不被执行, 也不静默降级用原始摘要
    Given 链 [fulltext, translate], fulltext 失败(或尚无产物)
    When sweep 执行
    Then translate 行未被执行、未消费任何输入, 条目保持 pending

  AC-18 产物与输出层次对应
    Given 链 [fulltext, translate] 完成
    When 产物落库
    Then 译文对应全文而非摘要, 闸门打开

The whole point is one sentence: a chain is a *pipeline*, so `[fulltext,
translate]` must translate the full text. Before this, every spec read
`item.content_ref` -- the feed's own summary -- so the translation was of a
50-character stub while the full text sat beside it, and the reader saw three
layers that did not match (#34).

Two construction rules, both learned the hard way here:

1. **Processors are module-level classes with no dependence on parent-process
   state.** They run in a *child* process, so a value the fixture set in a
   module global does not exist there (a first draft of this file recorded its
   inputs in a fixture-owned object and every test went red with "processor
   execution failed" -- a broken harness, not a broken product).
2. **The evidence is the stored artifact and the row**, not an in-memory call
   log: what the downstream processor received is proven by the product it
   wrote, which is the same thing the reader would be served.

Everything here is a real migrated SQLite database, the real pipeline, the
real filestore and the real sweep. No network.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

TENANT = "default"

SUMMARY = "<p>Short summary only.</p>"
REVISED_SUMMARY = "<p>Short summary only, revised.</p>"
FULLTEXT_PRODUCT = (
    "<h2>Full article heading</h2><p>" + ("Article body prose. " * 20) + "</p>"
)
FULLTEXT_PRODUCT_V2 = (
    "<h2>Revised heading</h2><p>" + ("Revised body prose. " * 20) + "</p>"
)
SUMMARY_PRODUCT = "<h2>A summary product</h2><p>Condensed.</p>"

# The identity of a product is sha256 over its bytes, which is also the digest
# component of the `cas://` reference filestore returns -- so a test can state
# the expected digest without guessing at the store's internals.
DIGEST_V1 = hashlib.sha256(FULLTEXT_PRODUCT.encode()).hexdigest()
DIGEST_V2 = hashlib.sha256(FULLTEXT_PRODUCT_V2.encode()).hexdigest()


# --------------------------------------------------------------------------
# The processors. One deterministic class per behaviour, so a test chooses the
# implementation to run exactly as it would choose a provider's response.
# --------------------------------------------------------------------------


class FulltextV1:
    """A fulltext-shaped producer serving the first revision."""

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        return str(store(FULLTEXT_PRODUCT))


class FulltextV2:
    """The same producer after the article was revised upstream."""

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        return str(store(FULLTEXT_PRODUCT_V2))


class EchoTranslate:
    """A translate-shaped consumer whose product *is* its input, wrapped.

    Echoing rather than translating is deliberate: the artifact then proves
    which text the downstream spec was handed, and the assertion is on the
    product the reader would be served rather than on a call log.
    """

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        body = " ".join(item_ctx.get("content", "").split())
        return str(store(f"<p>TRANSLATED[{body}]</p>"))


class EchoSummary:
    """A non-first-position consumer, to pin position-based selection."""

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        return str(store(SUMMARY_PRODUCT))


class FailingFulltext:
    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        from core.enums import ErrorClass
        from core.plugins.hooks import ProcessorError

        raise ProcessorError(ErrorClass.PARSE, "no extractable article")


class RefusingTranslate:
    """Fails loudly if the chain ran it despite a missing upstream."""

    @staticmethod
    def process(item_ctx: Any, *, target_lang: str, store: Any) -> str:
        raise AssertionError(
            "translate must not have run: its upstream product did not succeed"
        )


def _chain(*specs: tuple[str, str]) -> str:
    return json.dumps(
        [
            {"plugin_id": plugin, "kind": kind, "required": True}
            for plugin, kind in specs
        ]
    )


FULLTEXT_TRANSLATE = _chain(("fulltext", "fulltext"), ("translate", "translation"))
TRANSLATE_ONLY = _chain(("translate", "translation"))


def _two_target_translate(*langs: str) -> str:
    """A chain with one translation spec per target language."""
    return json.dumps(
        [
            {
                "plugin_id": "translate",
                "kind": "translation",
                "lang": lang,
                "required": True,
            }
            for lang in langs
        ]
    )


FULLTEXT_SUMMARY_TRANSLATE = _chain(
    ("fulltext", "fulltext"), ("summary", "summary"), ("translate", "translation")
)


@dataclass
class Row:
    plugin_id: str
    kind: str
    lang: str | None
    input_hash: str
    status: str
    payload_ref: str | None
    upstream_plugin_id: str | None
    upstream_digest: str | None


class World:
    """A migrated database, one source, and the operations a test needs."""

    def __init__(
        self, session: Session, factory: sessionmaker[Session], source_id: str
    ):
        self.session = session
        self.factory = factory
        self.source_id = source_id

    def rows(self, plugin_id: str | None = None) -> list[Row]:
        rows = [
            Row(
                plugin_id=row[0],
                kind=row[1],
                lang=row[2],
                input_hash=row[3],
                status=row[4],
                payload_ref=row[5],
                upstream_plugin_id=row[6],
                upstream_digest=row[7],
            )
            for row in self.session.execute(
                sa.text(
                    "select plugin_id, kind, lang, input_hash, status, payload_ref,"
                    " upstream_plugin_id, upstream_digest from item_enrichment"
                    " order by rowid"
                )
            ).all()
        ]
        if plugin_id is None:
            return rows
        return [row for row in rows if row.plugin_id == plugin_id]

    def only(self, plugin_id: str) -> Row:
        rows = self.rows(plugin_id)
        assert len(rows) == 1, f"expected one {plugin_id} row, got {rows}"
        return rows[0]

    def product(self, plugin_id: str) -> str:
        """The artifact a plugin stored -- what the reader would be served."""
        return self.product_of(self.only(plugin_id))

    def product_of(self, row: Row) -> str:
        """The artifact one specific row stored.

        Needed once several rows exist for a plugin: a revised content version
        leaves the previous version's product in place, and a test asking
        "what did the reader get" must name the revision it means.
        """
        from adapters import filestore

        assert row.payload_ref, f"{row.plugin_id} stored no artifact"
        return filestore.read_body(row.payload_ref)

    def succeeded_hash_for(self, plugin_id: str) -> str:
        """The input_hash of the newest succeeded product of `plugin_id`."""
        rows = [row for row in self.rows(plugin_id) if row.status == "succeeded"]
        assert rows, f"no succeeded {plugin_id} row"
        return rows[-1].input_hash

    def gate(self) -> str:
        value = self.session.scalar(sa.text("select publish_status from item"))
        assert value is not None
        return str(value)

    def store_item(self, *, content: str, chain: str, lang: str = "zh") -> str:
        """Store one item through the real pipeline; return its id.

        The same `external_id` on a later call is the same entry re-fetched,
        which is how a revised article arrives (identity kept, body changed).
        """
        from adapters.repository import parse_chain
        from app.pipeline import normalise_and_store
        from core.models import ItemDraft

        self.session.execute(
            sa.text(
                "update source set pipeline_chain = :c, target_lang = :l where id = :s"
            ),
            {"c": chain, "l": lang, "s": self.source_id},
        )
        self.session.commit()
        normalise_and_store(
            self.session,
            [
                ItemDraft(
                    origin_id=self.source_id,
                    external_id="post-1",
                    title="A summary post",
                    canonical_url="https://chain.example/post-1",
                    published_at=None,
                    content=content,
                )
            ],
            chain=parse_chain(chain, target_lang=lang),
        )
        self.session.commit()
        item_id = self.session.scalar(
            sa.text("select id from item where source_id = :s"), {"s": self.source_id}
        )
        assert item_id is not None, "the item must have been stored"
        return str(item_id)

    def sweep(self, processors: dict[str, Any]) -> dict[str, int]:
        from app.enrichment_worker import sweep_queue

        report = sweep_queue(
            self.session,
            processors=processors,
            row_session_factory=self.factory,
        )
        self.session.commit()
        return report


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """A migrated database and one source."""
    from adapters.repository import create_source, timestamp
    from tests.migrated_schema import migrated_database

    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    # The sandboxed child resolves plugin modules through PYTHONPATH; without
    # pinning it to THIS checkout the shared venv's .pth wins and the child
    # imports the main checkout's tree.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    engine = sa.create_engine(migrated_database(tmp_path, "chain-dataflow.db"))
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as session:
            source = create_source(
                session,
                source_type="rss",
                url="https://chain.example/feed",
                canonical_url="https://chain.example/feed",
                schedule_policy="{}",
                tenant_id=TENANT,
                at=timestamp(datetime.now(UTC)),
            )
            session.commit()
            source_id = source.id
        with factory() as session:
            yield World(session=session, factory=factory, source_id=source_id)
    finally:
        engine.dispose()


def run_chain(world: World, processors: dict[str, Any], *, rounds: int = 6) -> None:
    """Sweep until the chain stops making progress.

    A pipeline advances one position per sweep by construction: a spec may not
    be claimed before the product it consumes exists. That is the rule under
    test, not an implementation detail to work around, so the helper drives the
    sweep the way the worker's loop does instead of asserting a single-sweep
    shortcut that would hide a broken ordering rule.
    """
    for _ in range(rounds):
        report = world.sweep(processors)
        if (
            report["succeeded"] == 0
            and report["failed"] == 0
            and report["retried"] == 0
        ):
            return
    raise AssertionError("the chain never settled")


# --------------------------------------------------------------------------
# AC-14: a later spec consumes the earlier spec's product
# --------------------------------------------------------------------------


def test_the_chain_translates_the_full_text_not_the_summary(world: World) -> None:
    """AC-14: `[fulltext, translate]` translates the *full body*.

    This is the defect in the operator's words: the source was configured
    `[fulltext, translate]` and the reader got a translation of the 50-character
    feed summary sitting above a full text in another language -- layers that
    did not match. A chain is a pipeline; the later spec's input is the earlier
    spec's output.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_TRANSLATE)
    run_chain(world, {"fulltext": FulltextV1, "translate": EchoTranslate})

    # The product proves what the downstream processor was handed: it echoes
    # its input, so the artifact *is* the evidence.
    translated = world.product("translate")
    assert "Full article heading" in translated, (
        f"translate must consume the fulltext product; its artifact was {translated!r}"
    )
    assert "Short summary" not in translated, (
        "the feed summary must not be what the translation was made from"
    )


def test_the_row_records_which_upstream_product_it_consumed(world: World) -> None:
    """AC-14: the dependency is *recorded*, not merely obeyed.

    Without a recorded upstream digest, a later "the fulltext changed" can only
    be handled by guessing: re-run everything every sweep (paying a provider
    each time) or re-run nothing (leaving a translation of stale text). The row
    has to say what it consumed.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_TRANSLATE)
    run_chain(world, {"fulltext": FulltextV1, "translate": EchoTranslate})

    translation = world.only("translate")
    assert translation.status == "succeeded"
    assert translation.upstream_plugin_id == "fulltext"
    assert translation.upstream_digest == DIGEST_V1, (
        "the recorded digest is the consumed artifact's own identity"
    )


def test_a_first_spec_records_no_upstream(world: World) -> None:
    """AC-14 (boundary): the first spec has no upstream to consume.

    Guards against the shortcut "every spec consumes the previous product":
    a spec with nothing before it reads the item's own body and records no
    dependency.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_TRANSLATE)
    run_chain(world, {"fulltext": EchoTranslate, "translate": EchoTranslate})

    fulltext = world.only("fulltext")
    assert fulltext.upstream_plugin_id is None
    assert fulltext.upstream_digest is None
    # The first spec read the item's own stored body.
    assert SUMMARY in world.product("fulltext")


def test_a_third_spec_consumes_the_nearest_upstream(world: World) -> None:
    """AC-14 (position): consumption follows chain *position*, not pairing.

    `[fulltext, summary, translate]` must translate the summary, not the full
    text: the nearest preceding consumable product is what the rule selects.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_SUMMARY_TRANSLATE)
    run_chain(
        world,
        {"fulltext": FulltextV1, "summary": EchoSummary, "translate": EchoTranslate},
    )

    assert "A summary product" in world.product("translate"), (
        "the nearest preceding product is the input"
    )
    assert world.only("translate").upstream_plugin_id == "summary"


# --------------------------------------------------------------------------
# AC-15: a lone translate is unchanged
# --------------------------------------------------------------------------


def test_a_lone_translate_still_reads_the_item_body(world: World) -> None:
    """AC-15: the backward-compatibility case that must not regress.

    Most chains are `[translate]` alone. It has no upstream product, so its
    input is the item's own stored body and no upstream is recorded -- the
    behavior every existing deployment depends on.
    """
    world.store_item(content=SUMMARY, chain=TRANSLATE_ONLY)
    world.sweep({"translate": EchoTranslate})

    assert SUMMARY in world.product("translate"), (
        "a lone translate reads the item's own body"
    )
    translation = world.only("translate")
    assert (translation.status, translation.upstream_plugin_id) == ("succeeded", None)
    assert translation.upstream_digest is None, (
        "with no upstream product there is no dependency to record"
    )
    assert world.gate() == "ready", "a completed lone translate still opens the gate"


# --------------------------------------------------------------------------
# AC-16: a revised upstream product is not silently ignored
# --------------------------------------------------------------------------


def test_a_revised_fulltext_is_what_the_later_spec_consumes(world: World) -> None:
    """AC-16: the translation follows the *current* revision of the full text.

    An unpublished item is re-fetched with new content and re-processed. The
    translation must be made from the new full text. Two plausible wrong
    implementations: resolving "the newest succeeded product" picks the
    previous revision when the current one has not finished, and resolving
    "any succeeded product" translates superseded text forever.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_TRANSLATE)
    run_chain(world, {"fulltext": FulltextV1, "translate": EchoTranslate})
    assert "Full article heading" in world.product("translate")
    assert world.only("translate").upstream_digest == DIGEST_V1

    # The source revised the article while the item was still behind the gate:
    # same entry (same guid), new body -> the pipeline takes the new content
    # and re-queues the chain against the new content_hash.
    world.store_item(content=REVISED_SUMMARY, chain=FULLTEXT_TRANSLATE)
    assert world.gate() == "pending", "the revision re-arms the gate"

    run_chain(world, {"fulltext": FulltextV2, "translate": EchoTranslate})

    # The revision produces its own rows: a product is pinned to the content
    # version it was made from, so the previous version's rows remain as stored
    # history rather than being overwritten.
    fulltext_rows = world.rows("fulltext")
    assert len(fulltext_rows) == 2, fulltext_rows
    assert {row.upstream_digest for row in fulltext_rows} == {None}, (
        "the producer never consumes an upstream product"
    )

    # The row for the *current* content version is the one that must reflect
    # the revised product.
    current_hash = world.succeeded_hash_for("fulltext")
    current = [row for row in world.rows("translate") if row.input_hash == current_hash]
    assert len(current) == 1, current
    translation = current[0]
    assert translation.upstream_digest == DIGEST_V2 != DIGEST_V1, (
        "the recorded dependency is the revision actually consumed"
    )
    translated = world.product_of(translation)
    assert "Revised heading" in translated, (
        "the translation must follow the current fulltext revision"
    )
    assert "Full article heading" not in translated, (
        "translating the superseded revision again is the stale-text failure"
    )
    assert world.gate() == "ready"


# --------------------------------------------------------------------------
# AC-17: a failed or unfinished upstream does not silently degrade
# --------------------------------------------------------------------------


def test_a_failed_upstream_leaves_the_later_spec_unrun(world: World) -> None:
    """AC-17: no silent fallback to the summary.

    If fulltext fails, translating the summary is exactly the mismatched-layers
    outcome this issue is about, and it is the tempting shortcut ("there is
    *some* input available"). The later row must not run, and the item must
    stay behind the gate so the operator sees the failure.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_TRANSLATE)
    report = world.sweep({"fulltext": FailingFulltext, "translate": RefusingTranslate})

    assert report["failed"] == 1, report
    fulltext = world.only("fulltext")
    translation = world.only("translate")
    assert fulltext.status == "failed"
    assert translation.status == "queued", (
        "the later row waits for its upstream instead of failing or being skipped"
    )
    assert translation.payload_ref is None, "it produced nothing"
    assert translation.upstream_plugin_id is None, "it consumed nothing"
    assert world.gate() == "pending", "a failed required product keeps the gate shut"


def test_a_later_spec_waits_for_an_upstream_that_has_not_run(world: World) -> None:
    """AC-17 (ordering): the downstream row is not run early.

    Every row of a chain is enqueued at the same instant, so a sweep that
    claimed them together would translate the summary *before* the full text
    exists -- the same wrong outcome arriving as a race. With only the
    downstream processor available, the earlier spec cannot succeed, so the
    downstream row must not be claimed at all.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_TRANSLATE)

    report = world.sweep({"translate": EchoTranslate})

    translation = world.only("translate")
    assert translation.status == "queued", (
        "a downstream row must not run before its upstream product exists"
    )
    assert translation.payload_ref is None, "nothing was produced"
    assert (report["succeeded"], report["failed"]) == (0, 0), report


def test_the_later_spec_runs_once_its_upstream_lands(world: World) -> None:
    """AC-17 (the other half): waiting is not dropping.

    A deferred row must be picked up by the next sweep once its upstream
    product exists -- otherwise the chain would hold the gate forever.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_TRANSLATE)
    processors = {"fulltext": FulltextV1, "translate": EchoTranslate}

    # Sweep 1: the downstream row is refused at claim time, so only the
    # producer runs. (Both are claimed in one sweep otherwise.)
    world.sweep({"fulltext": FulltextV1})
    assert world.only("fulltext").status == "succeeded"
    assert world.only("translate").status == "queued"

    world.sweep(processors)

    assert world.only("translate").status == "succeeded"
    assert "Full article heading" in world.product("translate")
    assert world.gate() == "ready", "the chain completing opens the gate"


# --------------------------------------------------------------------------
# AC-18: the output contract survives the dataflow change
# --------------------------------------------------------------------------


def test_the_stored_translation_corresponds_to_the_full_text(world: World) -> None:
    """AC-18: the reader's layers now actually correspond.

    The merge order (translation -> fulltext -> summary) is pinned by
    test_reader_api.py. What was missing is that the translation corresponded
    to the full text it is displayed above rather than to the summary below it.
    """
    world.store_item(content=SUMMARY, chain=FULLTEXT_TRANSLATE)
    run_chain(world, {"fulltext": FulltextV1, "translate": EchoTranslate})

    stored = {
        row.plugin_id: world.product(row.plugin_id)
        for row in world.rows()
        if row.status == "succeeded"
    }
    assert set(stored) == {"fulltext", "translate"}, stored
    assert "Full article heading" in stored["translate"]
    assert "Short summary" not in stored["translate"]
    assert world.gate() == "ready", "the whole chain succeeding must open the gate"


def test_a_row_left_by_an_edited_chain_falls_back_to_the_item_body(
    world: World,
) -> None:
    """AC-15 (the chain is the authority): a stale row is still processed.

    Planned as `translate`-only with two translation targets: the chain runs,
    then the operator narrows it to a single target while rows from the first
    configuration are still waiting. Those rows name a spec that is no longer
    configured, and the honest reading of "this spec consumes nothing now" is
    the Item's own content -- not a product from a pipeline that no longer
    exists, and not a permanent stall.

    Reachable in practice: it is what happens to queued rows after any chain
    edit, which is why `_resolve_upstream` resolves the configured chain rather
    than trusting the row's stored columns alone.
    """
    # Two targets, neither of them the body's own language: the pipeline drops
    # a translation spec whose text is already in the target language, so an
    # English target here would never queue a row at all.
    world.store_item(content=SUMMARY, chain=_two_target_translate("zh", "ja"))
    assert len(world.rows("translate")) == 2, world.rows()

    # The operator keeps only the Chinese target.
    world.session.execute(
        sa.text(
            "update source set pipeline_chain = :c, target_lang = :l where id = :s"
        ),
        {"c": _two_target_translate("zh"), "l": "zh", "s": world.source_id},
    )
    world.session.commit()

    run_chain(world, {"translate": EchoTranslate})

    kept = [row for row in world.rows("translate") if row.lang == "zh"]
    assert len(kept) == 1, kept
    assert kept[0].status == "succeeded", "the still-configured spec must run"
    assert kept[0].upstream_plugin_id is None

    # The row whose spec was removed from the chain still has to be processed:
    # it must not stall the queue forever.
    removed_spec_rows = [row for row in world.rows("translate") if row.lang == "ja"]
    assert len(removed_spec_rows) == 1, removed_spec_rows
    row = removed_spec_rows[0]
    assert row.status == "succeeded", (
        f"a row left behind by a chain edit must still be processed, got {row}"
    )
    assert row.upstream_plugin_id is None
    assert row.upstream_digest is None, (
        "it consumed nothing, so it records no dependency"
    )
    assert SUMMARY in world.product_of(row), "it read the item's own body"
