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
