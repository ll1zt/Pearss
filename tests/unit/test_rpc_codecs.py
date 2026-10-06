"""The RPC wire codecs (P2b): field fidelity across the host/child boundary.

The codecs are the contract between two processes that never share object
identity, so what these tests pin is reconstruction, not representation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.models import ItemDraft


@pytest.mark.unit
def test_a_naive_published_at_stays_naive_across_the_rpc() -> None:
    """A zone-less draft must not gain a zone, or shift, over the wire.

    The ingest boundary deliberately records naive timestamps as evidence
    that the origin omitted the zone (ADR 0007's sanitize philosophy: record
    what arrived). Serialising through a local-zone astimezone would both
    attach UTC and shift the value by the host's offset - silently
    disagreeing with the in-process path.
    """
    naive = datetime(2026, 6, 1, 11, 0, 0)
    draft = ItemDraft(
        origin_id="s1",
        external_id="x:1",
        title="t",
        canonical_url="https://example.com/1",
        published_at=naive,
        content="",
    )

    rebuilt = ItemDraft.from_rpc_dict(draft.as_rpc_dict())

    assert rebuilt.published_at is not None
    assert rebuilt.published_at.tzinfo is None
    assert rebuilt.published_at == naive


@pytest.mark.unit
def test_an_aware_published_at_keeps_its_instant_across_the_rpc() -> None:
    aware = datetime(2026, 6, 1, 11, 0, 0, tzinfo=UTC)
    draft = ItemDraft(
        origin_id="s1",
        external_id="x:2",
        title="t",
        canonical_url="https://example.com/2",
        published_at=aware,
        content="",
    )

    rebuilt = ItemDraft.from_rpc_dict(draft.as_rpc_dict())

    assert rebuilt.published_at is not None
    assert rebuilt.published_at.utcoffset() == timedelta(0)
    assert rebuilt.published_at == aware
