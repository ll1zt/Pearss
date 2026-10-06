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
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from adapters import filestore
from adapters.db_models import Item as ItemRow
from adapters.db_models import ItemEnrichment as EnrichmentRow
from adapters.db_models import RawFetch as RawFetchRow
from adapters.db_models import ReadState as ReadStateRow
from adapters.db_models import Source as SourceRow
from adapters.repository import (
    DeletionResult,
    delete_items,
    delete_source,
    delete_source_items,
)
from adapters.timestamps import fixed_width_utc as timestamp
from app.main import create_session_factory
from tests.migrated_schema import migrated_database
from tests.source_seed import seed_source

REPO_ROOT = Path(__file__).resolve().parents[2]

# The recipe targets select by marker, so without this the whole file is
# invisible to `just ci`: pytest's `-m` filter deselects unmarked tests. These
# are organised into classes rather than module-level functions, which is how
# the gap went unnoticed -- every other integration module declares this.
pytestmark = pytest.mark.integration

NOW = timestamp(datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC))


def _delete_items(
    session: Session,
    item_ids: list[str],
    *,
    root: Path | None = None,
    commit: bool = True,
) -> DeletionResult:
    """Delete and reclaim, the way a route does.

    Reclaiming here rather than in the repository is the point of the split
    (ReclaimPlan): files are only removed once the rows are committed, so a
    test that wants to observe the pre-commit state passes commit=False.
    """
    result = delete_items(session, item_ids, root=root)
    if commit:
        session.commit()
        result.reclaim.reclaim()
    return result


def _delete_source_items(
    session: Session, source_id: str, *, root: Path | None = None, **kwargs: Any
) -> DeletionResult:
    result = delete_source_items(session, source_id, root=root, **kwargs)
    session.commit()
    result.reclaim.reclaim()
    return result


