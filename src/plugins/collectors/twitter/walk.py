"""The paged-walk entry point for the X (Twitter) collector.

This is the code that used to live in `app.pipeline.collect_authenticated_pages`.
It moves here because it is collector knowledge: the credentials, the queryId
recovery, the page shape — none of it is scheduling or storage policy. The
pipeline keeps the scheduling decision (`registration.paged`) and the paging
*state* stays in the database, read and written by the host through the small
`PagingState`-shaped value passed in — a collector never touches SQLAlchemy.

The failure contract is the registry's: failures come back as
`CollectionResult` with an ErrorClass, never as exceptions that escape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from adapters.http import FetchError
from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from core.models import ItemDraft
from core.paging import WalkState
from plugins.collectors.twitter import client as x_client
from plugins.collectors.twitter import collector as x_collector
from plugins.collectors.twitter import paging

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class PagedWalkResult:
    """What one walk contributed, before the host stores anything.

    The host owns `normalise_and_store` and the paging-state write; this
    result carries everything those need and nothing else. A failed walk
    carries `error_class` plus a human `detail` — typed fields, not a
    string protocol the host would have to re-parse.
    """

    drafts: list[ItemDraft]
    newest_id: int | None
    resume_cursor: str | None
    error_class: ErrorClass | None = None
    detail: str | None = None
    # Whether this walk ended on the interval's floor. Cursors are opaque, so
    # only this flag authorises the host to move the stored floor.
    reached_floor: bool = False

    @property
    def ok(self) -> bool:
        return self.error_class is None


def collect_pages(
    source_id: str,
    source_url: str,
    *,
    client: httpx.Client,
    state: WalkState,
    max_pages: int = 20,
) -> PagedWalkResult:
    """Walk the Following timeline newest-first, honoring the host's state.

    `state` is the host's persisted paging position (frontier, resume
    cursor, unresolved-rounds count); the walk itself never persists. The
    returned `drafts` are already filtered to what the frontier says is new.

    The budget follows an interval model. The first walk of a source --
    frontier empty, nothing to catch up on -- spends `PEARSS_X_FIRST_PAGES`
    pages, and the oldest tweet it reaches becomes the interval's floor: it
    is recorded as the walk's resume cursor and later cycles stop above it
    rather than chasing it, so history below the floor is left unfetched by
    design. Every later walk walks from the top only until it reaches the
    frontier, but its budget is a safety ceiling, not a target: a timeline
    whose new posts exceed one page must keep paging until the frontier is
    actually crossed, because stopping on budget with the frontier uncrossed
    is exactly how tweets get lost. Hence `ceiling`: the larger of what the
    caller allows and what one catch-up pass can need.
    """
    if not x_client.credentials_configured():
        # ADR 0012: a missing credential stops as `auth`. It is not worked
        # around by trying anonymously, which would only produce a 403 that
        # looks like a different problem.
        return PagedWalkResult(
            drafts=[],
            newest_id=None,
            resume_cursor=None,
            error_class=ErrorClass.AUTH,
            detail="X session credentials are not configured",
        )

    first_pages = x_client.configured_first_pages()
    has_history = state.seen_frontier is not None or bool(state.resume_cursor)
    ceiling = max(max_pages, first_pages)
    budget = first_pages if not has_history else ceiling

    credentials = x_client.Credentials.from_env(client)
    query_id = x_client.current_query_id()
    count = x_client.configured_count()
    # Set when X rejects the session, so the walk can retry once with a fresh
    # pair instead of failing the cycle.
    refreshed = False

    def fetch_page(cursor: str | None) -> paging.Page:
        payload = x_client.fetch_page(
            client=client,
            credentials=credentials,
            query_id=query_id,
            cursor=cursor,
            count=count,
            source_id=source_id,
        )
        body = payload.body
        return paging.Page(
            body=body,
            ids=x_collector.page_ids(body),
            next_cursor=x_collector.page_cursor(body),
            sort_span=x_collector.page_sort_span(body),
        )

    def fetch_with_recovery(cursor: str | None) -> paging.Page:
        """Recover from a rotated queryId or a rejected session, once each.

        X rotates the queryId without notice and answers 400; re-extracting it
        from the site's own bundle is the difference between a self-healing
        source and one that needs a human every few weeks.

        A 401/403 means the session itself went stale -- X rotates `ct0` on
        login, so the cached CookieCloud copy is exactly what was rejected.
        Re-resolving bypasses the cache and picks up what the browser synced.
        Both retries are once-only: looping on a rejection would hammer the
        origin while the real problem is that nobody can authorise us.
        """
        nonlocal query_id, credentials, refreshed
        try:
            return fetch_page(cursor)
        except x_client.QueryIdRejected:
            fresh = x_client.refresh_query_id(client)
            if fresh is None or fresh == query_id:
                raise
            query_id = fresh
            return fetch_page(cursor)
        except FetchError as exc:
            if exc.error_class is not ErrorClass.AUTH or refreshed:
                raise
            refreshed = True
            credentials = x_client.Credentials.refresh(client)
            return fetch_page(cursor)

    try:
        result = paging.two_phase_walk(
            fetch_page=fetch_with_recovery,
            max_pages=budget,
            seen_frontier=state.seen_frontier,
            # The stored cursor is the interval's floor, not a place to resume
            # from: resuming would walk below the oldest tweet ever kept, an
            # unbounded history crawl. As a floor it is a stop sign reached
            # from above, and unlike the old resume it is never dropped --
            # should_drop_resume bounded a backlog that slid backwards every
            # cycle; a floor does not slide, it only marks where history ends.
            floor=state.resume_cursor,
        )
    except x_client.CredentialError as exc:
        # A credential problem, not a transport one: the operator has to act,
        # and `network` would tell them to wait instead.
        return PagedWalkResult(
            drafts=[],
            newest_id=None,
            resume_cursor=None,
            error_class=ErrorClass.AUTH,
            detail=str(exc),
        )
    except Exception as exc:  # classified below; nothing retrieved
        error_class = getattr(exc, "error_class", None)
        return PagedWalkResult(
            drafts=[],
            newest_id=None,
            resume_cursor=None,
            error_class=(
                error_class
                if isinstance(error_class, ErrorClass)
                else ErrorClass.NETWORK
            ),
            detail=str(exc),
        )

    keep = {str(value) for value in result.keep_ids}
    drafts: list[ItemDraft] = []
    for body in result.pages:
        for draft in x_collector.parse(
            RawPayload(
                url=source_url,
                status_code=200,
                body=body,
                source_id=source_id,
            )
        ):
            # The walk decided what is new; a page below the frontier is read
            # for continuity checking, not for ingestion.
            if draft.external_id.removeprefix("twitter:") in keep:
                drafts.append(draft)

    walk_failed = result.failed is not None or not result.ok
    return PagedWalkResult(
        drafts=drafts,
        newest_id=result.newest_id,
        resume_cursor=result.resume,
        error_class=ErrorClass.BLOCKED if walk_failed else None,
        detail=result.failed
        or ("no page could be retrieved" if not result.ok else None),
        reached_floor=result.reached_floor,
    )
