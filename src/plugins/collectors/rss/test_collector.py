"""Fixture tests for the RSS/Atom collector.

These pin the external world inside CI (section 7.3). They never touch the
network: each case loads a recorded snapshot and asserts the drafts or the
ErrorClass it must produce.

The `changed` and `malformed` fixtures are the valuable ones. A site redesign
is the most likely way this system breaks quietly, and these two cases are
what turn that break into a failing test instead of corrupted output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapters.raw_payload import RawPayload
from core.enums import ErrorClass
from plugins.collectors.rss import collector as rss

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ID = "src-1"
FEED_URL = "https://blog.example.com/feed"


def payload(name: str) -> RawPayload:
    """A RawPayload loaded from a recorded snapshot."""
    body = (FIXTURES / name).read_bytes()
    return RawPayload(
        url=FEED_URL,
        status_code=200,
        body=body,
        source_id=SOURCE_ID,
        raw_fetch_id="raw-1",
    )


# --------------------------------------------------------------------------
# Valid feed
# --------------------------------------------------------------------------


@pytest.mark.fixture
def test_valid_feed_yields_one_draft_per_entry() -> None:
    drafts = rss.parse(payload("valid.xml"))
    assert len(drafts) == 3


@pytest.mark.fixture
def test_draft_carries_the_source_and_raw_fetch() -> None:
    drafts = rss.parse(payload("valid.xml"))
    assert all(d.source_id == SOURCE_ID for d in drafts)
    assert all(d.raw_fetch_id == "raw-1" for d in drafts)


@pytest.mark.fixture
def test_guid_is_preferred_as_external_id() -> None:
    first = rss.parse(payload("valid.xml"))[0]
    assert first.external_id == "urn:uuid:1a2b3c4d-0001-4000-8000-000000000001"


@pytest.mark.fixture
def test_published_date_is_parsed() -> None:
    first = rss.parse(payload("valid.xml"))[0]
    assert first.published_at == datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


@pytest.mark.fixture
def test_missing_date_stays_none() -> None:
    # Inventing a timestamp would misdate the article and reorder the feed.
    third = rss.parse(payload("valid.xml"))[2]
    assert third.published_at is None


@pytest.mark.fixture
def test_description_is_used_when_there_is_no_encoded_content() -> None:
    first = rss.parse(payload("valid.xml"))[0]
    assert "short summary" in first.content


@pytest.mark.fixture
def test_encoded_content_is_preferred_over_summary() -> None:
    second = rss.parse(payload("valid.xml"))[1]
    assert "longer body" in second.content


@pytest.mark.fixture
def test_author_and_tags_are_captured() -> None:
    first = rss.parse(payload("valid.xml"))[0]
    assert first.metadata["author"] == "author@example.com (Ada Example)"
    assert first.metadata["tags"] == "widgets,opinion"


@pytest.mark.fixture
def test_feed_title_is_recorded() -> None:
    drafts = rss.parse(payload("valid.xml"))
    assert all(d.metadata["feed_title"] == "Example Blog" for d in drafts)


@pytest.mark.fixture
def test_tracking_query_is_preserved_by_the_collector() -> None:
    # The collector records what the feed said; stripping tracking parameters
    # is canonicalisation's job, not the collector's.
    second = rss.parse(payload("valid.xml"))[1]
    assert second.canonical_url.endswith("?utm_source=feed")


# --------------------------------------------------------------------------
# Atom
# --------------------------------------------------------------------------


@pytest.mark.fixture
def test_atom_feed_parses() -> None:
    drafts = rss.parse(payload("valid_atom.xml"))
    assert len(drafts) == 2


@pytest.mark.fixture
def test_atom_alternate_link_is_used() -> None:
    drafts = rss.parse(payload("valid_atom.xml"))
    assert drafts[0].canonical_url == "https://blog.example.com/atom/one"


@pytest.mark.fixture
def test_atom_content_is_preferred_over_summary() -> None:
    drafts = rss.parse(payload("valid_atom.xml"))
    assert "Full content" in drafts[1].content


# --------------------------------------------------------------------------
# Empty and broken feeds
# --------------------------------------------------------------------------


@pytest.mark.fixture
def test_valid_feed_with_no_entries_is_not_an_error() -> None:
    # A feed before its first post is legitimate. Reporting this as a failure
    # would make every new source look broken.
    assert rss.parse(payload("empty.xml")) == []


@pytest.mark.fixture
def test_entries_without_links_are_structure_changed() -> None:
    # The document still parses as valid RSS; only the expected shape is gone.
    with pytest.raises(rss.CollectorError) as excinfo:
        rss.parse(payload("changed.xml"))
    assert excinfo.value.error_class is ErrorClass.STRUCTURE_CHANGED


@pytest.mark.fixture
def test_no_non_url_becomes_a_canonical_url() -> None:
    # Regression: a urn:uuid guid was accepted as the canonical URL, which
    # would have put a non-fetchable identifier into a feed as <link>.
    with pytest.raises(rss.CollectorError):
        rss.parse(payload("changed.xml"))


@pytest.mark.fixture
def test_truncated_feed_is_parse_not_silently_published() -> None:
    # Regression: feedparser salvages partial entries from truncated XML, so
    # the feed yielded a usable-looking draft instead of failing. Publishing
    # half-parsed content is worse than reporting the error.
    with pytest.raises(rss.CollectorError) as excinfo:
        rss.parse(payload("malformed.xml"))
    assert excinfo.value.error_class is ErrorClass.PARSE


@pytest.mark.fixture
def test_empty_payload_is_rejected() -> None:
    raw = RawPayload(url=FEED_URL, status_code=200, body=b"", source_id=SOURCE_ID)
    with pytest.raises(rss.CollectorError) as excinfo:
        rss.parse(raw)
    assert excinfo.value.error_class is ErrorClass.PARSE


@pytest.mark.fixture
def test_non_feed_xml_is_structure_changed() -> None:
    # Well-formed XML that is not a feed: parseable, but the shape is wrong.
    raw = RawPayload(
        url=FEED_URL,
        status_code=200,
        body=b'<?xml version="1.0"?><html><body>Not a feed</body></html>',
        source_id=SOURCE_ID,
    )
    with pytest.raises(rss.CollectorError) as excinfo:
        rss.parse(raw)
    assert excinfo.value.error_class is ErrorClass.STRUCTURE_CHANGED


# --------------------------------------------------------------------------
# URL matching
# --------------------------------------------------------------------------


@pytest.mark.fixture
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/feed", True),
        ("https://example.com/rss", True),
        ("https://example.com/atom.xml", True),
        ("https://example.com/feed.xml", True),
        ("https://example.com/2026/01/a-post", False),
    ],
)
def test_match_url_never_touches_the_network(url: str, expected: bool) -> None:
    # match_url runs during source selection, so it must be pure.
    assert rss.match_url(url) is expected
