"""Deletion tests: the shared-body rule is the reason this file exists.

Deleting a row is unremarkable. Deleting the *file* a row pointed at is where
this can go wrong, because content-addressed storage means two items with
identical bytes resolve to one file. Get it wrong and a live article loses its
body silently -- `read_body` answers "" for a missing file and logs a single
warning -- so the checks here are about what survives, not just what goes.

Every test drives the repository directly rather than the API: the API tests
prove the endpoints call this, these prove this is correct.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from adapters import filestore
from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment as EnrichmentRow
from adapters.db_models import RawFetch as RawFetchRow
from adapters.db_models import ReadState as ReadStateRow
from adapters.db_models import Source as SourceRow
from adapters.repository import delete_items, delete_source, delete_source_items
from adapters.timestamps import fixed_width_utc as timestamp
from app.main import create_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]

NOW = timestamp(datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """The real factory, so the engine's connection setup is exercised.

    Building an engine here directly would skip the pragmas that
    `create_session_factory` installs -- including `foreign_keys=ON`, which
    every cascade this module relies on depends on. A test that hand-rolled
    its engine would pass while production silently orphaned rows.
    """
    url = str(sa.URL.create("sqlite+pysqlite", database=str(tmp_path / "del.db")))
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    factory = create_session_factory(url)
    try:
        yield factory
    finally:
        factory.kw["bind"].dispose()


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a temp dir.

    `store_body` resolves its root from the environment, so the test sets it
    rather than passing a root everywhere: production goes through the
    environment variable, and a test that bypassed it would not exercise the
    path that actually runs.
    """
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(root))
    return root


