"""Property tests for core's hash, id, and timestamp invariants.

These are the invariants AGENTS.md names explicitly, so they deserve proofs,
not examples:

- fixed-width timestamps sort lexicographically the same way they sort
  chronologically (every ORDER BY and keyset cursor in the project relies on
  this; `isoformat()` breaks it, which is why the single formatter exists)
- content_hash is a function of (title, prose) only: markup, whitespace and
  zero-width decoration must not defeat dedupe
- item identity is scoped by tenant and derives deterministically from
  (tenant, source, external_id), which is what makes re-fetch idempotent
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from adapters.timestamps import fixed_width_utc
from core.normalize import content_hash, derive_guid, derive_item_id, normalize_text

pytestmark = pytest.mark.unit

_TIMESTAMP_SOURCE = st.integers(min_value=0, max_value=4_102_444_800 * 1_000_000 - 1)


def _moment(micros: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)


# --------------------------------------------------------------------------
# fixed_width_utc: the ordering invariant
# --------------------------------------------------------------------------


@settings(max_examples=300)
@given(_TIMESTAMP_SOURCE)
def test_fixed_width_is_always_the_same_width(micros: int) -> None:
    """Six fractional digits and a trailing Z, for every representable value.

    `isoformat()` omits the fraction when microseconds are zero. If the
    formatter did that too, `...T00:00:00Z` would sort *after*
    `...T00:00:00.000001Z` ('Z' is 0x5A, '.' is 0x2E) -- exactly the
    chronological inversion this format exists to prevent.
    """
    formatted = fixed_width_utc(_moment(micros))

    assert len(formatted) == len("2026-06-01T12:00:00.000000Z")
    assert formatted.endswith("Z")
    assert formatted[19] == "."


@settings(max_examples=300)
@given(_TIMESTAMP_SOURCE, _TIMESTAMP_SOURCE)
def test_lexicographic_order_equals_chronological_order(a: int, b: int) -> None:
    """String comparison is time comparison, for every pair.

    This is the property the whole storage layer leans on: cursors and
    ORDER BYs compare timestamp *text*.
    """
    first, second = _moment(a), _moment(b)

    assert (fixed_width_utc(first) <= fixed_width_utc(second)) == (first <= second)


@settings(max_examples=200)
@given(_TIMESTAMP_SOURCE)
def test_fixed_width_round_trips(micros: int) -> None:
    moment = _moment(micros)

    formatted = fixed_width_utc(moment)
    parsed = datetime.strptime(formatted, "%Y-%m-%dT%H:%M:%S.%f%z")

    assert parsed == moment


# --------------------------------------------------------------------------
# content_hash: a function of prose, not presentation
# --------------------------------------------------------------------------


# Templates that wrap the prose exactly once; the tags must disappear under
# plain_text, leaving the identical normalized text.
_MARKUP = st.sampled_from(
    ["<p>{}</p>", "<div class='x'>{}</div>", "<b>{}</b>", "<span> {} </span>", "{}"]
)
_PROSE = st.text(
    alphabet="ab c\n\td\u200b\u00a0eé漢字-.,!?\"'()[]", max_size=120
).filter(lambda s: normalize_text(s) != "")


@settings(max_examples=200)
@given(_PROSE, _MARKUP)
def test_content_hash_ignores_markup(prose: str, template: str) -> None:
    """The same prose in different markup hashes the same.

    Cross-source dedupe exists precisely because two feeds carry one article
    with different markup; a hash that saw the tags would defeat it.
    """
    bare = content_hash(title="t", content=prose)
    marked_up = content_hash(title="t", content=template.format(prose))

    assert marked_up == bare


@settings(max_examples=200)
@given(_PROSE)
def test_content_hash_ignores_surrounding_whitespace_and_zero_width(
    prose: str,
) -> None:
    """Decoration around the prose is not content.

    Zero-width characters are stripped and whitespace runs collapse, so a
    body embellished with invisible characters dedupes against the clean one.
    """
    decorated = "\u200b\u200b " + prose + " \u00a0\n\t"

    assert content_hash(title="t", content=decorated) == content_hash(
        title="t", content=prose
    )


@settings(max_examples=200)
@given(_PROSE, _PROSE)
def test_content_hash_is_deterministic_and_sensitive(first: str, second: str) -> None:
    """Same input, same hash; different prose, different hash."""
    once = content_hash(title="t", content=first)
    twice = content_hash(title="t", content=first)
    assert once == twice

    if normalize_text(first) != normalize_text(second):
        assert content_hash(title="t", content=first) != content_hash(
            title="t", content=second
        )


# --------------------------------------------------------------------------
# item identity: tenant-scoped and deterministic
# --------------------------------------------------------------------------


_ID_PARTS = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=24
)


@settings(max_examples=200)
@given(_ID_PARTS, _ID_PARTS, _ID_PARTS)
def test_item_identity_is_deterministic(
    tenant: str, source: str, external: str
) -> None:
    first = derive_item_id(tenant_id=tenant, origin_id=source, external_id=external)
    second = derive_item_id(tenant_id=tenant, origin_id=source, external_id=external)

    assert first == second
    assert derive_guid(tenant_id=tenant, origin_id=source, external_id=external) == (
        derive_guid(tenant_id=tenant, origin_id=source, external_id=external)
    )


@settings(max_examples=200)
@given(_ID_PARTS, _ID_PARTS, _ID_PARTS, _ID_PARTS)
def test_a_different_tenant_is_a_different_item(
    tenant_a: str, tenant_b: str, source: str, external: str
) -> None:
    """Identity never leaks across tenants, however similar the rest."""
    if tenant_a == tenant_b:
        return

    assert derive_item_id(
        tenant_id=tenant_a, origin_id=source, external_id=external
    ) != derive_item_id(tenant_id=tenant_b, origin_id=source, external_id=external)


@settings(max_examples=100)
@given(_ID_PARTS, _ID_PARTS, _ID_PARTS)
def test_guid_and_item_id_are_distinct_namespaces(
    tenant: str, source: str, external: str
) -> None:
    """The guid is what leaves the system; the id is internal.

    If the two derivations ever collided, a Reader API client's view of
    identity and the storage key would be the same string -- and changing one
    scheme would silently change the other.
    """
    assert derive_guid(
        tenant_id=tenant, origin_id=source, external_id=external
    ) != derive_item_id(tenant_id=tenant, origin_id=source, external_id=external)
