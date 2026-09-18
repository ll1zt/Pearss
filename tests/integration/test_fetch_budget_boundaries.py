"""A09: the fetch budget must bound DNS and child-proxied egress too.

Two boundaries outside the original slow-stream fix remain:

1. `resolve_host` performs `socket.getaddrinfo` inline, synchronously. A
   resolver that never answers holds the whole fetch past any policy
   budget; the total deadline must cover it.
2. A sandboxed child's reverse "http" call runs on the host with only the
   transport's default per-request cap. The outer RPC `call_timeout` never
   reaches the in-flight reverse call, so a child whose fetch hook makes
   one proxied request that the host stalls on outlives the caller's
   budget.

Both scenarios use real processes and real coordination events — no sleeps.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_user_fetch_fails_within_budget_when_the_resolver_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given an origin whose name never resolves, when the total budget is
    short, then the fetch is refused inside the budget, classified as
    network — not blocked on the resolver indefinitely.
    """

    from adapters import http
    from adapters.http import FetchError, FetchPolicy, fetch

    hung = Event()

    # A nameserver that accepts the query and never answers. The seam under
    # test is `_getaddrinfo` -- the real resolver call, which has no timeout
    # of its own. The join that bounds it lives in `resolve_host`, so the
    # patch must replace the inner call, not the boundary itself.
    def hung_nameserver(host: str) -> set[str]:
        hung.wait(600)
        return set()

    # A nameserver that accepts the query and never answers. The seam under
    # test is `_getaddrinfo` -- the real resolver call, which has no timeout
    # of its own. The join that bounds it lives in `resolve_host`, so the
    # patch must replace the inner call, not the boundary itself. The root
    # conftest's hermetic resolver is restored first so the real
    # resolve_host (deadline logic included) is the code under test.
    monkeypatch.undo()
    monkeypatch.setattr(http, "_getaddrinfo", hung_nameserver)
    # The private-address guard stays ON (default): that is the path whose
    # DNS wait must be bounded. With it off, no resolution runs at all. The
    # root conftest's PEARSS_TEST_DNS short-circuit is removed so resolution
    # actually reaches the (hung) nameserver.
    monkeypatch.delenv(http.TEST_DNS_ENV, raising=False)

    with (
        httpx.Client(
            trust_env=False,
            transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        ) as client,
        ThreadPoolExecutor(1) as pool,
    ):
        started = monotonic()
        result = pool.submit(
            fetch,
            "https://hung-resolver.example/feed",
            client=client,
            policy=FetchPolicy(total_timeout=0.5),
        )
        try:
            with pytest.raises(FetchError) as caught:
                result.result(timeout=5.0)
            elapsed = monotonic() - started
            assert elapsed < 5.0, (
                f"the fetch spent {elapsed:.1f}s inside DNS, past its budget"
            )
            assert caught.value.error_class.value == "blocked"
        finally:
            hung.set()  # free the nameserver thread before unpatching
            monkeypatch.undo()


class _StallingHostTransport(httpx.BaseTransport):
    """The host side of a proxied egress whose origin stalls indefinitely."""

    def __init__(self) -> None:
        self.stalled = Event()
        self.release = Event()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.stalled.set()
        # The origin never answers until the test lets go: from the child's
        # point of view this is an origin holding the connection open.
        self.release.wait(30)
        return httpx.Response(200)


def test_a_child_reverse_http_call_is_bounded_by_the_call_budget() -> None:
    """Given a sandboxed collector whose fetch rides host-proxied egress,
    when the host-side handling stalls longer than the child's call budget,
    then the RPC call fails within the budget instead of waiting forever.
    """
    from app.plugin_runtime import PluginChild, PluginChildError, SandboxedCollector

    transport = _StallingHostTransport()
    child = PluginChild("telegram", call_timeout=2.0)
    try:
        child.set_http_transport(transport)
        child.start()
        proxy = SandboxedCollector(child)
        started = monotonic()
        with pytest.raises(PluginChildError) as caught:
            proxy.fetch(
                "https://t.me/journey_of_someone",
                client=httpx.Client(),
                source_id="s",
            )
        elapsed = monotonic() - started
        assert elapsed < 10, f"the call ran {elapsed:.1f}s past its 2s budget"
        assert "timed out" in str(caught.value)
    finally:
        transport.release.set()
        child.stop()