def _source(session: Session, source_id: str = "src-1") -> None:
    # The URL follows the id: canonical_url is part of a unique constraint, so
    # two sources in one test cannot share one.
    session.add(
        SourceRow(
            id=source_id,
            type="rss",
            url=f"https://example.test/{source_id}.xml",
            canonical_url=f"https://example.test/{source_id}.xml",
            parser_plugin_id=None,
            schedule_policy='{"type": "fixed", "interval": "30m"}',
            folder="default",
            status="active",
            trust_level="standard",
            tenant_id="default",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()


def _item(
    session: Session,
    *,
    item_id: str,
    source_id: str = "src-1",
    body: str | None = None,
    publish_status: str = "published",
    published_at: str | None = NOW,
) -> str | None:
    """Add an item, storing `body` and returning its content_ref."""
    ref = filestore.store_body(body) if body is not None else None
    session.add(
        ItemRow(
            id=item_id,
            source_id=source_id,
            guid=f"guid-{item_id}",
            content_hash=f"hash-{item_id}",
            title=f"title {item_id}",
            url=f"https://example.test/{item_id}",
            author="",
            published_at=published_at,
            content_ref=ref,
            publish_status=publish_status,
            tenant_id="default",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.flush()
    return ref


def _detail(item_id: str) -> dict[str, str]:
    return {"author": "", "metadata": {}, "id": item_id}


class TestDeleteItems:
    def test_removes_the_row_and_its_body(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            ref = _item(session, item_id="it-1", body="<p>hello</p>")
            assert ref is not None
            path = filestore.path_for(ref, root=storage)
            assert path is not None and path.is_file()

            counts = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert counts["items"] == 1
            assert counts["bodies"] == 1
            assert session.get(ItemRow, "it-1") is None
            assert not path.exists()

    def test_a_shared_body_survives_while_another_item_references_it(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """The case that makes this dangerous.

        Two items with identical bytes share one file. Deleting either must not
        remove it, or the survivor's body silently becomes "".
        """
        with session_factory() as session:
            _source(session)
            ref = _item(session, item_id="it-1", body="<p>shared</p>")
            _item(session, item_id="it-2", body="<p>shared</p>")
            assert ref is not None
            path = filestore.path_for(ref, root=storage)
            assert path is not None

            counts = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert counts["items"] == 1
            assert counts["bodies"] == 0, "the survivor still needs this file"
            assert path.is_file()
            assert filestore.read_body(ref, root=storage) == "<p>shared</p>"

    def test_the_body_goes_once_the_last_reference_does(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """Both orders converge: whichever item is deleted last frees the file."""
        with session_factory() as session:
            _source(session)
            ref = _item(session, item_id="it-1", body="<p>shared</p>")
            _item(session, item_id="it-2", body="<p>shared</p>")
            assert ref is not None
            path = filestore.path_for(ref, root=storage)
            assert path is not None

            assert delete_items(session, ["it-1"], root=storage)["bodies"] == 0
            assert delete_items(session, ["it-2"], root=storage)["bodies"] == 1
            session.commit()

            assert not path.exists()

    def test_a_distinct_body_is_removed_without_affecting_others(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            gone = _item(session, item_id="it-1", body="<p>one</p>")
            kept = _item(session, item_id="it-2", body="<p>two</p>")
            assert gone is not None and kept is not None

            counts = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert counts["bodies"] == 1
            assert not filestore.path_for(gone, root=storage).exists()  # type: ignore[union-attr]
            assert filestore.read_body(kept, root=storage) == "<p>two</p>"

    def test_read_state_and_enrichment_rows_go_with_the_item(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            _item(session, item_id="it-1", body="<p>x</p>")
            session.add(
                ReadStateRow(
                    item_id="it-1",
                    is_read=True,
                    is_starred=False,
                    tenant_id="default",
                    updated_at=NOW,
                )
            )
            session.add(
                EnrichmentRow(
                    id="en-1",
                    item_id="it-1",
                    plugin_id="translate",
                    plugin_version="1",
                    kind="translation",
                    lang="zh",
                    input_hash="h",
                    status="succeeded",
                    tenant_id="default",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.flush()

            counts = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert counts["read_states"] == 1
            assert counts["enrichments"] == 1
            assert session.get(ReadStateRow, ("it-1", "default")) is None
            assert session.get(EnrichmentRow, "en-1") is None

    def test_a_translation_product_file_is_reclaimed_too(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """Products have their own payload file and no cascade to remove it."""
        with session_factory() as session:
            _source(session)
            _item(session, item_id="it-1", body="<p>x</p>")
            translated = filestore.store_body("<p>译文</p>")
            path = filestore.path_for(translated, root=storage)
            assert path is not None and path.is_file()
            session.add(
                EnrichmentRow(
                    id="en-1",
                    item_id="it-1",
                    plugin_id="translate",
                    plugin_version="1",
                    kind="translation",
                    lang="zh",
                    payload_ref=translated,
                    input_hash="h",
                    status="succeeded",
                    tenant_id="default",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.flush()

            counts = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert counts["bodies"] == 2, "item body and product file"
            assert not path.exists()

    def test_unknown_ids_are_not_an_error(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """Idempotence: a retry must report the same success, not a 404.

        The second call is what a client whose response was lost would send.
        """
        with session_factory() as session:
            _source(session)
            _item(session, item_id="it-1", body="<p>x</p>")

            first = delete_items(session, ["it-1"], root=storage)
            second = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert first["items"] == 1
            assert second["items"] == 0
            assert second["bodies"] == 0

    def test_an_empty_list_is_a_no_op(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            counts = delete_items(session, [], root=storage)
            assert counts == {
                "items": 0,
                "read_states": 0,
                "enrichments": 0,
                "bodies": 0,
            }

    def test_an_item_without_a_body_is_deleted_cleanly(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            _item(session, item_id="it-1", body=None)

            counts = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert counts["items"] == 1
            assert counts["bodies"] == 0

    def test_pending_items_are_deletable(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """Deletion is not gated the way output is.

        A pending item is invisible to every reader, but it is exactly what an
        operator wants to remove when a crawl is failing and the queue is
        filling with rows no client will ever see.
        """
        with session_factory() as session:
            _source(session)
            _item(session, item_id="it-1", body="<p>x</p>", publish_status="pending")

            counts = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert counts["items"] == 1


class TestDeleteSourceItems:
    def test_clears_the_items_but_keeps_the_source_and_its_cursor(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """The 'bad crawl, keep collecting' case.

        The cursor must survive: losing it would make the next walk re-sample
        history from the first page.
        """
        with session_factory() as session:
            _source(session)
            session.execute(
                sa.text(
                    "update source set seen_frontier = 12345, floor_cursor = 'abc' "
                    "where id = 'src-1'"
                )
            )
            _item(session, item_id="it-1", body="<p>a</p>")
            _item(session, item_id="it-2", body="<p>b</p>")

            counts = delete_source_items(session, "src-1", root=storage)
            session.commit()

            assert counts["items"] == 2
            assert session.get(SourceRow, "src-1") is not None
            frontier, floor = session.execute(
                sa.text(
                    "select seen_frontier, floor_cursor from source where id = 'src-1'"
                )
            ).one()
            assert frontier == 12345
            assert floor == "abc"

    def test_a_time_window_narrows_what_goes(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            _item(
                session,
                item_id="old",
                body="<p>old</p>",
                published_at="2026-01-01T00:00:00.000000Z",
            )
            _item(
                session,
                item_id="new",
                body="<p>new</p>",
                published_at="2026-09-01T00:00:00.000000Z",
            )

            counts = delete_source_items(
                session, "src-1", since="2026-06-01T00:00:00.000000Z", root=storage
            )
            session.commit()

            assert counts["items"] == 1
            assert session.get(ItemRow, "new") is None
            assert session.get(ItemRow, "old") is not None

    def test_excluding_published_keeps_what_clients_have_seen(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            _item(session, item_id="pub", body="<p>p</p>", publish_status="published")
            _item(session, item_id="ready", body="<p>r</p>", publish_status="ready")

            counts = delete_source_items(
                session, "src-1", include_published=False, root=storage
            )
            session.commit()

            assert counts["items"] == 1
            assert session.get(ItemRow, "pub") is not None
            assert session.get(ItemRow, "ready") is None


class TestDeleteSource:
    def test_removes_the_source_its_items_and_their_bodies(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            ref = _item(session, item_id="it-1", body="<p>x</p>")
            assert ref is not None
            path = filestore.path_for(ref, root=storage)
            assert path is not None

            counts = delete_source(session, "src-1", root=storage)
            session.commit()

            assert counts == {
                "sources": 1,
                "items": 1,
                "raw_fetches": 0,
                "bodies": 1,
            }
            assert session.get(SourceRow, "src-1") is None
            assert not path.exists()

    def test_fetch_records_and_their_payloads_go_too(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            payload = filestore.store_body("<rss>raw</rss>")
            path = filestore.path_for(payload, root=storage)
            assert path is not None and path.is_file()
            session.add(
                RawFetchRow(
                    id="rf-1",
                    source_id="src-1",
                    payload_ref=payload,
                    content_hash="h",
                    status="succeeded",
                    fetched_at=NOW,
                    correlation_id="c",
                )
            )
            session.flush()

            counts = delete_source(session, "src-1", root=storage)
            session.commit()

            assert counts["raw_fetches"] == 1
            assert not path.exists()
            assert session.get(RawFetchRow, "rf-1") is None

    def test_a_shared_body_survives_an_other_sources_item(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """Deleting one source must not blank another source's identical item."""
        with session_factory() as session:
            _source(session, "src-1")
            _source(session, "src-2")
            ref = _item(session, item_id="it-1", source_id="src-1", body="<p>same</p>")
            _item(session, item_id="it-2", source_id="src-2", body="<p>same</p>")
            assert ref is not None
            path = filestore.path_for(ref, root=storage)
            assert path is not None

            counts = delete_source(session, "src-1", root=storage)
            session.commit()

            assert counts["bodies"] == 0
            assert path.is_file()
            assert filestore.read_body(ref, root=storage) == "<p>same</p>"

    def test_an_unknown_source_reports_zero(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            counts = delete_source(session, "nope", root=storage)
            session.commit()
            assert counts["sources"] == 0
