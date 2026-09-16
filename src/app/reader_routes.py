"""Reader API HTTP routes.

Mounted under /api/greader.php so NetNewsWire's "FreshRSS account" type can
log in. All Google-Reader-shaped concerns are delegated to
adapters/reader_api.py; this module only does HTTP.

Authentication asymmetry is deliberate and load-bearing — get this wrong and
no client can authenticate:

    GET  -> Authorization: GoogleLogin auth=<token>
    POST -> form field T=<token>

Clients do not mix them: a GET ignores a query token and a POST ignores the
header. Documented in ADR 0005.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final, TypeVar

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from adapters import filestore, reader_api
from adapters.db_models import Item as ItemRow
from adapters.timestamps import fixed_width_utc as timestamp
from core.reader_state import (
    TagChange,
    derive_auth_token,
    hash_password,
    merge_state,
    verify_password,
)

CallableT = TypeVar("CallableT", bound=Callable[..., Any])

LOGGER = logging.getLogger(__name__)

TENANT = "default"
DEFAULT_USERNAME = "reader"

router = APIRouter(prefix="/api/greader.php", tags=["reader-api"])

_UNAUTHORIZED_HEADERS = {"X-Reader-Google-Bad-Token": "true"}


def _unauthorized() -> HTTPException:
    """The exact shape clients expect on an auth failure under /reader/api/0."""
    return HTTPException(
        status_code=401, detail="Unauthorized", headers=_UNAUTHORIZED_HEADERS
    )


class ReaderAuth:
    """Resolves and validates Reader API tokens."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def token_for(self, session: Session, username: str) -> str | None:
        from adapters.repository import reader_credential

        stored = reader_credential(session, username)
        if stored is None:
            return None
        return derive_auth_token(username=username, password_hash=stored)

    def valid(self, session: Session, token: str | None) -> bool:
        """Constant-time comparison against every configured credential."""
        from adapters.repository import reader_credential

        if not token:
            return False
        from secrets import compare_digest

        matched = False
        for username in _configured_usernames(session):
            stored = reader_credential(session, username)
            if stored is None:
                continue
            expected = derive_auth_token(username=username, password_hash=stored)
            # No early return: comparing against every credential keeps the
            # timing from revealing which username was valid.
            if compare_digest(token, expected):
                matched = True
        return matched


def _configured_usernames(session: Session) -> list[str]:
    """Usernames with a stored credential, plus the environment default."""
    from adapters.db_models import ReaderCredential

    rows = session.execute(
        ReaderCredential.__table__.select().with_only_columns(
            ReaderCredential.__table__.c.username
        )
    ).scalars()
    names = {str(row) for row in rows}
    names.add(os.environ.get("PEARSS_READER_USERNAME", DEFAULT_USERNAME))
    return sorted(names)


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def _auth_from_header(
    request: Request, authorization: str | None = Header(default=None)
) -> str:
    """Authenticate a GET: token comes from the Authorization header only."""
    auth: ReaderAuth = request.app.state.reader_auth
    token = _parse_google_login(authorization)
    with auth._session_factory() as session:
        if not auth.valid(session, token):
            raise _unauthorized()
    return token or ""


def _parse_google_login(header: str | None) -> str | None:
    """Extract the token from `GoogleLogin auth=<token>`."""
    if not header:
        return None
    scheme, _, remainder = header.partition(" ")
    if scheme != "GoogleLogin":
        return None
    field, _, value = remainder.partition("=")
    if field != "auth":
        return None
    return value.strip() or None


def _auth_from_form(
    request: Request, token: str | None = Form(default=None, alias="T")
) -> str:
    """Authenticate a write POST: token comes from the form field T only."""
    auth: ReaderAuth = request.app.state.reader_auth
    with auth._session_factory() as session:
        if not auth.valid(session, token):
            raise _unauthorized()
    return token or ""


