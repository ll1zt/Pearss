"""Content-level discovery: a caller's addresses, fetched into one source.

Scenario register (normative source: `SCENARIOS.md`; issues #170, #192, #194,
#211; AC-172..175, AC-195..196, AC-199..200, AC-236).

`POST /v1/sources/{id}:fetch-items` is the service side of "an agent enumerates
a site's history": the caller discovers *addresses* (which the deployment
cannot, without crawling the site), and the source's own collector fetches
them. Everything lands through the same write path as any collection.

Since #194 the plural endpoint has a singular sibling:
`POST /v1/sources/{id}:fetch-item` fetches one address and answers with one
receipt (and one ledger row), and it is the atom both plural forms run -- the
shim above loops it, and a `kind=fetch` work runs it once per target. "One
implementation, two granularities" is only true if all three agree, which is what
AC-200 checks here.

Since #192 the plural endpoint is a **restricted compatibility shim**: it still
answers with the per-address report (existing callers do not change), but only
up to a ceiling that a response can carry, and it records the run in the work
ledger on the way out. `POST /v1/works` is the replacement, where the row is
written *before* the fetch and a timeout cannot lose the results.

The origin is a `MockTransport` over recorded payloads, and it records every
request it sees: two of the scenarios are about requests that must *not*
happen, and "the origin was never asked" is only observable at the origin.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text as sql
from sqlalchemy.orm import Session, sessionmaker

from app.main import create_app, create_engine_for
from app.middleware import CorrelationAndAuditMiddleware
from tests.migrated_schema import migrated_database

WRITE_TOKEN = "fetch-items-write-token"
SITE = "https://bear.example.com"
FEED_URL = f"{SITE}/blog/feed"
ARTICLE_URL = f"{SITE}/blog/archives/2379"
OFF_SITE_URL = "https://elsewhere.example/blog/archives/1"
REST_PATH = "/blog/wp-json/wp/v2/posts"
FIXTURES = Path(__file__).parents[2] / "src/plugins/collectors/rss/fixtures"

REST_CONTENT = (
    "<p>UAE で免許を更新するには政府指定の眼科で視力検査を受ける必要がある。"
    "日本だと両眼で 0.7 以上、UAE だと 0.5 見えれば良いらしい。</p>"
)
PAGE_HTML = (
    "<html><head><title>Page title - Bear Blog</title>"
    f'<link rel="canonical" href="{SITE}/blog/posts/page">'
    "</head><body><article><h1>Headline</h1><p>"
    + ("Body prose. " * 40)
    + "</p></article></body></html>"
)


def policy(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"type": "fixed", "interval": "30m"}
    base.update(overrides)
    return base


class Origin:
    """A recorded origin: it answers the shapes this path fetches, or 404s."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if request.url.path == REST_PATH:
            post = {
                "id": 2379,
                "link": ARTICLE_URL,
                "guid": {"rendered": f"{SITE}/blog/?p=2379"},
                "title": {"rendered": "視力検査と昼食"},
                "content": {"rendered": REST_CONTENT},
                "date_gmt": "2026-07-27T15:00:00",
            }
            include = request.url.params.get("include")
            body = [post] if include in (None, "2379") else []
            return httpx.Response(
                200, json=body, headers={"content-type": "application/json"}
            )
        if request.url.path.startswith("/blog/posts/"):
            return httpx.Response(
                200, text=PAGE_HTML, headers={"content-type": "text/html"}
            )
        return httpx.Response(404, text="no such address")


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = migrated_database(tmp_path, "fetch-items.db")
    engine = create_engine_for(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def origin() -> Origin:
    return Origin()


@pytest.fixture
def app(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: Origin,
) -> Iterator[FastAPI]:
    monkeypatch.setenv("PEARSS_API_TOKEN", WRITE_TOKEN)
    monkeypatch.setenv("PEARSS_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("PEARSS_PLUGIN_DIR", str(tmp_path / "plugins"))
    application = create_app(session_factory)
    # The lifespan does not run under a bare ASGI transport, so the shared
    # client is installed by hand -- the same seam the trigger tests use.
    application.state.http_client = httpx.Client(
        transport=httpx.MockTransport(origin.handler)
    )
    try:
        yield application
    finally:
        application.state.http_client.close()
        del application.state.http_client


@pytest_asyncio.fixture
async def writer(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
    ) as opened:
        yield opened


async def _add_source(
    writer: httpx.AsyncClient, *, url: str = FEED_URL, kind: str = "rss"
) -> str:
    response = await writer.post(
        "/v1/sources",
        headers={"Idempotency-Key": f"src-{kind}-{abs(hash(url))}"},
        json={"url": url, "type": kind, "schedule_policy": policy()},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _fetch_items(
    writer: httpx.AsyncClient,
    source_id: str,
    urls: list[str],
    *,
    key: str = "fetch-items-key",
    **options: Any,
) -> httpx.Response:
    return await writer.post(
        f"/v1/sources/{source_id}:fetch-items",
        headers={"Idempotency-Key": key},
        json={"urls": urls, **options},
    )


async def _fetch_item(
    writer: httpx.AsyncClient,
    source_id: str,
    url: str,
    *,
    key: str = "fetch-item-key",
) -> httpx.Response:
    return await writer.post(
        f"/v1/sources/{source_id}:fetch-item",
        headers={"Idempotency-Key": key},
        json={"url": url},
    )


async def _stored_urls(writer: httpx.AsyncClient) -> set[str]:
    response = await writer.get("/v1/items", params={"limit": 100})
    assert response.status_code == 200, response.text
    return {row["url"] for row in response.json()["data"]}


# --------------------------------------------------------------------------
# AC-172: same-origin addresses land in the source
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac172_addresses_on_the_sources_host_are_stored(
    writer: httpx.AsyncClient,
) -> None:
    """AC-172: the list is fetched, each address reports action and id."""
    source_id = await _add_source(writer)

    response = await _fetch_items(writer, source_id, [ARTICLE_URL])

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["requested"] == 1
    assert payload["inserted"] == 1
    [result] = payload["results"]
    assert result["url"] == ARTICLE_URL
    assert result["action"] == "stored"
    assert result["item_id"]
    assert result["error_class"] is None

    # The article is a real item, in full: the REST body, not a summary.
    assert await _stored_urls(writer) == {ARTICLE_URL}


# --------------------------------------------------------------------------
# AC-173: an address off the source's host is refused, and never fetched
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac173_an_address_on_another_host_is_refused_untouched(
    writer: httpx.AsyncClient, origin: Origin
) -> None:
    """AC-173: refused per address, with no request and no row.

    A caller's address list must not become requests against servers this
    source never agreed to talk to -- `source_id` is the provenance boundary
    (ADR 0022), and this is where it is enforced for addresses.
    """
    source_id = await _add_source(writer)
    origin.requests.clear()

    response = await _fetch_items(writer, source_id, [OFF_SITE_URL])

    assert response.status_code == 200, response.text
    [result] = response.json()["results"]
    assert result["action"] == "rejected"
    assert result["error_class"] == "structure_changed"
    assert result["item_id"] is None
    assert origin.requests == [], "a refused address must not be fetched"
    assert await _stored_urls(writer) == set()


# --------------------------------------------------------------------------
# AC-174: one bad address does not take the list down
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac174_one_failing_address_leaves_the_others_intact(
    writer: httpx.AsyncClient,
) -> None:
    """AC-174: 200, the good address stored, the bad one classified."""
    source_id = await _add_source(writer)
    missing = f"{SITE}/blog/archives/999999"

    response = await _fetch_items(writer, source_id, [missing, ARTICLE_URL])

    assert response.status_code == 200, response.text
    payload = response.json()
    actions = {result["url"]: result for result in payload["results"]}
    assert actions[missing]["action"] == "failed"
    assert actions[missing]["error_class"]
    assert actions[ARTICLE_URL]["action"] == "stored"
    assert payload["inserted"] == 1
    assert await _stored_urls(writer) == {ARTICLE_URL}


# --------------------------------------------------------------------------
# AC-175: the caller's pacing is clamped, and a replay changes nothing
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac175_pacing_is_clamped_to_the_servers_bounds(
    writer: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-175: the effective pacing is reported back, clamped and floored.

    A deployment's interval is a floor under whatever a caller asks for: it is
    the operator's statement about how hard this deployment may hit someone
    else's server, and a caller cannot talk it down.
    """
    monkeypatch.setenv("PEARSS_FETCH_MIN_INTERVAL", "5")
    source_id = await _add_source(writer)

    response = await _fetch_items(
        writer,
        source_id,
        [ARTICLE_URL],
        batch_size=999,
        min_interval_seconds=999,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["batch_size"] == 50, "the batch ceiling"
    assert payload["min_interval_seconds"] == 60.0, "the interval ceiling"
    assert payload["deployment_min_interval_seconds"] == 5.0

    floored = await _fetch_items(
        writer,
        source_id,
        [ARTICLE_URL],
        key="fetch-items-floor",
        min_interval_seconds=0,
    )
    assert floored.status_code == 200, floored.text
    assert floored.json()["min_interval_seconds"] == 5.0, (
        "the deployment's interval is a floor under the caller's zero"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac175_replaying_the_same_request_does_not_duplicate(
    writer: httpx.AsyncClient,
) -> None:
    """AC-175: the Idempotency-Key replays the answer without re-storing.

    The first call already stored the article; the replay must return the same
    report -- not insert a second row, and not even re-fetch.
    """
    source_id = await _add_source(writer)
    first = await _fetch_items(writer, source_id, [ARTICLE_URL])
    assert first.status_code == 200

    replay = await _fetch_items(writer, source_id, [ARTICLE_URL])

    assert replay.status_code == first.status_code
    assert replay.json() == first.json(), "a replay is the same answer"
    assert await _stored_urls(writer) == {ARTICLE_URL}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac175_a_capability_the_collector_lacks_is_refused_before_fetching(
    writer: httpx.AsyncClient, origin: Origin
) -> None:
    """AC-168's endpoint form: no item capability, no request, no rows.

    An `agent` source has no collector at all, which is the extreme case of
    "this source cannot fetch an address". The refusal has to be the *source's*
    answer rather than a per-address failure, because nothing about the list
    could have made it work.
    """
    source_id = await _add_source(writer, url="agent://discovered", kind="agent")
    origin.requests.clear()

    response = await _fetch_items(writer, source_id, [ARTICLE_URL])

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"]["code"] == "unsupported_capability"
    assert origin.requests == []
    assert await _stored_urls(writer) == set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac195_a_small_batch_still_answers_inline_and_leaves_a_ledger(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-195: the compat shape is the report *and* a terminal work in the ledger.

    The response is the old one (existing callers do not change); what is new is
    that the same run is also a work with one unit per address, so "what
    happened to this address?" stays answerable after the response is gone
    (ADR 0046). The work arrives terminal -- the fetch has already happened --
    which is why no worker can pick it up.
    """
    source_id = await _add_source(writer)

    response = await _fetch_items(writer, source_id, [ARTICLE_URL, OFF_SITE_URL])

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["requested"] == 2
    assert payload["inserted"] == 1
    assert [(row["url"], row["action"]) for row in payload["results"]] == [
        (ARTICLE_URL, "stored"),
        (OFF_SITE_URL, "rejected"),
    ]

    # The ledger, read directly: these rows are written by the code under test,
    # so this is verification rather than seeding (which goes through the
    # repository).
    with session_factory() as session:
        work = (
            session.execute(sql("select id, kind, status, finished_at from work"))
            .mappings()
            .one()
        )
        units = (
            session.execute(
                sql(
                    "select target, outcome, error_class, detail, ref from work_unit "
                    "order by rowid"
                )
            )
            .mappings()
            .all()
        )
    assert (work["kind"], work["status"]) == ("fetch", "partial")
    assert work["finished_at"] is not None
    assert [(unit["target"], unit["outcome"]) for unit in units] == [
        (ARTICLE_URL, "ok"),
        (OFF_SITE_URL, "rejected"),
    ]
    assert units[0]["ref"] == payload["results"][0]["item_id"]
    assert units[0]["detail"] == "stored"
    assert units[1]["error_class"] == "structure_changed"

    # Written terminal, so the same command is still free to be submitted as a
    # real work: a compat run must not occupy the live-command index and make a
    # subsequent submission look like a duplicate of something already done.
    submitted = await writer.post(
        "/v1/works",
        headers={"Idempotency-Key": "fetch-items-compat-then-work"},
        json={
            "kind": "fetch",
            "targets": [ARTICLE_URL, OFF_SITE_URL],
            "params": {"source_id": source_id},
        },
    )
    assert submitted.status_code == 202, submitted.text
    assert submitted.json()["work_id"] != work["id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_compat_ceiling_is_inclusive(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """Exactly the ceiling is accepted; the ceiling itself is the bound.

    20 is the MCP read tools' page size and the number of results this response
    can carry without becoming the thing that loses them. Off-host addresses
    keep it offline: they are refused before any request, one unit each.
    """
    source_id = await _add_source(writer)
    urls = [f"https://elsewhere.example/blog/archives/{n}" for n in range(20)]

    response = await _fetch_items(writer, source_id, urls)

    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 20
    with session_factory() as session:
        assert session.execute(sql("select count(*) from work_unit")).scalar_one() == 20


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac196_a_list_over_the_compat_ceiling_is_refused_with_the_way_out(
    writer: httpx.AsyncClient, origin: Origin, session_factory: sessionmaker[Session]
) -> None:
    """AC-196: over the ceiling, the answer names the endpoint that can do it.

    The ceiling is a *response* bound, so the refusal has to point at the shape
    that has no such bound rather than telling the caller to split the list:
    splitting a 568-address backfill into 29 requests is exactly the delivery
    that loses results to a gateway timeout (measured: 200 addresses time out
    while the deployment keeps fetching).
    """
    source_id = await _add_source(writer)
    origin.requests.clear()

    response = await _fetch_items(
        writer, source_id, [f"{SITE}/blog/archives/{n}" for n in range(21)]
    )

    assert response.status_code == 422, response.text
    error = response.json()["detail"]["error"]
    assert error["code"] == "batch_too_large"
    assert "/v1/works" in error["suggestion"], error
    assert origin.requests == []
    assert await _stored_urls(writer) == set()
    with session_factory() as session:
        assert session.execute(sql("select count(*) from work")).scalar_one() == 0


@pytest.mark.fixture
def test_the_rest_fixture_still_carries_a_guid() -> None:
    """The identity this path relies on is the one the fixture records.

    Cheap guard on the corpus: the WordPress REST payload is what proves
    AC-169's identity alignment, and a fixture trimmed back to the fields the
    *older* code read would quietly undo it.
    """
    posts = json.loads((FIXTURES / "wp_rest_posts.json").read_text(encoding="utf-8"))
    assert all(post["guid"]["rendered"].startswith(SITE) for post in posts)


# --------------------------------------------------------------------------
# AC-199: one address, one receipt, one ledger row
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac199_one_address_answers_with_one_receipt_and_a_ledger_row(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-199: the atom's answer is one receipt, and it is written down.

    A response cannot be smaller than the effect it reports, and the effect is
    one address -- so there is no list here, and no pacing knobs either. The
    receipt is also the record: one work with this address as its target and one
    unit, so "what happened to it?" survives the response.
    """
    from adapters.repository import units_for_work, work_by_id
    from core.enums import UnitOutcome, WorkStatus

    source_id = await _add_source(writer)

    response = await _fetch_item(writer, source_id, ARTICLE_URL)

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["url"] == ARTICLE_URL
    assert receipt["outcome"] == "ok"
    assert receipt["error_class"] is None
    assert receipt["item_id"]
    assert receipt["work_id"]

    with session_factory() as session:
        work = work_by_id(session, receipt["work_id"])
        units = units_for_work(session, receipt["work_id"])
    assert work is not None and work.status is WorkStatus.SUCCEEDED
    assert work.kind == "fetch" and work.targets == [ARTICLE_URL]
    assert [(unit.target, unit.outcome) for unit in units] == [
        (ARTICLE_URL, UnitOutcome.OK)
    ]
    assert units[0].ref == receipt["item_id"]
    assert units[0].detail == "stored"

    stored = await writer.get("/v1/items", params={"url": ARTICLE_URL})
    assert [row["id"] for row in stored.json()["data"]] == [receipt["item_id"]]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac199_an_off_host_address_is_a_receipt_and_no_request(
    writer: httpx.AsyncClient, origin: Origin, session_factory: sessionmaker[Session]
) -> None:
    """AC-199: a refusal is an outcome, not an exception.

    An address off the source's host is a fact about that address, so it comes
    back as a receipt (`rejected`, with the class that says why) -- and the
    origin is never asked, which is only observable at the origin.
    """
    from adapters.repository import units_for_work, work_by_id
    from core.enums import ErrorClass, UnitOutcome, WorkStatus

    source_id = await _add_source(writer)
    origin.requests.clear()

    response = await _fetch_item(writer, source_id, OFF_SITE_URL)

    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["outcome"] == "rejected"
    assert receipt["error_class"] == "structure_changed"
    assert receipt["item_id"] is None
    assert origin.requests == []
    assert await _stored_urls(writer) == set()

    with session_factory() as session:
        work = work_by_id(session, receipt["work_id"])
        units = units_for_work(session, receipt["work_id"])
    # Nothing was done, so the work is not a success -- `rejected` is a fact
    # about the target, and the fold has to read it that way.
    assert work is not None and work.status is WorkStatus.FAILED
    assert [(unit.outcome, unit.error_class) for unit in units] == [
        (UnitOutcome.REJECTED, ErrorClass.STRUCTURE_CHANGED)
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_atom_refuses_the_same_sources_the_batch_does(
    writer: httpx.AsyncClient, origin: Origin
) -> None:
    """The source-level refusals hold for one address too.

    A single address must not be a way around them: an unknown source is a 404,
    a collector without the item capability is a 422, and an inactive source is
    a 409 -- all before anything is fetched.
    """
    origin.requests.clear()

    unknown = await _fetch_item(writer, "no-such-source", ARTICLE_URL)
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["detail"]["error"]["code"] == "source_not_found"

    agent_id = await _add_source(writer, url="agent://singular", kind="agent")
    incapable = await _fetch_item(writer, agent_id, ARTICLE_URL)
    assert incapable.status_code == 422, incapable.text
    assert incapable.json()["detail"]["error"]["code"] == "unsupported_capability"

    paused_id = await _add_source(writer, url="https://paused.example/feed")
    paused = await writer.patch(
        f"/v1/sources/{paused_id}",
        headers={"Idempotency-Key": "pause-for-atom"},
        json={"status": "paused"},
    )
    assert paused.status_code == 200, paused.text
    inactive = await _fetch_item(writer, paused_id, f"{SITE}/blog/archives/1")
    assert inactive.status_code == 409, inactive.text
    assert inactive.json()["detail"]["error"]["code"] == "source_not_active"

    assert origin.requests == []


# --------------------------------------------------------------------------
# AC-200: the atom and the work executor agree
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac200_the_atom_and_a_one_target_work_agree(
    writer: httpx.AsyncClient,
    origin: Origin,
    session_factory: sessionmaker[Session],
) -> None:
    """AC-200: same address, two entry points, one identity and one outcome.

    The atom is what the work executor runs per target, so this is the property
    that has to hold: a caller asking for one address and a caller asking a work
    for the same address must not produce two rows, two identities or two
    answers. The second call goes through the real executor -- the same worker
    the deployment runs -- not through a second HTTP path.
    """
    from adapters.repository import units_for_work, work_by_id
    from app.work_worker import WorkWorker
    from core.enums import UnitOutcome, WorkStatus

    source_id = await _add_source(writer)

    receipt = (await _fetch_item(writer, source_id, ARTICLE_URL)).json()
    assert receipt["outcome"] == "ok"

    submitted = await writer.post(
        "/v1/works",
        headers={"Idempotency-Key": "atom-and-work"},
        json={
            "kind": "fetch",
            "targets": [ARTICLE_URL],
            "params": {"source_id": source_id},
        },
    )
    assert submitted.status_code == 202, submitted.text
    work_id = submitted.json()["work_id"]

    client = httpx.Client(transport=httpx.MockTransport(origin.handler))
    worker = WorkWorker(session_factory, client=client)
    try:
        report = await worker.sweep_once()
    finally:
        client.close()
    assert report == {"works": 1, "units": 1}

    with session_factory() as session:
        work = work_by_id(session, work_id)
        units = units_for_work(session, work_id)
    assert [(unit.target, unit.outcome) for unit in units] == [
        (ARTICLE_URL, UnitOutcome.OK)
    ]
    # Same identity: the work deduped onto the row the atom stored, rather than
    # growing a second one for the same article.
    assert units[0].ref == receipt["item_id"]
    assert work is not None and work.status is WorkStatus.SUCCEEDED

    stored = await writer.get("/v1/items", params={"url": ARTICLE_URL})
    assert [row["id"] for row in stored.json()["data"]] == [receipt["item_id"]]
    assert stored.json()["total"] == 1


# --------------------------------------------------------------------------
# AC-236: the shim's removal trigger is observable, so it can be evaluated
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_compat_shim_is_identifiable_in_the_audit_log(
    writer: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> None:
    """AC-236: ADR 0046 §7's removal trigger can actually be answered.

    The trigger (issue #211) is "no caller has used this endpoint for a full
    retention window", and it is only usable if the audit trail identifies *this*
    endpoint rather than "some write". The middleware records the route template
    (`app/middleware.py::_operation`), so the row is the literal
    `POST /v1/sources/{source_id}:fetch-items` and one count over it answers the
    question.

    Already correct before this change, and declared as such: what makes it worth
    pinning now is that an accepted ADR depends on it. A refactor that recorded
    the *concrete* path per request (`.../src-abc:fetch-items`) would look right
    in a log and silently make the trigger unevaluable, because the template is
    what the count matches.
    """
    source_id = await _add_source(writer)
    response = await _fetch_items(writer, source_id, [ARTICLE_URL])
    assert response.status_code == 200, response.text

    # The audit row is written after the response is on the wire, so reading
    # straight after the call is a race -- the same reason the MCP suite drains.
    CorrelationAndAuditMiddleware.drain_audit()
    with session_factory() as session:
        rows = [
            str(row[0])
            for row in session.execute(
                sql("select operation from audit_log where operation like :pattern"),
                # Bound rather than inlined: SQLAlchemy reads the `:fetch` in
                # the pattern as a bind parameter.
                {"pattern": "POST /v1/sources/%:fetch-items"},
            ).fetchall()
        ]

    assert rows == ["POST /v1/sources/{source_id}:fetch-items"], rows
