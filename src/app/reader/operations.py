"""Reader API application-layer operations (issue #86).

Each function here owns one operation's session, transaction and repository
call sequence, expressed in domain terms -- source ids, folders, external item
ids, read flags. Splitting the monolith left the routes doing HTTP: parse the
form, translate Google Reader's stream ids into these terms, call an operation,
render the result. No function here parses a stream id or writes Google Reader
JSON; that stays at the compatibility boundary.

The one rule the issue names is that the "transaction + replay" sequence was
every caller's own responsibility before this module existed. It is now written
once per operation, so a route cannot forget to commit or to reclaim files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from adapters import filestore
from adapters.db_models import Item as ItemRow
from adapters.timestamps import fixed_width_utc as timestamp
from core.reader_state import TagChange
from core.tenancy import SINGLE_TENANT

LOGGER = logging.getLogger(__name__)


class InvalidSourceUrl(ValueError):
    """The URL cannot be used as an rss source (bad shape, scheme or host)."""


class StreamBoundary(NamedTuple):
    """The keyset boundary a continuation cursor carries.

    `item_id` is the internal id of the last item actually served, and
    `published_at`/`published_null` place it in the ordering: dated rows by
    timestamp, undated rows last, by id. Together they are stable under
    membership changes between pages, which an offset is not.
    """

    item_id: str
    published_at: str | None
    published_null: bool


@dataclass(frozen=True, slots=True)
class Subscription:
    """One subscription as the sidebar needs it, before stream-id encoding."""

    id: str
    title: str | None
    url: str | None
    canonical_url: str | None
    folder: str


@dataclass(frozen=True, slots=True)
class ContentItem:
    """One item as the contents endpoint needs it, before rendering.

    `body` is already assembled -- the stored body with any processor product
    merged over it -- and the flags are the read state. Turning this into
    Google Reader's JSON is the route's job.
    """

    row: Any
    is_read: bool
    is_starred: bool
    body: str
    source_title: str | None


@dataclass(frozen=True, slots=True)
class UnsubscribedSource:
    """What one `unsubscribe` removed, for the operator-facing log line."""

    source_id: str
    items: int
    files: int


# --------------------------------------------------------------------------
# Subscription reads
# --------------------------------------------------------------------------


def list_subscriptions(session_factory: sessionmaker[Session]) -> list[Subscription]:
    """Subscriptions the client should see, disabled ones excluded.

    A disabled source is not a subscription the operator wants collected;
    listing it would put it back into the client's sidebar on every refresh.
    Paused stays listed -- the operator may resume it, and its history is still
    readable. Deleted sources need no clause: deletion removes the row
    (ADR 0021), so there is nothing left to filter out.
    """
    from adapters.db_models import Origin as OriginRow
    from adapters.db_models import Subscription as SubscriptionRow
    from core.enums import SourceStatus

    with session_factory() as session:
        rows = (
            session.query(OriginRow, SubscriptionRow)
            .join(SubscriptionRow, SubscriptionRow.origin_id == OriginRow.id)
            .filter(
                OriginRow.tenant_id == SINGLE_TENANT,
                SubscriptionRow.status != SourceStatus.DISABLED.value,
            )
            .all()
        )
        return [
            Subscription(
                id=origin.id,
                title=origin.title,
                url=origin.url,
                canonical_url=origin.canonical_url,
                folder=subscription.folder,
            )
            for origin, subscription in rows
        ]


def list_folder_names(session_factory: sessionmaker[Session]) -> list[str]:
    """The folder names that exist, derived from the sources that carry them.

    Folders are not entities: an empty folder simply does not appear, because
    there is no folder row to persist.
    """
    from sqlalchemy import distinct

    from adapters.db_models import Subscription as SubscriptionRow

    with session_factory() as session:
        rows = (
            session.query(distinct(SubscriptionRow.folder))
            .filter(SubscriptionRow.tenant_id == SINGLE_TENANT)
            .order_by(SubscriptionRow.folder)
            .all()
        )
    return [str(row[0]) for row in rows if row[0]]


# --------------------------------------------------------------------------
# Subscription writes
# --------------------------------------------------------------------------


def subscribe(
    session_factory: sessionmaker[Session], *, url: str, at: datetime
) -> tuple[str, str | None]:
    """Create (or return) the rss source for `url`; returns `(id, title)`.

    Idempotent by the identity of the source: re-adding a URL returns the
    existing subscription, which is what `create_source` already does for the
    same reason (`POST /v1/sources` relies on it). A URL that cannot be a
    source raises `InvalidSourceUrl`, which the route renders as a 400.

    The schedule is the same adaptive policy a feed gets when nothing is
    specified: the client sends none, and a source that never collects would
    look like a broken feed.
    """
    from adapters.repository import create_source
    from core.enums import SourceType
    from core.folders import DEFAULT_FOLDER
    from core.models import schedule_policy_from_json, schedule_policy_to_json
    from core.normalize import origin_canonical_key

    try:
        canonical = origin_canonical_key(source_type=SourceType.RSS.value, url=url)
    except ValueError as exc:
        raise InvalidSourceUrl(str(exc)) from exc

    policy = schedule_policy_to_json(
        schedule_policy_from_json({"type": "adaptive", "min": "15m", "max": "24h"})
    )
    with session_factory() as session:
        row = create_source(
            session,
            source_type=SourceType.RSS.value,
            url=url,
            canonical_url=canonical,
            schedule_policy=policy,
            tenant_id=SINGLE_TENANT,
            folder=DEFAULT_FOLDER,
            at=timestamp(at),
        )
        session.commit()
        return row.id, row.title


def move_folder(
    session_factory: sessionmaker[Session], *, origin: str, dest: str, at: datetime
) -> int:
    """Re-file every source whose folder is `origin` to `dest`; returns the count.

    Renaming a folder and removing one are the same operation with different
    validation (issue #86): both re-file every source that carries the name, so
    the old name stops existing because nothing carries it any more. The
    difference -- what may be a target, what may be a source -- is the caller's
    to check.
    """
    from adapters.db_models import Subscription as SubscriptionRow

    now = timestamp(at)
    with session_factory() as session:
        rows = (
            session.query(SubscriptionRow)
            .filter(
                SubscriptionRow.tenant_id == SINGLE_TENANT,
                SubscriptionRow.folder == origin,
            )
            .all()
        )
        for row in rows:
            row.folder = dest
            row.updated_at = now
        session.commit()
    return len(rows)


def set_sources_folder(
    session_factory: sessionmaker[Session],
    *,
    source_ids: list[str],
    folder: str,
    at: datetime,
) -> int:
    """Move the named subscriptions to `folder`; returns how many moved.

    Driven by `subscription/edit`'s `a=`/`r=`. Disabled sources are skipped:
    re-filing a source the operator has taken out of service would move it back
    into the sidebar on the client's next `subscription/list`. Deleted rows
    need no clause -- deletion removes them (ADR 0021). Unknown ids are skipped
    rather than 404'd, the same leniency `edit-tag` applies to item ids.
    """
    from adapters.db_models import Subscription as SubscriptionRow
    from core.enums import SourceStatus

    now = timestamp(at)
    with session_factory() as session:
        rows = (
            session.query(SubscriptionRow)
            .filter(
                SubscriptionRow.id.in_(source_ids),
                SubscriptionRow.tenant_id == SINGLE_TENANT,
                SubscriptionRow.status != SourceStatus.DISABLED.value,
            )
            .all()
        )
        for row in rows:
            row.folder = folder
            row.updated_at = now
        session.commit()
    return len(rows)


def rename_sources(
    session_factory: sessionmaker[Session],
    *,
    source_ids: list[str],
    title: str,
    at: datetime,
) -> int:
    """Set `title` on the named active subscriptions; returns how many changed.

    Driven by `subscription/edit`'s `t=`. The conditional write on the
    collection path fills an absent title and never replaces a present one
    (`set_origin_title_if_absent`), exactly to protect an operator's rename; a
    client rename is that same choice expressed from an app, so it lands here
    and sticks (ADR 0024, revision).
    """
    from adapters.db_models import Origin as OriginRow
    from adapters.db_models import Subscription as SubscriptionRow
    from core.enums import SourceStatus

    now = timestamp(at)
    with session_factory() as session:
        rows = (
            session.query(OriginRow)
            .join(SubscriptionRow, SubscriptionRow.origin_id == OriginRow.id)
            .filter(
                OriginRow.id.in_(source_ids),
                OriginRow.tenant_id == SINGLE_TENANT,
                SubscriptionRow.status != SourceStatus.DISABLED.value,
            )
            .all()
        )
        for row in rows:
            row.title = title
            row.updated_at = now
        session.commit()
    return len(rows)


def unsubscribe(
    session_factory: sessionmaker[Session], *, source_ids: list[str]
) -> list[UnsubscribedSource]:
    """Delete the named sources and their history (ADR 0021 semantics).

    The same repository entry point `DELETE /v1/sources` uses, so the delete
    carries identical consequences -- items, bodies, read state, processor
    products, fetch records. "Already gone" is not an error: a client resending
    after a dropped response is asking for the subscription to be gone, and it
    is. Files are reclaimed after each commit, because an unlink cannot be
    rolled back while rows can.
    """
    from adapters.repository import delete_source as repository_delete_source

    removed: list[UnsubscribedSource] = []
    with session_factory() as session:
        for source_id in source_ids:
            result = repository_delete_source(session, source_id)
            session.commit()
            removed.append(
                UnsubscribedSource(
                    source_id=source_id,
                    items=int(result.counts.get("items", 0)),
                    files=int(result.reclaim.reclaim()),
                )
            )
    return removed


# --------------------------------------------------------------------------
# Read-state writes
# --------------------------------------------------------------------------


def apply_tag_change(
    session_factory: sessionmaker[Session],
    *,
    external_ids: list[int],
    change: TagChange,
    at: datetime,
) -> int:
    """Merge `change` onto the states of the named items; returns how many.

    Only items that exist are touched: a client may still hold ids for items
    that were withdrawn. Idempotent by construction -- the same request
    replayed produces the same state, because every write is last-write-wins on
    a fixed pair of flags.
    """
    from adapters.repository import apply_read_state, resolve_reader_ids

    with session_factory() as session:
        item_ids = list(resolve_reader_ids(session, external_ids).values())
        rows = session.query(ItemRow).filter(ItemRow.id.in_(item_ids)).all()
        known = {row.id for row in rows}
        # The same function `PATCH /v1/items/{id}/read-state` calls (issue #202):
        # this layer owns Google Reader's vocabulary, not the rule about what a
        # state change means.
        for item_id in sorted(known):
            apply_read_state(session, item_id=item_id, change=change, at=at)
        session.commit()
    return len(known)


def mark_stream_read(
    session_factory: sessionmaker[Session],
    *,
    source_id: str | None,
    folder: str | None,
    before: datetime,
) -> int:
    """Mark everything in a scope read up to `before`; returns how many changed.

    The scope arrives in domain terms -- one source, one folder, or neither for
    the whole library. Translating a stream id into this scope is the route's
    job, so an unsupported stream can be refused there instead of sweeping
    everything.
    """
    from adapters.repository import mark_all_read

    with session_factory() as session:
        changed = mark_all_read(
            session,
            tenant_id=SINGLE_TENANT,
            before=before,
            source_id=source_id,
            folder=folder,
        )
        session.commit()
    return int(changed)


# --------------------------------------------------------------------------
# Item streams
# --------------------------------------------------------------------------


def select_stream_ids(
    session_factory: sessionmaker[Session],
    *,
    stream_id: str,
    include_states: tuple[str, ...],
    exclude_states: tuple[str, ...],
    limit: int,
    offset: int,
    boundary: StreamBoundary | None,
    published_after: str | None = None,
) -> tuple[list[tuple[str, str | None]], bool]:
    """One page of a stream's ids and whether more remain.

    Fetches one row beyond the page: its presence is how "there is more" is
    known without a second COUNT over the same filtered set. A count would also
    have to repeat every filter, and a rule written twice is a rule that drifts.
    Each row carries its ordering columns so the caller's cursor can anchor to
    the last row actually served -- an id alone is not a boundary.

    `published_after` is Google Reader's `ot=` lower bound, already rendered as
    a fixed-width UTC timestamp by the route.
    """
    from adapters.repository import visible_ids_for_stream

    with session_factory() as session:
        rows = visible_ids_for_stream(
            session,
            stream_id=stream_id,
            tenant_id=SINGLE_TENANT,
            limit=limit + 1,
            offset=offset,
            after_published_at=boundary.published_at if boundary else None,
            after_item_id=boundary.item_id if boundary else None,
            after_published_null=boundary.published_null if boundary else False,
            include_states=include_states,
            exclude_states=exclude_states,
            published_after=published_after,
            with_ordering=True,
        )
        ordered = cast("list[tuple[str, str | None]]", rows)
    return ordered[:limit], len(ordered) > limit


def load_contents(
    session_factory: sessionmaker[Session], *, external_ids: list[int]
) -> list[ContentItem]:
    """Items for the contents endpoint, gate applied and bodies assembled.

    A pending item is not served to any client, including this one
    (netnewswire.md acceptance criterion 7). Serving a body is an observable
    first publication, and state-machines.md requires every output adapter to
    record it -- only a `published` item has its content frozen. Leaving this
    to the RSS render meant an item read exclusively through the Reader API
    stayed `ready` forever, so the immutability guarantee that exists because
    clients cache content was not in force on the path where clients actually
    do the caching.
    """
    from adapters.repository import (
        mark_published,
        origin_display_labels,
        read_states_for,
        resolve_reader_ids,
        row_to_item,
    )
    from app.pipeline import bodies_for

    with session_factory() as session:
        wanted = list(resolve_reader_ids(session, external_ids).values())
        if not wanted:
            return []

        rows = session.query(ItemRow).filter(ItemRow.id.in_(wanted)).all()
        # The gate: a pending item is not served to any client.
        rows = [row for row in rows if row.publish_status != "pending"]
        states = read_states_for(
            session, [row.id for row in rows], tenant_id=SINGLE_TENANT
        )
        bodies = bodies_for(session, [row_to_item(row) for row in rows])
        # P3 processor chain: successful products merge above the original --
        # fulltext replaces the unreadable summary stub (ADR 0028), and a
        # translation rides on top of that (ADR 0018): the reader subscribed
        # for a translated reading experience, so the stack reads
        # translation -> fulltext -> original. Products never rewrite items.
        products = _product_refs(
            session, [row.id for row in rows], kinds=("fulltext", "translation")
        )
        # One query for the whole page rather than one per item: a contents
        # request can carry a hundred ids, and a client that cannot show a
        # source name is the bug this is here to fix.
        titles = origin_display_labels(session, [row.origin_id for row in rows])
        mark_published(
            session, [row_to_item(row) for row in rows], at=datetime.now(UTC)
        )
        session.commit()

    items: list[ContentItem] = []
    for row in rows:
        state = states.get(row.id)
        body = bodies.get(row.id, "")
        # Innermost first: fulltext wraps the summary, translation wraps the
        # result -- each merge only ever prepends and keeps what came before.
        fref = products.get((row.id, "fulltext"))
        if fref:
            body = _merge_over(filestore.read_body(fref), body, "fulltext")
        tref = products.get((row.id, "translation"))
        if tref:
            body = _merge_over(filestore.read_body(tref), body, "translation")
        items.append(
            ContentItem(
                row=row,
                is_read=bool(state and state.is_read),
                is_starred=bool(state and state.is_starred),
                body=body,
                source_title=titles.get(row.origin_id),
            )
        )
    return items


def _product_refs(
    session: Session, item_ids: list[str], *, kinds: tuple[str, ...]
) -> dict[tuple[str, str], str]:
    """Successful processor products for `item_ids`, keyed by (item, kind).

    When several rows exist per (item, kind) -- a content change re-processed
    the item -- the newest wins, the same rule the conditional-state lookup
    uses. One query serves every kind the reader merges, so a hundred-id
    contents page stays one round trip.
    """
    from adapters.db_models import ItemEnrichment

    if not item_ids:
        return {}
    rows = (
        session.execute(
            select(ItemEnrichment)
            .where(
                ItemEnrichment.item_id.in_(item_ids),
                ItemEnrichment.kind.in_(kinds),
                ItemEnrichment.status == "succeeded",
            )
            .order_by(ItemEnrichment.updated_at.desc())
        )
        .scalars()
        .all()
    )
    refs: dict[tuple[str, str], str] = {}
    for row in rows:
        if not row.payload_ref:
            continue
        refs.setdefault((row.item_id, row.kind), row.payload_ref)
    return refs


def _merge_over(product: str, original: str, kind: str) -> str:
    """The product on top, the original below, one rule between.

    The original is never dropped: a derived artifact is not a replacement, and
    the reader may want to check it against the source. The rule names the kind
    that produced the product.
    """
    rule = f'<hr data-pearss="{kind}-separator">'
    return f"{product}{rule}{original}"