async def authenticate_any(request: Request) -> str:
    """Accept the token from the Authorization header or the form body.

    Used by the read endpoints. NetNewsWire sends them as POST with the token
    in the Authorization header and every parameter in the query string, not the
    form-body `T` the Google Reader convention describes. Serving those as
    GET-only produced a wall of 405s and no articles, so the asymmetry is
    enforced where clients depend on it (writes) and relaxed where the real
    client disagrees with the documentation (reads).

    The query string is deliberately not consulted. A token in a URL is
    captured by access logs, proxies and browser history, so accepting it there
    would trade a credential for a convenience no client in fact needs.
    """
    auth: ReaderAuth = request.app.state.reader_auth
    token = _parse_google_login(request.headers.get("authorization"))
    if not token and request.method in {"POST", "PUT", "PATCH"}:
        # Only parse a body when one was actually sent, and never consume it on
        # a GET, where there is nothing to read.
        form = await request.form()
        supplied = form.get("T")
        if isinstance(supplied, str) and supplied.strip():
            token = supplied.strip()
    with auth._session_factory() as session:
        if not auth.valid(session, token):
            raise _unauthorized()
    return token or ""


def reader_read(
    router: APIRouter, path: str, **kwargs: Any
) -> Callable[[CallableT], CallableT]:
    """Register a Reader API read under both GET and POST.

    Google Reader clients are split on this verb: some GET, some POST with all
    parameters in the query string. FreshRSS, which NetNewsWire is speaking
    to, accepts both, so a GET-only implementation failed a third of the
    refresh traffic with 405 Method Not Allowed.

    Typed as identity on the callable so the endpoint's own signature survives
    for FastAPI's introspection and for mypy.
    """

    def decorator(func: CallableT) -> CallableT:
        router.get(path, **kwargs)(func)
        router.post(path, **kwargs)(func)
        return func

    return decorator


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


@router.post("/accounts/ClientLogin", response_model=None)
def client_login(
    request: Request,
    # Google Reader sends capitalised field names, and FastAPI form fields
    # are case-sensitive, so lowercase parameter names would silently receive
    # empty strings and reject every login.
    email: str = Form(default="", alias="Email"),
    passwd: str = Form(default="", alias="Passwd"),
    output: str = Query(default=""),
) -> Response:
    """Exchange credentials for an auth token.

    Returns the auth token in the three-field form clients expect. Never
    reveals whether the username or the password was the wrong part.
    """
    from adapters.repository import reader_credential

    # Sync from the environment on demand. Startup alone is not enough: a
    # database migrated after this process began would otherwise never pick
    # the credential up without a restart.
    configure_reader_credentials(request.app.state.session_factory)

    with request.app.state.reader_auth._session_factory() as session:
        stored = reader_credential(session, email)
        ok = (
            bool(email)
            and bool(passwd)
            and stored is not None
            and verify_password(passwd, stored)
        )
        if not ok:
            LOGGER.info("reader login failed for user %r", email)
            if output == "json":
                raise HTTPException(
                    status_code=401, detail={"error_message": "access unauthorized"}
                )
            raise HTTPException(status_code=401, detail="Unauthorized")
        token = derive_auth_token(username=email, password_hash=stored or "")

    payload = {"SID": token, "LSID": token, "Auth": token}
    if output == "json":
        return JSONResponse(payload)
    return PlainTextResponse(
        "\n".join(f"{key}={value}" for key, value in payload.items())
    )


@reader_read(router, "/reader/api/0/token")
def get_token(
    request: Request, _: str = Depends(authenticate_any)
) -> PlainTextResponse:
    """Return the caller's own token, used by clients as an edit token."""
    token = _parse_google_login(request.headers.get("authorization")) or ""
    return PlainTextResponse(token)


# --------------------------------------------------------------------------
# Account and subscription reads
# --------------------------------------------------------------------------


@reader_read(router, "/reader/api/0/user-info")
def user_info(_: str = Depends(authenticate_any)) -> dict[str, str]:
    return {
        "userId": TENANT,
        "userName": TENANT,
        "userProfileId": TENANT,
        "userEmail": TENANT,
    }


