"""The registry's fetch hook.

A collector whose origin needs signed or cookie-authenticated requests — or,
for the rss collector, one that upgrades WordPress summary feeds to full text
— fetches its own pages. The hook is declared on the registration
(`fetches=True`) rather than probed from the module, so a collector that
happens to define a `fetch` function does not silently change collection
behaviour.
"""

from __future__ import annotations

import httpx
import pytest

from adapters.raw_payload import ConditionalState, RawPayload
from app import collector_registry
from core.models import ItemDraft
from plugins.collectors.rss import collector as rss_collector


def _payload() -> RawPayload:
    return RawPayload(url="https://example.com/x", status_code=200, body=b"body")


def _plain_payload() -> RawPayload:
    return RawPayload(url="https://example.com/plain", status_code=200, body=b"plain")


@pytest.mark.unit
def test_fetch_hook_is_declared_not_probed() -> None:
    # telegram and twitter keep the plain path: a module defining a `fetch`
    # function later must not change collection without a registry decision.
    for name in ("telegram", "twitter"):
        registration = collector_registry.by_name(name)
        assert registration is not None
        assert registration.fetches is False
    twitter = collector_registry.by_name("twitter")
    assert twitter is not None and twitter.paged is True


@pytest.mark.unit
def test_zhihu_declares_its_own_fetch() -> None:
    # The signed Zhihu API is why the hook exists; the registration records
    # that decision explicitly rather than probing the module.
    registration = collector_registry.by_name("zhihu")
    assert registration is not None
    assert registration.fetches is True
    assert registration.paged is False


@pytest.mark.unit
def test_zsxq_declares_its_own_fetch() -> None:
    # The cookie-authenticated zsxq API goes through the same hook.
    registration = collector_registry.by_name("zsxq")
    assert registration is not None
    assert registration.fetches is True
    assert registration.paged is False


@pytest.mark.unit
def test_rss_declares_its_own_fetch() -> None:
    # The WordPress fulltext upgrade runs through the hook; the plain path
    # still lives inside it (conditional-state replay included).
    registration = collector_registry.by_name("rss")
    assert registration is not None
    assert registration.fetches is True
    assert registration.paged is False


@pytest.mark.unit
def test_rss_hook_starts_from_the_plain_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rss hook fetches the feed plainly first — that is where ETag
    replay lives — and only upgrades on a detected summary feed."""
    seen: dict[str, object] = {}

    def fake_plain_fetch(
        url: str,
        *,
        client: httpx.Client,
        source_id: str,
        conditional: ConditionalState | None = None,
    ) -> RawPayload:
        seen["url"] = url
        seen["source_id"] = source_id
        seen["conditional"] = conditional
        # A plain, non-WordPress payload: no upgrade path triggers.
        return _plain_payload()

    monkeypatch.setattr(rss_collector, "plain_fetch", fake_plain_fetch)

    registration = collector_registry.by_name("rss")
    assert registration is not None
    payload = collector_registry.fetch_page_with(
        registration,
        "https://example.com/feed.xml",
        client=httpx.Client(),
        source_id="src-1",
        conditional=ConditionalState(etag="e1"),
    )

    assert payload.body == b"plain"
    assert seen == {
        "url": "https://example.com/feed.xml",
        "source_id": "src-1",
        "conditional": ConditionalState(etag="e1"),
    }


@pytest.mark.unit
def test_fetching_registration_uses_its_own_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCollector:
        @staticmethod
        def match_url(url: str) -> bool:
            return url.startswith("https://signed.example")

        @staticmethod
        def fetch(
            url: str,
            *,
            client: httpx.Client,
            source_id: str,
            conditional: ConditionalState | None = None,
        ) -> RawPayload:
            body = f"signed:{source_id}".encode()
            return RawPayload(url=url, status_code=200, body=body)

        @staticmethod
        def parse(raw: RawPayload) -> list[ItemDraft]:
            return []

    registration = collector_registry.Registration(
        name="fake",
        collector=FakeCollector(),
        fetches=True,
        manifest=collector_registry.Manifest(
            id="fake",
            version="0.1.0",
            name="Fake",
            description="test",
            example_url="https://signed.example/page",
            features=frozenset({"fetch"}),
            required_config=(),
        ),
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
