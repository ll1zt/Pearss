"""Reader API HTTP routes (issue #86).

Mounted under /api/greader.php so NetNewsWire's "FreshRSS account" type can log
in. Every Google-Reader-shaped concern -- stream ids, item id forms, the JSON
the clients read -- lives at this boundary or in `adapters/reader_api.py`; the
session-and-transaction work is in `app.reader.operations`. A route here parses
its form, translates the client's shape into domain terms, calls an operation
and renders the answer.

Authentication asymmetry is deliberate and load-bearing -- get this wrong and
no client can authenticate:

    GET  -> Authorization: GoogleLogin auth=<token>
    POST -> form field T=<token>

Clients do not mix them: a GET ignores a query token and a POST ignores the
header. Documented in ADR 0005; the code lives in `app.reader.auth`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final, TypeVar

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from adapters import reader_api
from adapters.db_models import Item as ItemRow
from adapters.timestamps import fixed_width_utc
from app.middleware import CREDENTIALLESS_AUTH_FAILURE
from app.reader import operations
from app.reader.auth import (
    _auth_from_form,
    _parse_google_login,
    authenticate_any,
    configure_reader_credentials,
)
from app.reader.operations import StreamBoundary
from core.reader_state import TagChange, derive_auth_token, verify_password
from core.tenancy import SINGLE_TENANT

CallableT = TypeVar("CallableT", bound=Callable[..., Any])

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/greader.php", tags=["reader-api"])


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
            if not email and not passwd:
                # Issue #18: an empty form probes the endpoint without
                # offering anything to check -- mark it for the audit
                # middleware (a filled-in wrong username/password stays a
                # guess and keeps its audit row).
                request.scope[CREDENTIALLESS_AUTH_FAILURE] = True
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
        "userId": SINGLE_TENANT,
        "userName": SINGLE_TENANT,
        "userProfileId": SINGLE_TENANT,
        "userEmail": SINGLE_TENANT,
    }


@reader_read(router, "/reader/api/0/subscription/list")
def subscription_list(
    request: Request, _: str = Depends(authenticate_any)
) -> dict[str, list[dict[str, object]]]:
    from core.normalize import origin_display_label

    # The source's real folder. Every source has one (the column is
    # NOT NULL DEFAULT 'default'), so the sidebar always has a group to put the
    # subscription in -- and stream/items/ids answers for exactly the folders
    # advertised here.
    subscriptions: list[dict[str, object]] = [
        {
            "id": reader_api.feed_stream_id(row.id),
            "title": origin_display_label(
                title=row.title,
                url=row.url,
                canonical_url=row.canonical_url,
                origin_id=row.id,
            ),
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
        for row in operations.list_subscriptions(request.app.state.session_factory)
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
    tags: list[dict[str, str]] = [{"id": reader_api.STARRED_TAG}]
    tags.extend(
        {"id": reader_api.folder_stream_id(name), "type": "folder"}
        for name in operations.list_folder_names(request.app.state.session_factory)
    )
    return {"tags": tags}


# --------------------------------------------------------------------------
# Subscription writes
# --------------------------------------------------------------------------
#
# Read You adds, renames and removes subscriptions from its own UI, and it
# is the only client that does: NetNewsWire's FreshRSS account is read-only
# over this surface. These three endpoints therefore exist for one client,
# and each is a real write -- audited by the middleware like any POST, and
# attributed in the log.
#
# `subscription/edit`'s `ac=unsubscribe` *is* implemented (2026-09-16). It
# deletes the source and everything it collected -- the ADR 0021 semantics,
# through the same repository entry point `DELETE /v1/sources` uses. See the
# `subscription_edit` docstring for why the earlier refusal was reversed: a
# client whose UI already showed a confirmation dialog only gets a permanent
# retry loop from a refusal.


@router.post("/reader/api/0/subscription/quickadd")
def subscription_quickadd(
    request: Request,
    _: str = Depends(_auth_from_form),
    quickadd: str = Form(default=""),
) -> dict[str, object]:
    """Subscribe to a feed URL -- Read You's "add feed" path.

    The URL arrives already resolved. Read You fetches the address the user
    typed, parses it, follows any `<link rel=alternate>` to the actual feed,
    and only then calls this with the *feed's* URL (`RssHelper.searchFeed` ->
    `discoverFeedLink`, then `subscribe(feedLink = resolvedFeedLink)`). So
    this is not a discovery endpoint: it subscribes to what it is given, and
    A5 `discover_feed` -- deferred by decision, not by omission -- stays
    deferred.

    What it does share with discovery is the failure mode worth guarding: the
    client `requireNotNull`s `streamId` and lets the exception escape into
    `sync`, whose catch answers `Result.retry()`. An endpoint that returned
    no id would therefore not fail one "add feed" -- it would put the whole
    account into a retry loop. Hence a real subscription and a real id, or an
    error, never a well-formed empty reply.

    Idempotent by the identity of the source: re-adding a URL returns the
    existing subscription, which is what `create_source` already does for the
    same reason (`POST /v1/sources` relies on it).
    """
    target = quickadd.strip()
    if not target:
        raise HTTPException(status_code=400, detail="quickadd requires a feed URL")

    try:
        source_id, title = operations.subscribe(
            request.app.state.session_factory, url=target, at=datetime.now(UTC)
        )
    except operations.InvalidSourceUrl as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    LOGGER.info("reader quickadd: %r -> source %s", target, source_id)
    return {
        "numResults": 1,
        "query": target,
        "streamId": reader_api.feed_stream_id(source_id),
        # Null until the first collection names it, which is the honest
        # answer: the origin's title is discovered at collection time.
        "streamName": title,
    }


@router.post("/reader/api/0/rename-tag")
def rename_tag(
    request: Request,
    _: str = Depends(_auth_from_form),
    s: str = Form(default=""),
    dest: str = Form(default=""),
) -> PlainTextResponse:
    """Rename a folder -- Read You's group rename.

    Folders in Pearss are not entities: a source carries a `folder` name and
    the set of folders is derived from those names (`tag/list` reads them
    back with a DISTINCT). So renaming is a rename of the *value*, applied to
    every source that carries it, and an empty folder needs no handling
    because an empty folder does not exist.

    Both ids arrive as `user/-/label/<name>` streams. A `dest` that is not a
    label stream is a client bug worth refusing rather than guessing at.
    """
    from core.folders import DEFAULT_FOLDER, normalize_folder

    origin = reader_api.label_stream_name(s)
    if origin is None:
        raise HTTPException(
            status_code=400, detail="s must be a user/-/label/ stream id"
        )
    target = reader_api.label_stream_name(dest)
    if target is None:
        raise HTTPException(
            status_code=400, detail="dest must be a user/-/label/ stream id"
        )
    try:
        new_folder = normalize_folder(target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if new_folder == DEFAULT_FOLDER:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{DEFAULT_FOLDER!r} is the default folder and "
                "cannot be a rename target"
            ),
        )

    moved = operations.move_folder(
        request.app.state.session_factory,
        origin=origin,
        dest=new_folder,
        at=datetime.now(UTC),
    )
    LOGGER.info(
        "reader rename-tag: folder %r -> %r (%d source(s))",
        origin,
        new_folder,
        moved,
    )
    return PlainTextResponse("OK")


@router.post("/reader/api/0/disable-tag")
def disable_tag(
    request: Request,
    _: str = Depends(_auth_from_form),
    s: str = Form(default=""),
) -> PlainTextResponse:
    """Remove a folder -- Read You's group delete.

    Read You deletes a group's feeds first (`deleteFeed` per feed) and then
    calls this. Since a folder is a value on the sources rather than a row,
    "removing" it means moving its sources back to the default folder: the
    group stops existing because nothing carries its name any more, and the
    subscriptions inside it survive rather than vanishing with it.

    Refusing when sources remain would be defensible, but it would break the
    client's intent: it has already unsubscribed the feeds it meant to, and
    a refusal here leaves the remainder in a folder the user just deleted.
    """
    from core.folders import DEFAULT_FOLDER

    origin = reader_api.label_stream_name(s)
    if origin is None:
        raise HTTPException(
            status_code=400, detail="s must be a user/-/label/ stream id"
        )
    if origin == DEFAULT_FOLDER:
        raise HTTPException(
            status_code=400, detail="the default folder cannot be removed"
        )

    moved = operations.move_folder(
        request.app.state.session_factory,
        origin=origin,
        dest=DEFAULT_FOLDER,
        at=datetime.now(UTC),
    )
    LOGGER.info(
        "reader disable-tag: folder %r removed, %d source(s) returned to default",
        origin,
        moved,
    )
    return PlainTextResponse("OK")


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
    ot: str | None = Query(default=None),
    c: str | None = Query(default=None),
) -> dict[str, object]:
    """Item ids for a stream.

    `c` is a continuation cursor: the reply carries `continuation` while more
    items remain, and the client follows it. That is part of the protocol,
    not an optimisation -- a client that cannot page can only ever see the
    first page, and it has no way to tell a truncated answer from a complete
    one. Read You's `fetchItemIdsAndContinue` loops `while continuation !=
    null`, so omitting the cursor made it treat the first 1000 ids as the
    whole read set, which is how a read item marked in another client failed
    to propagate.

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

    The page size bounds the *answer*, so a short page is meaningful: if
    fewer ids come back than were asked for, the stream is exhausted. That is
    what lets the cursor below be a plain offset rather than something the
    client has to interpret -- it never has to compare pages, only to keep
    asking while a cursor is offered.

    Pending items are always excluded: the gate applies to the Reader API just
    as it does to RSS.

    `ot` is a lower bound on `published_at`, in Unix seconds. ReadYou sends it
    on every global sync (`getReadItemIds(since = now - 1 month)`) and pages to
    exhaustion, so honouring it bounds the walk to the window the client asked
    for instead of the whole read history.
    """
    # An unrecognised state yields nothing rather than being dropped: a filter
    # we cannot honour must not widen into the unfiltered list.
    include_states = tuple(reader_api.tag_states(it))
    exclude_states = tuple(reader_api.tag_states(xt))
    if it and not include_states:
        return {"itemRefs": [], "continuation": None}
    if xt and not exclude_states:
        return {"itemRefs": [], "continuation": None}

    offset, boundary, exhausted = _decode_cursor(c)
    if exhausted:
        # The depth cap ends the walk: an empty page and no cursor, so a client
        # looping `while continuation != null` stops instead of being fed page
        # one again (issue #275).
        return {"itemRefs": [], "continuation": None}
    limit = max(1, min(n, 10000))
    page, has_more = operations.select_stream_ids(
        request.app.state.session_factory,
        stream_id=s,
        include_states=include_states,
        exclude_states=exclude_states,
        limit=limit,
        offset=offset,
        boundary=boundary,
        published_after=_window_start(ot),
    )
    next_cursor = _encode_cursor(offset, page) if has_more and page else None

    # Decimal, not hex: NetNewsWire does Int(id)! on these values and drops
    # every entry it cannot parse as an integer.
    return {
        "itemRefs": [
            {"id": str(reader_api.reader_id(item_id))} for item_id, _published in page
        ],
        # A cursor, never null-unless-exhausted. Read You loops
        # `while continuation != null`, so omitting it here would terminate
        # its walk after one page and it would treat what it saw as the whole
        # stream -- the silent-subset failure this endpoint has already
        # produced twice (the ignored `it=`, the post-LIMIT `xt` filter).
        "continuation": next_cursor,
    }


# A walk that claims more than this many served items is a client bug or an
# attacker; either way it stops there rather than paging without bound. The
# count rides inside the cursor, so the cap terminates the stream: a client
# looping `while continuation != null` receives an empty page with no
# further cursor instead of seeing the same cursor forever.
_MAX_SERVED: Final[int] = 100_000
# Bare integers (the pre-keyset cursor format) still page by offset so an
# in-flight walk from the previous release keeps working, with that
# format's weaker guarantees.
_MAX_OFFSET: Final[int] = 100_000


def _decode_cursor(cursor: str | None) -> tuple[int, StreamBoundary | None, bool]:
    """Split a cursor into its served counter, keyset boundary, and exhaustion.

    An unparseable cursor restarts at the beginning rather than erroring: the
    cursor is ours to produce and the client only echoes it, so a malformed one
    means a bug on this side or a hand-written request, and neither is improved
    by a 500. "Unparseable" is decided by the **shape** -- a decimal served
    count, two `|` separators, a decimal external id -- and that check runs
    before the cap, so the rule holds everywhere instead of being overridden at
    the cap (issue #284).

    The third element is `exhausted`: a **well-formed** keyset cursor at or past
    the cap ends the walk explicitly. Decoding it to `(0, None)` -- which is
    what it used to do -- means "page one", so the walk restarted from the top
    and a client looping `while continuation != null` never finished (issue
    #275).

    The timestamp field is not validated: it is compared lexicographically
    against the column, and we mint it from the column itself, so a
    hand-written value can only reach here by hand. Deciding its shape would
    mean a second definition of the format that `adapters/timestamps` exists to
    be the only one of.
    """
    if not cursor:
        return 0, None, False
    served, separator, rest = cursor.partition("|")
    try:
        count = int(served)
    except ValueError:
        return 0, None, False
    if not separator:
        # Legacy bare offset from the previous release.
        return min(max(0, count), _MAX_OFFSET), None, False
    published, separator, external = rest.partition("|")
    if not separator:
        return 0, None, False
    try:
        internal = reader_api.reader_id_hex(int(external))
    except ValueError:
        return 0, None, False
    if count >= _MAX_SERVED:
        return 0, None, True
    if published == "-":
        return count, StreamBoundary(internal, None, True), False
    return count, StreamBoundary(internal, published, False), False


def _encode_cursor(served: int, page: list[tuple[str, str | None]]) -> str:
    """Bundle the served counter and the page's last row into a cursor.

    The boundary is the last row actually served, so the next page starts
    exactly after it no matter how membership moved in between. Timestamps
    are already fixed-width text in the database, so they embed directly and
    compare lexicographically in SQL exactly as they order.
    """
    item_id, published = page[-1]
    return f"{served + len(page)}|{published or '-'}|{reader_api.reader_id(item_id)}"


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
    external = [
        value
        for value in (reader_api.parse_item_id_param(raw) for raw in i)
        if value is not None
    ]
    if not external:
        return _empty_contents()

    contents = operations.load_contents(
        request.app.state.session_factory, external_ids=external
    )
    if not contents:
        return _empty_contents()

    return _contents_payload(
        [
            _render_item(
                item.row,
                is_read=item.is_read,
                is_starred=item.is_starred,
                body=item.body,
                source_title=item.source_title,
            )
            for item in contents
        ]
    )


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
        # path (ADR 0003). It is sanitised once, at ingest, by
        # `app/pipeline.py` through `adapters/sanitize.py` (nh3, allow-list),
        # so every output path -- RSS, Reader, MCP -- serves the same safe
        # bytes and the Reader layer does not sanitise again. Here it is a
        # JSON string, so there is no XML escape or injection surface.
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
        "author": SINGLE_TENANT,
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

    external = [
        value
        for value in (reader_api.parse_item_id_param(raw) for raw in i)
        if value is not None
    ]
    if not external:
        return PlainTextResponse("OK")

    changed = operations.apply_tag_change(
        request.app.state.session_factory,
        external_ids=external,
        change=TagChange(is_read=is_read, is_starred=is_starred),
        at=datetime.now(UTC),
    )
    LOGGER.info(
        "reader edit-tag: %d item(s), read=%s starred=%s",
        changed,
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
    t: str = Form(default=""),
) -> PlainTextResponse:
    """Move, rename and remove subscriptions -- the app's management writes.

    FreshRSS's contract: `ac=edit`, `s` names the feeds, `a` a label to add,
    `r` one to remove, `t` a new title. A feed can only be in one category,
    so an `a` wins and the folder is set; an `r` without an `a` moves back to
    the default. Unknown feeds are skipped rather than 404'd, the same
    leniency edit-tag applies to item ids -- a client works from the ids
    subscription/list gave it, and one that was withdrawn since should not
    fail the whole edit.

    `t=` renames the source. It was refused silently until 2026-09-16, on
    the reasoning that the next fetch would overwrite the name; reading
    `set_source_title_if_absent` shows that reasoning was wrong -- the
    conditional write fills an absent title and never replaces a present
    one, exactly to protect an operator's rename. A client rename is that
    same choice expressed from an app, so it now lands through the same
    guarded write and sticks (ADR 0024, revision).

    `ac=unsubscribe` deletes the source with everything it collected -- the
    ADR 0021 semantics, through the same repository entry point
    `DELETE /v1/sources` uses. It was refused outright for a day; the
    refusal was reversed because refusing a client whose UI already showed
    a confirmation dialog achieved nothing except a permanent retry loop
    (`deleteFeed` failing reads as sync failure). The delete is real and
    irreversible, as it is through `/v1`; a replay answers OK rather than
    an error, matching the "already gone is done" rule the /v1 endpoint
    established. `ac=subscribe` stays unsupported: subscribing goes through
    `quickadd`, which is what the one client that subscribes actually
    calls.
    """
    from core.folders import DEFAULT_FOLDER, normalize_folder

    if ac == "unsubscribe":
        return _unsubscribe_feeds(request, s)

    if ac != "edit":
        raise HTTPException(
            status_code=400,
            detail=f"unsupported subscription/edit action {ac!r}; "
            "only 'edit' and 'unsubscribe'",
        )

    feed_ids = [
        value
        for value in (reader_api.parse_feed_stream_id(raw) for raw in s)
        if value is not None
    ]
    new_title = t.strip()
    if new_title and feed_ids:
        renamed = operations.rename_sources(
            request.app.state.session_factory,
            source_ids=feed_ids,
            title=new_title,
            at=datetime.now(UTC),
        )
        LOGGER.info("reader subscription rename: %d feed(s) -> %r", renamed, new_title)

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
        # A rename-only edit: the title above already landed.
        return PlainTextResponse("OK")

    if not feed_ids:
        return PlainTextResponse("OK")

    moved = operations.set_sources_folder(
        request.app.state.session_factory,
        source_ids=feed_ids,
        folder=folder,
        at=datetime.now(UTC),
    )
    LOGGER.info(
        "reader subscription/edit: %d feed(s) moved to folder %r",
        moved,
        folder,
    )
    return PlainTextResponse("OK")


def _unsubscribe_feeds(request: Request, s: list[str]) -> PlainTextResponse:
    """Delete the named sources and their history (ADR 0021 semantics).

    "Already gone" answers OK rather than an error: a client resending after a
    dropped response is asking for the subscription to be gone, and it is.
    """
    feed_ids = [
        value
        for value in (reader_api.parse_feed_stream_id(raw) for raw in s)
        if value is not None
    ]
    if not feed_ids:
        return PlainTextResponse("OK")

    for removed in operations.unsubscribe(
        request.app.state.session_factory, source_ids=feed_ids
    ):
        LOGGER.info(
            "reader unsubscribe: %s removed (items=%d, files=%d)",
            removed.source_id,
            removed.items,
            removed.files,
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
    when = _parse_ts(ts) or datetime.now(UTC)
    if reader_api.is_state_stream(s, state="reading-list"):
        source_id: str | None = None
        folder: str | None = None
    elif (feed := reader_api.parse_feed_stream_id(s)) is not None:
        source_id = feed
        folder = None
    elif (label := reader_api.label_stream_name(s)) is not None:
        source_id = None
        folder = label
    else:
        # A scope we do not model gets a refusal, never an unfiltered sweep.
        raise HTTPException(status_code=400, detail=f"unsupported stream {s!r}")

    changed = operations.mark_stream_read(
        request.app.state.session_factory,
        source_id=source_id,
        folder=folder,
        before=when,
    )
    LOGGER.info("reader mark-all-as-read: %d item(s)", changed)
    return PlainTextResponse("OK")


def _parse_ts(value: str | None) -> datetime | None:
    """Reader timestamps are seconds, or microseconds when the value says so.

    The unit is decided by magnitude, not by the width of the string (issue
    #350). `10**12` is the split: 1e12 seconds is year 33658, while 1e12
    microseconds is 2001, so the two plausible ranges do not overlap and the
    value picks its own unit. Deciding by string length read a 15-digit
    microsecond value as seconds -- out of `datetime`'s range, so it crashed
    the endpoint -- and made the rule a guess rather than a property.

    Sub-second precision is dropped: the caller compares whole seconds.
    """
    if not value or not value.strip():
        return None
    raw = value.strip()
    try:
        number = int(raw)
    except ValueError:
        return None
    if number > 10**12:
        number //= 1_000_000
    return datetime.fromtimestamp(number, tz=UTC)


def _window_start(raw: str | None) -> str | None:
    """Google Reader's `ot` as a fixed-width UTC bound, or None for "no bound".

    `ot` is Unix seconds. miniflux applies it only when it is a positive
    integer, comparing strictly (`published_at > ot`). An absent, malformed or
    non-positive value means "no lower bound" rather than "nothing": a filter
    we cannot honour must not narrow the answer to the empty set, the same rule
    an unrecognised state follows. The bound is rendered in the column's own
    fixed-width format, so the comparison is lexicographic and chronological at
    once.
    """
    if raw is None:
        return None
    try:
        seconds = int(raw.strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return fixed_width_utc(datetime.fromtimestamp(seconds, tz=UTC))