@reader_read(router, "/reader/api/0/subscription/list")
def subscription_list(
    request: Request, _: str = Depends(authenticate_any)
) -> dict[str, list[dict[str, object]]]:
    from adapters.db_models import Source
    from core.enums import SourceStatus
    from core.normalize import source_display_label

    with request.app.state.session_factory() as session:
        rows = (
            session.query(Source)
            .filter(
                Source.tenant_id == TENANT,
                # A disabled source is not a subscription the operator wants
                # collected; listing it would put it back into the client's
                # sidebar on every refresh. Paused stays listed — the operator
                # may resume it, and its history is still readable.
                #
                # Deleted sources need no clause here: deletion removes the row
                # (ADR 0021), so there is nothing left to filter out.
                Source.status != SourceStatus.DISABLED.value,
            )
            .all()
        )

    subscriptions = [
        {
            "id": reader_api.feed_stream_id(row.id),
            "title": source_display_label(
                title=row.title,
                url=row.url,
                canonical_url=row.canonical_url,
                source_id=row.id,
            ),
            # The source's real folder. Every source has one (the column is
            # NOT NULL DEFAULT 'default'), so the sidebar always has a group
            # to put the subscription in -- and stream/items/ids answers for
            # exactly the folders advertised here.
            "categories": [
                {
                    "id": reader_api.folder_stream_id(row.folder),
                    "label": row.folder,
                    "type": "folder",
                }
            ],
            "url": row.url or "",
            "htmlUrl": row.canonical_url or row.url or "",
            "iconUrl": "",
        }
        for row in rows
    ]
    return {"subscriptions": subscriptions}


@reader_read(router, "/reader/api/0/tag/list")
def tag_list(request: Request, _: str = Depends(authenticate_any)) -> dict[str, Any]:
    """Starred state plus the folders that exist.

    A client builds its sidebar from this and from the categories on
    subscription/list; the two must agree about which folders exist or the
    sidebar shows groups with nothing in them (or subscriptions with no
    group). Folders are derived from the sources, so an empty folder simply
    does not appear -- there is no folder entity of its own to persist.
    """
    from sqlalchemy import distinct

    from adapters.db_models import Source

    with request.app.state.session_factory() as session:
        folders = (
            session.query(distinct(Source.folder))
            .filter(Source.tenant_id == TENANT)
            .order_by(Source.folder)
            .all()
        )

    tags: list[dict[str, str]] = [{"id": reader_api.STARRED_TAG}]
    tags.extend(
        {"id": reader_api.folder_stream_id(row[0]), "type": "folder"}
        for row in folders
        if row[0]
    )
    return {"tags": tags}


# --------------------------------------------------------------------------
# Item streams
# --------------------------------------------------------------------------


