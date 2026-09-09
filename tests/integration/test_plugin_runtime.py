"""The sandbox runner: real child processes, no mocks.

These spawn actual `python -m plugins.collectors` children and speak the
frozen RPC protocol to them — the crash-recovery path in particular cannot
be tested against a stub, because what it exercises is a real process
dying and being respawned.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.plugin_runtime import PluginChild


@pytest.fixture
def child() -> Iterator[PluginChild]:
    sandbox = PluginChild("telegram")
    sandbox.start()
    yield sandbox
    sandbox.stop()


@pytest.mark.integration
def test_start_pings_and_the_child_serves_match_url(child: PluginChild) -> None:
    assert child.match_url("https://t.me/durov") is True
    assert child.match_url("https://example.com/feed") is False


@pytest.mark.integration
def test_a_killed_child_is_restarted_once_and_recovers(child: PluginChild) -> None:
    """The whole point of the sandbox: the child's death costs nothing."""
    assert child.match_url("https://t.me/durov") is True

    proc = child._proc
    assert proc is not None
    proc.kill()
    proc.wait()

    assert child.match_url("https://t.me/durov") is True
    new_proc = child._proc
    assert new_proc is not None and new_proc.pid != proc.pid


@pytest.mark.integration
def test_stop_is_idempotent(child: PluginChild) -> None:
    child.stop()
    child.stop()  # must not raise


@pytest.mark.integration
def test_start_is_idempotent(child: PluginChild) -> None:
    proc = child._proc
    assert proc is not None
    child.start()  # must not spawn a second child
    assert child._proc is proc


@pytest.mark.integration
def test_an_unknown_collector_fails_at_start() -> None:
    from app.plugin_runtime import PluginChildError

    sandbox = PluginChild("no-such-collector")
    with pytest.raises(PluginChildError):
        sandbox.start()


@pytest.mark.integration
def test_the_backfill_surface_round_trips_over_rpc(child: PluginChild) -> None:
    """The declared backward walk is callable, not just declarable.

    The registry's backfiller() narrowing needs before_url/oldest_cursor on
    the collector; over the sandbox these are RPC methods against the
    child's hooks object. A bool-only `backfill()` used to make the
    capability undetectably inert.
    """
    from app.plugin_runtime import SandboxedCollector

    proxy = SandboxedCollector(child)
    hooks = proxy.backfill()
    assert hooks is not None, "telegram declares the backward walk"

    page_url = hooks.before_url("https://t.me/s/durov", "1234")
    assert page_url is not None
    assert "before=1234" in page_url

    from core.models import ItemDraft

    def draft(external_id: str) -> ItemDraft:
        return ItemDraft(
            source_id="s1",
            external_id=external_id,
            title="n",
            canonical_url=f"https://t.me/durov/{external_id.removeprefix('telegram:')}",
            published_at=None,
            content="",
        )

    # Real telegram external ids are permalink-shaped; the cursor extractor
    # takes the trailing path segment.
    drafts = [draft("telegram:/durov/99"), draft("telegram:/durov/55")]
    cursor = hooks.oldest_cursor(drafts)
    assert cursor == "99", "the preview page lists ascending; the top one is oldest"


@pytest.mark.integration
def test_a_collector_without_a_walk_returns_none(child: PluginChild) -> None:
    from app.plugin_runtime import PluginChild, SandboxedCollector

    rss_child = PluginChild("rss")
    rss_child.start()
    try:
        assert SandboxedCollector(rss_child).backfill() is None
    finally:
        rss_child.stop()


@pytest.mark.integration
def test_a_child_that_freezes_mid_line_is_not_waited_on_forever() -> None:
    """The read timeout must bound a stalled write, not just silence.

    A child that answered a byte or two and then hung used to hold the
    host's per-plugin lock forever: the selector only guarded the first
    byte, and `readline` blocked past it.
    """
    import subprocess
    import sys
    import time

    frozen = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('{'); sys.stdout.flush(); "
            "import time; time.sleep(600)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    from app.plugin_runtime import PluginChild, PluginChildError

    child = PluginChild.__new__(PluginChild)
    child._collector_id = "frozen"
    child._env = None
    child._limits = None
    child._call_timeout = 1.0
    child._startup_timeout = 1.0
    child._http_transport = None
    import threading

    child._lock = threading.Lock()
    child._proc = frozen
    child._next_id = 0
    try:
        started = time.monotonic()
        try:
            child._roundtrip("ping", {}, timeout=1.0)
            raise AssertionError("a frozen child must not answer")
        except PluginChildError as exc:
            assert "timed out" in str(exc)
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"the read stalled {elapsed:.1f}s past the deadline"
    finally:
        child.stop()  # kills and reaps, closing the pipes


@pytest.mark.unit
def test_the_child_fetch_branch_forwards_conditional_params_verbatim() -> None:
    """The child's fetch branch forwards conditional params verbatim.

    The branch used to hardcode conditional=None. Zhihu's and zsxq's own
    hooks ignore conditional (signed APIs answer no 304), so the contract
    is pinned at the dispatcher: whatever the host sent, the collector's
    hook receives.
    """
    from adapters.raw_payload import ConditionalState, RawPayload
    from plugins.collectors import __main__ as entry

    seen: list[ConditionalState | None] = []
    real_collector_for = entry._collector_for

    class _StubCollector:
        @staticmethod
        def fetch(url, *, client, source_id, conditional=None):
            seen.append(conditional)
            return RawPayload(
                url=url, status_code=200, body=b"<rss/>", source_id=source_id
            )

    entry._collector_for = lambda collector_id: _StubCollector()
    try:
        result = entry.handle(
            "stub",
            "fetch",
            {
                "url": "https://example.com/feed",
                "source_id": "s9",
                "conditional": {"etag": '"v1"', "last_modified": "yesterday"},
            },
            host_http=None,
        )
    finally:
        entry._collector_for = real_collector_for

    assert result["raw"]["status_code"] == 200
    assert seen == [ConditionalState(etag='"v1"', last_modified="yesterday")], (
        "conditional state was dropped or reshaped on the way to the hook"
    )


@pytest.mark.integration
def test_telegram_fetch_rewrites_the_url_to_the_preview(child: PluginChild) -> None:
    """The collector must request t.me/s/<name>, not the bare t.me/<name>.

    The bare URL serves Telegram's app-install page, which carries no
    messages; the first real subscription answered it and parse classified
    the page as structure_changed. The rewrite now lives in a fetch hook,
    exercised here through the real child.
    """
    import httpx

    from adapters.raw_payload import RawPayload
    from app.plugin_runtime import SandboxedCollector
    from plugins.collectors.telegram.collector import parse as tg_parse

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        assert "/s/" in str(request.url), (
            f"the child requested {request.url}; the preview rewrite did not run"
        )
        return httpx.Response(
            200,
            content=b"<html><body><div class='tgme_widget_message' "
            b"data-post='chan/1'>post</div></body></html>",
            headers={"content-type": "text/html"},
        )

    child.set_http_transport(httpx.MockTransport(handler))
    proxy = SandboxedCollector(child)
    payload = proxy.fetch(
        "https://t.me/journey_of_someone",
        client=httpx.Client(),
        source_id="s-tg",
    )
    assert payload.ok
    assert seen_urls == ["https://t.me/s/journey_of_someone"]
    drafts = tg_parse(
        RawPayload(
            url="https://t.me/s/journey_of_someone",
            status_code=200,
            body=payload.body,
            source_id="s-tg",
        )
    )
    assert len(drafts) == 1
