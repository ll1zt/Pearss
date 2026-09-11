"""Property tests for Reader API credential and read-state domain logic.

These are pure functions with security obligations:

- `verify_password` must never raise on *any* stored string (a corrupt
  credential row fails the login; it must not 500 the request)
- `merge_state` is the only writer of read/starred flags; an absolute change
  applied twice must equal applying it once, or a replayed Reader API edit
  would toggle state
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.reader_state import (
    ReadState,
    TagChange,
    derive_auth_token,
    hash_password,
    merge_state,
    verify_password,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

_ANY_TEXT = st.text(max_size=80)
_ANY_STORED = st.text(max_size=120)


# --------------------------------------------------------------------------
# scrypt credentials
# --------------------------------------------------------------------------
# deadline=None: one scrypt evaluation costs ~200ms by design (ADR 0005
# chose those parameters), so hypothesis's default 200ms-per-example budget
# would flag honest work as a regression.


@settings(max_examples=30, deadline=None)
@given(_ANY_TEXT.filter(lambda s: s != ""))
def test_a_hashed_password_verifies_and_a_wrong_one_does_not(password: str) -> None:
    stored = hash_password(password)

    assert verify_password(password, stored)
    assert not verify_password(password + "x", stored)


@settings(max_examples=100, deadline=None)
@given(_ANY_TEXT, _ANY_STORED)
def test_verify_never_raises_on_any_input(password: str, stored: str) -> None:
    """Malformed or hostile stored hashes fail closed, silently."""
    assert verify_password(password, stored) in (True, False)


@settings(max_examples=15, deadline=None)
@given(_ANY_TEXT.filter(lambda s: s != ""))
def test_salting_changes_the_hash_but_not_the_verification(password: str) -> None:
    first = hash_password(password)
    second = hash_password(password)

    assert first != second, "a reused salt would make hashes comparable"
    assert verify_password(password, first) and verify_password(password, second)


@settings(max_examples=20, deadline=None)
@given(_ANY_TEXT.filter(lambda s: s != ""))
def test_the_token_binds_username_and_hash(username: str) -> None:
    """Rotating the password must invalidate every issued token."""
    hash_a = hash_password("pw-a")
    hash_b = hash_password("pw-b")

    token = derive_auth_token(username=username, password_hash=hash_a)
    assert token == derive_auth_token(username=username, password_hash=hash_a)
    assert token != derive_auth_token(username=username, password_hash=hash_b)


# --------------------------------------------------------------------------
# read-state merging
# --------------------------------------------------------------------------


@settings(max_examples=100)
@given(st.booleans(), st.booleans())
def test_an_absolute_change_applied_twice_equals_once(
    is_read: bool, is_starred: bool
) -> None:
    """Reader API replays the same edit on reconnect; it must be idempotent."""
    change = TagChange(is_read=is_read, is_starred=is_starred)

    once = merge_state(item_id="i", current=None, change=change, at=_NOW)
    twice = merge_state(item_id="i", current=once, change=change, at=_NOW)

    assert twice == once


@settings(max_examples=100)
@given(st.booleans(), st.booleans(), st.booleans())
def test_unset_fields_preserve_the_current_flags(
    current_read: bool, current_starred: bool, new_read: bool
) -> None:
    current = ReadState(item_id="i", is_read=current_read, is_starred=current_starred)

    merged = merge_state(
        item_id="i",
        current=current,
        change=TagChange(is_read=new_read, is_starred=None),
        at=_NOW,
    )

    assert merged.is_read == new_read
    assert merged.is_starred == current_starred


@settings(max_examples=50)
@given(st.booleans(), st.booleans())
def test_a_change_on_no_current_row_coerces_to_false(
    is_read: bool | None, is_starred: bool | None
) -> None:
    """A first-ever edit creates the row; absent flags mean 'not set', not 'unknown'."""
    merged = merge_state(
        item_id="i",
        current=None,
        change=TagChange(is_read=is_read, is_starred=is_starred),
        at=_NOW,
    )

    assert merged.is_read is bool(is_read)
    assert merged.is_starred is bool(is_starred)
    assert merged.updated_at == _NOW