@reader_read(router, "/reader/api/0/stream/items/ids")
def stream_items_ids(
    request: Request,
    _: str = Depends(authenticate_any),
    s: str = Query(default=reader_api.READING_LIST),
    n: int = Query(default=10000),
    xt: list[str] = Query(default=[]),
    it: list[str] = Query(default=[]),
    c: str | None = Query(default=None),
) -> dict[str, object]:
    """Item ids for a stream.

    `xt` excludes a state, `it` includes one -- both are how a client fetches
    a subset, and both are applied in SQL so `n` bounds the *answer* rather
    than the pre-filter set. That ordering is load-bearing: Read You asks for
    "the newest 1000 unread", and filtering after a limit would hand it fewer
    than it asked for, which it counts as items that do not exist.

    FreshRSS clients express the read stream with `it=`, not `s=`. Read You
    sends `s=reading-list&it=<read>` for a FreshRSS account and `s=<read>` for
    a Google Reader one (`useIt = isFreshRss` in its source). Ignoring `it`
    made that query return the whole library -- and because the client reads
    the result as "these are read", it marked every unread item read on each
    sync, while a parallel branch flipped them back. The unread count
    oscillated between 0 and the total.

    Pending items are always excluded: the gate applies to the Reader API just
    as it does to RSS.
    """
    from adapters.repository import visible_ids_for_stream

    # An unrecognised state yields nothing rather than being dropped: a filter
    # we cannot honour must not widen into the unfiltered list.
    include_states = tuple(reader_api.tag_states(it))
    exclude_states = tuple(reader_api.tag_states(xt))
    if it and not include_states:
        return {"itemRefs": [], "continuation": None}
    if xt and not exclude_states:
        return {"itemRefs": [], "continuation": None}

    with request.app.state.session_factory() as session:
        ids = visible_ids_for_stream(
            session,
            stream_id=s,
            tenant_id=TENANT,
            limit=max(1, min(n, 10000)),
            offset=_offset(c),
            include_states=include_states,
            exclude_states=exclude_states,
        )

    # Decimal, not hex: NetNewsWire does Int(id)! on these values and drops
    # every entry it cannot parse as an integer.
    return {
        "itemRefs": [{"id": str(reader_api.reader_id(item_id))} for item_id in ids],
        "continuation": None,
    }


# A continuation cursor deeper than this many items is a client bug or an
# attacker; either way the answer is an empty stream, not an unbounded scan.
_MAX_OFFSET: Final[int] = 100_000


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return min(max(0, int(cursor)), _MAX_OFFSET)
    except ValueError:
        return 0


@router.post("/reader/api/0/stream/items/contents")
def stream_items_contents(
    request: Request,
    _: str = Depends(authenticate_any),
    i: list[str] = Form(default=[]),
) -> dict[str, object]:
    """Content for specific items.

    Authenticated like a read, not like a write. This endpoint changes nothing
    -- it is POST only because the item ids are too numerous for a query
    string -- and NetNewsWire sends it with the Authorization header and no
    form `T`. Requiring `T` here returned 401 to every real client request,
    which surfaced as a populated article list whose bodies were all blank.
    """
    from adapters.db_models import Item as ItemRow
    from adapters.repository import (
        mark_published,
        read_states_for,
        resolve_reader_ids,
        row_to_item,
        source_display_labels,
    )
    from app.pipeline import bodies_for

    with request.app.state.session_factory() as session:
        external = [
            value
            for value in (reader_api.parse_item_id_param(raw) for raw in i)
            if value is not None
        ]
        if not external:
            return _empty_contents()

        wanted = list(resolve_reader_ids(session, external).values())
        if not wanted:
            return _empty_contents()

        rows = session.query(ItemRow).filter(ItemRow.id.in_(wanted)).all()
        # The gate: a pending item is not served to any client, including this
        # one. netnewswire.md acceptance criterion 7.
        rows = [row for row in rows if row.publish_status != "pending"]
        states = read_states_for(session, [row.id for row in rows], tenant_id=TENANT)
        bodies = bodies_for(session, [row_to_item(row) for row in rows])
        # P3 processor chain: a successful translation merges above the
        # original — the reader subscribed with a target language; the
        # original stays for reference (ADR 0018).
        translations = _translation_refs(session, [row.id for row in rows])
        # One query for the whole page rather than one per item: a contents
        # request can carry a hundred ids, and a client that cannot show a
        # source name is the bug this is here to fix.
        titles = source_display_labels(session, [row.source_id for row in rows])
        # Serving a body through the Reader API is an observable first
        # publication, and state-machines.md requires every output adapter to
        # record it -- only a `published` item has its content frozen. Leaving
        # this to the RSS render meant an item read exclusively through the
        # Reader API stayed `ready` forever, so the immutability guarantee that
        # exists because clients cache content was not in force on the path
        # where clients actually do the caching.
        mark_published(
            session, [row_to_item(row) for row in rows], at=datetime.now(UTC)
        )
        session.commit()

    items = []
    for row in rows:
        state = states.get(row.id)
        body = bodies.get(row.id, "")
        tref = translations.get(row.id)
        if tref:
            body = _merge_translation(filestore.read_body(tref), body)
        items.append(
            _render_item(
                row,
                is_read=bool(state and state.is_read),
                is_starred=bool(state and state.is_starred),
                body=body,
                source_title=titles.get(row.source_id),
            )
        )
    return _contents_payload(items)