def _delete_source(
    session: Session, source_id: str, *, root: Path | None = None
) -> DeletionResult:
    result = delete_source(session, source_id, root=root)
    session.commit()
    result.reclaim.reclaim()
    return result


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """The real factory, so the engine's connection setup is exercised.

    Building an engine here directly would skip the pragmas that
    `create_session_factory` installs -- including `foreign_keys=ON`, which
    every cascade this module relies on depends on. A test that hand-rolled
    its engine would pass while production silently orphaned rows.
    """
    url = migrated_database(tmp_path, "del.db")

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
    seed_source(
        session,
        source_id=source_id,
        url=f"https://example.test/{source_id}.xml",
        schedule_policy='{"type": "fixed", "interval": "30m"}',
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
            origin_id=source_id,
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

            result = _delete_items(session, ["it-1"], root=storage)

            assert result.counts["items"] == 1
            assert result.counts["bodies"] == 1
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

            result = _delete_items(session, ["it-1"], root=storage)

            assert result.counts["items"] == 1
            assert result.counts["bodies"] == 0, "the survivor still needs this file"
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

            assert _delete_items(session, ["it-1"], root=storage).counts["bodies"] == 0
            assert _delete_items(session, ["it-2"], root=storage).counts["bodies"] == 1
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

            result = _delete_items(session, ["it-1"], root=storage)

            assert result.counts["bodies"] == 1
            gone_path = filestore.path_for(gone, root=storage)
            assert gone_path is not None and not gone_path.exists()
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

            result = _delete_items(session, ["it-1"], root=storage)

            assert result.counts["read_states"] == 1
            assert result.counts["enrichments"] == 1
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

            result = _delete_items(session, ["it-1"], root=storage)

            assert result.counts["bodies"] == 2, "item body and product file"
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

            first = _delete_items(session, ["it-1"], root=storage)
            second = _delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert first.counts["items"] == 1
            assert second.counts["items"] == 0
            assert second.counts["bodies"] == 0

    def test_an_empty_list_is_a_no_op(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            counts = _delete_items(session, [], root=storage).counts
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

            result = _delete_items(session, ["it-1"], root=storage)

            assert result.counts["items"] == 1
            assert result.counts["bodies"] == 0

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

            result = _delete_items(session, ["it-1"], root=storage)

            assert result.counts["items"] == 1


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
                    "update origin set seen_frontier = 12345, floor_cursor = 'abc' "
                    "where id = 'src-1'"
                )
            )
            _item(session, item_id="it-1", body="<p>a</p>")
            _item(session, item_id="it-2", body="<p>b</p>")

            counts = _delete_source_items(session, "src-1", root=storage).counts

            assert counts["items"] == 2
            assert session.get(SourceRow, "src-1") is not None
            frontier, floor = session.execute(
                sa.text(
                    "select seen_frontier, floor_cursor from origin where id = 'src-1'"
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

            counts = _delete_source_items(
                session, "src-1", since="2026-06-01T00:00:00.000000Z", root=storage
            ).counts

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

            counts = _delete_source_items(
                session, "src-1", include_published=False, root=storage
            ).counts

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

            counts = _delete_source(session, "src-1", root=storage).counts
            session.commit()

            assert counts == {
                "sources": 1,
                "items": 1,
                "raw_fetches": 0,
                "bodies": 1,
            }
            assert session.get(SourceRow, "src-1") is None
            assert not path.exists()

    def test_fetch_records_go_but_their_payload_column_is_untouched(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """Fetch rows are removed; payload files are not this module's business.

        `RawFetch.payload_ref` is never written -- `pipeline._raw_fetch_row`
        stores only `content_hash`, because a raw feed body is rebuildable from
        the origin and storing it twice is waste. A row carrying one is a shape
        production cannot produce, which is why the deletion path has no
        payload-unlink branch: a loop over an always-NULL column is dead code,
        and it would use the wrong reference test anyway (a fetch payload is
        not an item body, so no item row accounts for it).

        So this asserts the boundary rather than the absent behaviour: the
        fetch row goes, and the payload file is left alone.
        """
        with session_factory() as session:
            _source(session)
            payload = filestore.store_body("<rss>raw</rss>")
            path = filestore.path_for(payload, root=storage)
            assert path is not None and path.is_file()
            session.add(
                RawFetchRow(
                    id="rf-1",
                    origin_id="src-1",
                    source_id="src-1",
                    payload_ref=payload,
                    content_hash="h",
                    status="succeeded",
                    fetched_at=NOW,
                    correlation_id="c",
                )
            )
            session.flush()

            counts = _delete_source(session, "src-1", root=storage).counts

            assert counts["raw_fetches"] == 1
            assert session.get(RawFetchRow, "rf-1") is None
            assert counts["bodies"] == 0, "only item bodies count as reclaimed"
            assert path.is_file(), (
                "payload files are deliberately not reclaimed; if that ever "
                "changes, the unlink must not consult item references"
            )

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

            counts = _delete_source(session, "src-1", root=storage).counts

            assert counts["bodies"] == 0
            assert path.is_file()
            assert filestore.read_body(ref, root=storage) == "<p>same</p>"

    def test_an_unknown_source_reports_zero(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            counts = _delete_source(session, "nope", root=storage).counts
            assert counts["sources"] == 0


class TestTimeWindow:
    """The window bounds are the one filter whose edge case is invisible.

    An item with no publication date sits outside every window by definition,
    but the comparison that expresses that is easy to get wrong in a way that
    only shows up as content disappearing.
    """

    def test_an_until_bound_does_not_sweep_up_undated_items(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """Regression: `until` used to delete every item with no date.

        The filter was `coalesce(published_at, '') <= until`. The empty string
        sorts before every timestamp, so the bound matched every undated row --
        a caller trimming old content would silently lose items that had no
        date at all, which is the opposite of the request.
        """
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
            _item(session, item_id="undated", body="<p>?</p>", published_at=None)

            counts = _delete_source_items(
                session,
                "src-1",
                until="2026-06-01T00:00:00.000000Z",
                root=storage,
            ).counts

            assert counts["items"] == 1, "only the item inside the window"
            assert session.get(ItemRow, "old") is None
            assert session.get(ItemRow, "new") is not None
            assert session.get(ItemRow, "undated") is not None, (
                "an item with no date belongs to no window and must survive"
            )

    def test_a_since_bound_excludes_undated_items(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """The other bound, for the same reason.

        `since` happened to behave correctly, but only by accident of how the
        empty string sorts. Pinning it keeps a future rewrite from reversing
        the two.
        """
        with session_factory() as session:
            _source(session)
            _item(
                session,
                item_id="recent",
                body="<p>r</p>",
                published_at="2026-09-01T00:00:00.000000Z",
            )
            _item(session, item_id="undated", body="<p>?</p>", published_at=None)

            counts = _delete_source_items(
                session,
                "src-1",
                since="2026-06-01T00:00:00.000000Z",
                root=storage,
            ).counts

            assert counts["items"] == 1
            assert session.get(ItemRow, "recent") is None
            assert session.get(ItemRow, "undated") is not None

    def test_a_window_selecting_nothing_deletes_nothing(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        with session_factory() as session:
            _source(session)
            _item(session, item_id="undated", body="<p>?</p>", published_at=None)

            counts = _delete_source_items(
                session,
                "src-1",
                since="2026-01-01T00:00:00.000000Z",
                until="2026-12-31T00:00:00.000000Z",
                root=storage,
            ).counts

            assert counts["items"] == 0
            assert session.get(ItemRow, "undated") is not None


class TestTransactionBoundary:
    """Files must outlive a rollback; rows must not.

    Deletion has two halves with different atomicity: rows are transactional,
    an unlink is not. Removing a file before the commit means a rollback
    restores rows whose bodies are gone -- and `read_body` answers "" for a
    missing file without raising, so the loss is silent.
    """

    def test_a_rollback_keeps_the_body_file(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """Regression: unlinks used to happen inside the transaction.

        After a rollback the row is back, so its body must still be readable --
        otherwise the item is live but blank, with only a log warning to say so.
        """
        with session_factory() as session:
            _source(session)
            ref = _item(session, item_id="it-1", body="<p>keep me</p>")
            assert ref is not None
            path = filestore.path_for(ref, root=storage)
            assert path is not None and path.is_file()
            # Committed before the delete: otherwise the rollback below would
            # discard the seed as well as the delete, and the test would prove
            # nothing about what a rollback does to the body file.
            session.commit()

            # commit=False: the delete happens, then the transaction is thrown
            # away, exactly as an error between the delete and the commit would
            # do in a route.
            _delete_items(session, ["it-1"], root=storage, commit=False)
            session.rollback()

            assert session.get(ItemRow, "it-1") is not None, "the row came back"
            assert path.is_file(), "so its body must have survived the rollback"
            assert filestore.read_body(ref, root=storage) == "<p>keep me</p>"

    def test_a_rollback_leaves_the_refcount_uncorrupted(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """A rolled-back shared deletion must not free the survivor's file."""
        with session_factory() as session:
            _source(session)
            ref = _item(session, item_id="it-1", body="<p>shared</p>")
            _item(session, item_id="it-2", body="<p>shared</p>")
            assert ref is not None
            path = filestore.path_for(ref, root=storage)
            assert path is not None
            session.commit()

            _delete_items(session, ["it-1"], root=storage, commit=False)
            session.rollback()

            assert path.is_file()

            # And the surviving deletion still works afterwards.
            result = _delete_items(session, ["it-1"], root=storage)
            assert result.counts["bodies"] == 0, "it-2 still holds the file"
            assert path.is_file()

    def test_the_plan_names_the_files_before_they_go(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """The plan is computed inside the transaction, acted on after it."""
        with session_factory() as session:
            _source(session)
            ref = _item(session, item_id="it-1", body="<p>x</p>")
            assert ref is not None
            path = filestore.path_for(ref, root=storage)
            assert path is not None

            result = delete_items(session, ["it-1"], root=storage)
            assert result.reclaim.refs == (ref,), "named while the count is true"
            assert path.is_file(), "but nothing on disk has moved yet"

            session.commit()
            assert result.reclaim.reclaim() == 1
            assert not path.exists()

    def test_reclaiming_twice_is_harmless(
        self, session_factory: sessionmaker[Session], storage: Path
    ) -> None:
        """A retry after a partial failure must not raise."""
        with session_factory() as session:
            _source(session)
            _item(session, item_id="it-1", body="<p>x</p>")
            result = delete_items(session, ["it-1"], root=storage)
            session.commit()

            assert result.reclaim.reclaim() == 1
            assert result.reclaim.reclaim() == 0, "already gone; not an error"
