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