def _translation_refs(session: Any, item_ids: list[str]) -> dict[str, str]:
    """Successful translation products for `item_ids`, keyed by item id.

    When several exist (a content change re-processed the item), the newest
    row wins — the same rule the conditional-state lookup uses.
    """
    from adapters.db_models import ItemEnrichment

    if not item_ids:
        return {}
    rows = (
        session.execute(
            select(ItemEnrichment)
            .where(
                ItemEnrichment.item_id.in_(item_ids),
                ItemEnrichment.kind == "translation",
                ItemEnrichment.status == "succeeded",
            )
            .order_by(ItemEnrichment.updated_at.desc())
        )
        .scalars()
        .all()
    )
    refs: dict[str, str] = {}
    for row in rows:
        refs.setdefault(row.item_id, row.payload_ref or "")
    return {item_id: ref for item_id, ref in refs.items() if ref}


def _merge_translation(translation: str, original: str) -> str:
    """Translation on top, original below, one rule between.

    The original is never dropped: the translation is a derived artifact and
    the reader may want to check it against the source. The merge happens at
    the boundary where bodies are already assembled, so every output path
    through here shows the same shape.
    """
    rule = '<hr data-pearss="translation-separator">'
    return f"{translation}{rule}{original}"


def _render_item(
    row: ItemRow,
    *,
    is_read: bool,
    is_starred: bool,
    body: str = "",
    source_title: str | None = None,
) -> dict[str, object]:
    categories = [reader_api.READING_LIST]
    if is_read:
        categories.append(reader_api.READ_TAG)
    if is_starred:
        categories.append(reader_api.STARRED_TAG)

    published = _epoch(row.published_at) if row.published_at else 0
    item: dict[str, object] = {
        "id": reader_api.encode_item_id(reader_api.reader_id(row.id)),
        "categories": categories,
        "title": row.title,
        "crawlTimeMsec": str(published * 1000),
        "timestampUsec": str(published * 1000000),
        "published": published,
        "updated": published,
        "alternate": [{"href": row.url, "type": "text/html"}],
        # The body is untrusted remote HTML, same trust decision as the RSS
        # path (ADR 0003). Here it is a JSON string, so there is no XML escape
        # or injection surface; the reader applies its own sanitising rules.
        "summary": {"direction": "ltr", "content": body},
        "content": {"direction": "ltr", "content": body},
        "origin": {
            "streamId": reader_api.feed_stream_id(row.source_id),
            "title": source_title,
            "htmlUrl": row.url,
        },
        "enclosure": [],
        "canonical": [{"href": row.url}],
    }
    # Omitted rather than sent empty, matching Google Reader: FreshRSS only
    # sets this key when the author is non-empty. An empty string reads as
    # "anonymous" to some clients, and we mean "the origin named nobody".
    if row.author:
        item["author"] = row.author
    return item


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _empty_contents() -> dict[str, object]:
    return _contents_payload([])


def _contents_payload(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "direction": "ltr",
        "id": reader_api.READING_LIST,
        "title": "Reading List",
        "self": [{"href": "/api/greader.php/reader/api/0/stream/items/contents"}],
        "updated": int(datetime.now(UTC).timestamp()),
        "author": TENANT,
        "items": items,
    }


# --------------------------------------------------------------------------
# State writes
# --------------------------------------------------------------------------


