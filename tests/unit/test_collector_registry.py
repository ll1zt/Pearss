"""The registry's fetch hook.

A collector whose origin needs signed or cookie-authenticated requests fetches
its own pages; the plain adapter cannot know how to sign one. The hook is
declared on the registration (`fetches=True`) rather than probed from the
module, so a collector that happens to define a `fetch` function does not
silently change collection behaviour.
"""

from __future__ import annotations

import httpx
import pytest

from adapters.raw_payload import ConditionalState, RawPayload
from app import collector_registry


def _payload() -> RawPayload:
    return RawPayload(url="https://example.com/x", status_code=200, body=b"body")


def _plain_payload() -> RawPayload:
    return RawPayload(url="https://example.com/plain", status_code=200, body=b"plain")


@pytest.mark.unit
def test_fetch_hook_is_declared_not_probed() -> None:
    # Existing registrations keep the plain path: a module defining a `fetch`
    # function later must not change collection without a registry decision.
    for name in ("telegram", "twitter", "rss"):
        registration = collector_registry.by_name(name)
        assert registration is not None
        assert registration.fetches is False
    assert collector_registry.by_name("twitter").paged is True


@pytest.mark.unit
def test_plain_registration_fetches_through_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = collector_registry.by_name("rss")
    assert registration is not None

    seen: dict[str, object] = {}

    def fake_fetch(url, *, client, source_id, conditional):
        seen["url"] = url
        seen["source_id"] = source_id
        seen["conditional"] = conditional
        return _plain_payload()

    monkeypatch.setattr(collector_registry, "plain_fetch", fake_fetch)
    conditional = ConditionalState(etag="e1")

    payload = collector_registry.fetch_page_with(
        registration,
        "https://example.com/feed.xml",
        client=httpx.Client(),
        source_id="src-1",
        conditional=conditional,
    )

    assert payload.body == b"plain"
    assert seen["url"] == "https://example.com/feed.xml"
    assert seen["source_id"] == "src-1"
    assert seen["conditional"] == conditional


@pytest.mark.unit
def test_fetching_registration_uses_its_own_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCollector:
        @staticmethod
        def match_url(url: str) -> bool:
            return url.startswith("https://signed.example")

        @staticmethod
        def fetch(url: str, *, client: httpx.Client, source_id: str) -> RawPayload:
            return RawPayload(url=url, status_code=200, body=f"signed:{source_id}".encode())

        @staticmethod
        def parse(raw: RawPayload) -> list:
            return []

    registration = collector_registry.Registration(
        name="fake", collector=FakeCollector(), fetches=True
    )

    def boom(*args: object, **kwargs: object) -> RawPayload:
        raise AssertionError("the plain adapter must not be called")

    monkeypatch.setattr(collector_registry, "plain_fetch", boom)

    payload = collector_registry.fetch_page_with(
        registration,
        "https://signed.example/page",
        client=httpx.Client(),
        source_id="src-9",
        conditional=ConditionalState(etag="ignored"),
    )

    assert payload.body == b"signed:src-9"