@router.post("/reader/api/0/edit-tag")
def edit_tag(
    request: Request,
    _: str = Depends(_auth_from_form),
    i: list[str] = Form(default=[]),
    a: list[str] = Form(default=[]),
    r: list[str] = Form(default=[]),
) -> PlainTextResponse:
    """Mark items read/unread and starred/unstarred.

    Idempotent by construction: the same request replayed produces the same
    state, because every write is last-write-wins on a fixed pair of flags.
    """
    try:
        is_read, is_starred = reader_api.classify_tags(added=a, removed=r)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if is_read is None and is_starred is None:
        return PlainTextResponse("OK")

    from adapters.db_models import Item as ItemRow
    from adapters.repository import (
        read_states_for,
        resolve_reader_ids,
        upsert_read_state,
    )

    external = [
        value
        for value in (reader_api.parse_item_id_param(raw) for raw in i)
        if value is not None
    ]
    if not external:
        return PlainTextResponse("OK")

    now = datetime.now(UTC)
    with request.app.state.session_factory() as session:
        item_ids = list(resolve_reader_ids(session, external).values())
        # Only touch items that exist and are visible; a client may still hold
        # ids for items that were withdrawn.
        rows = session.query(ItemRow).filter(ItemRow.id.in_(item_ids)).all()
        known = {row.id for row in rows}
        states = read_states_for(session, list(known), tenant_id=TENANT)

        for item_id in sorted(known):
            merged = merge_state(
                item_id=item_id,
                current=states.get(item_id),
                change=TagChange(is_read=is_read, is_starred=is_starred),
                at=now,
            )
            upsert_read_state(session, merged, tenant_id=TENANT)
        session.commit()

    LOGGER.info(
        "reader edit-tag: %d item(s), read=%s starred=%s",
        len(known),
        is_read,
        is_starred,
    )
    return PlainTextResponse("OK")


@router.post("/reader/api/0/subscription/edit")
def subscription_edit(
    request: Request,
    _: str = Depends(_auth_from_form),
    ac: str = Form(default="edit"),
    s: list[str] = Form(default=[]),
    a: list[str] = Form(default=[]),
    r: list[str] = Form(default=[]),
) -> PlainTextResponse:
    """Move subscriptions between folders -- the drag-and-drop write path.

    FreshRSS's contract: `ac=edit`, `s` names the feeds, `a` a label to add,
    `r` one to remove. A feed can only be in one category, so an `a` wins and
    the folder is set; an `r` without an `a` moves back to the default.
    Unknown feeds are skipped rather than 404'd, the same leniency edit-tag
    applies to item ids -- a client works from the ids subscription/list gave
    it, and one that was withdrawn since should not fail the whole edit.

    Renaming is not implemented, and a `t=` is answered with the same OK the
    rest of the edit produces. That is deliberate rather than lazy: a source's
    title belongs to the origin (it is discovered at collection time and
    recorded once), so a client rename would be overwritten by the next fetch
    anyway. Accepting it silently and failing to honour it would be worse than
    this -- the reply is honest about everything this endpoint owns.
    """
    from adapters.db_models import Source as SourceRow
    from core.enums import SourceStatus
    from core.folders import DEFAULT_FOLDER, normalize_folder

    if ac != "edit":
        raise HTTPException(
            status_code=400,
            detail=f"unsupported subscription/edit action {ac!r}; only 'edit'",
        )

    add_label = next((v for v in a if v), None)
    remove_label = next((v for v in r if v), None)
    if add_label and not add_label.startswith("user/-/label/"):
        raise HTTPException(
            status_code=400,
            detail="a folder must be a user/-/label/ stream id",
        )

    # Both sent is a client mistake about its own model; the add is what such
    # a drag means (moved INTO the target folder), so it decides.
    if add_label:
        try:
            folder = normalize_folder(add_label.removeprefix("user/-/label/"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif remove_label:
        folder = DEFAULT_FOLDER
    else:
        # A rename (t=) or a no-op edit: nothing this endpoint owns changes.
        return PlainTextResponse("OK")

    feed_ids = [
        value
        for value in (reader_api.parse_feed_stream_id(raw) for raw in s)
        if value is not None
    ]
    if not feed_ids:
        return PlainTextResponse("OK")

    now = datetime.now(UTC)
    with request.app.state.session_factory() as session:
        rows = (
            session.query(SourceRow)
            .filter(
                SourceRow.id.in_(feed_ids),
                SourceRow.tenant_id == TENANT,
                # Re-filing a source the operator has taken out of service
                # would move it back into the sidebar on the client's next
                # subscription/list. Deleted rows need no clause: deletion
                # removes them (ADR 0021).
                SourceRow.status != SourceStatus.DISABLED.value,
            )
            .all()
        )
        for row in rows:
            row.folder = folder
            row.updated_at = timestamp(now)
        session.commit()

    LOGGER.info(
        "reader subscription/edit: %d feed(s) moved to folder %r",
        len(rows),
        folder,
    )
    return PlainTextResponse("OK")


@router.post("/reader/api/0/mark-all-as-read")
def mark_all_as_read(
    request: Request,
    _: str = Depends(_auth_from_form),
    s: str = Form(default=reader_api.READING_LIST),
    ts: str | None = Form(default=None),
) -> PlainTextResponse:
    """Mark everything in a stream read up to a timestamp."""
    from adapters.repository import mark_all_read

    when = _parse_ts(ts) or datetime.now(UTC)
    source_id = reader_api.parse_feed_stream_id(s)
    with request.app.state.session_factory() as session:
        changed = mark_all_read(
            session, tenant_id=TENANT, before=when, source_id=source_id
        )
        session.commit()
    LOGGER.info("reader mark-all-as-read: %d item(s)", changed)
    return PlainTextResponse("OK")


def _parse_ts(value: str | None) -> datetime | None:
    """Reader timestamps are seconds, or microseconds when very long."""
    if not value or not value.strip():
        return None
    raw = value.strip()
    try:
        number = int(raw)
    except ValueError:
        return None
    if len(raw) >= 16:
        number //= 1_000_000
    return datetime.fromtimestamp(number, tz=UTC)


def configure_reader_credentials(
    session_factory: sessionmaker[Session], *, at: datetime | None = None
) -> bool:
    """Sync the environment credential into the database.

    Returns True if a credential was stored. The plaintext password is read
    from the environment and never persisted.
    """
    from sqlalchemy import inspect
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from adapters.repository import upsert_reader_credential

    username = os.environ.get("PEARSS_READER_USERNAME", DEFAULT_USERNAME)
    password = os.environ.get("PEARSS_READER_PASSWORD", "")
    if not password:
        LOGGER.warning("PEARSS_READER_PASSWORD unset; Reader API login disabled")
        return False

    try:
        from adapters.db_models import ReaderCredential

        with session_factory() as session:
            # Starting up must not depend on the schema being current: a fresh
            # clone has credentials set but an unmigrated database, and failing
            # the boot there is unrecoverable without manual intervention.
            bind = session.get_bind()
            if "reader_credential" not in inspect(bind).get_table_names():
                LOGGER.warning(
                    "reader_credential table missing; run 'just migrate' "
                    "before using the Reader API"
                )
                return False
            # Rehash only when the password actually changed. The auth token
            # is derived from the stored hash, and scrypt salts are random,
            # so an unconditional rehash would mint a fresh token on every
            # login - each ClientLogin invalidating the token it had just
            # handed the same client's concurrent requests (observed as
            # alternating 200/401 in NNW's refresh). verify_password keeps
            # the check honest: a changed password rehashes, an unchanged
            # one keeps its hash and every outstanding token stays valid.
            row = session.get(ReaderCredential, username)
            if row is None or not verify_password(password, row.password_hash):
                upsert_reader_credential(
                    session,
                    username=username,
                    password_hash=hash_password(password),
                    at=at or datetime.now(UTC),
                )
                session.commit()
    except (OperationalError, ProgrammingError) as exc:
        LOGGER.warning("could not sync reader credential at startup: %s", exc)
        return False
    return True
