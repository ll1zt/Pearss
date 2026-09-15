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


def test_an_empty_stored_hash_never_authenticates() -> None:
    """A blank credential row is refused, and refused outright.

    This is a boundary, not a mutation kill. `not stored` is redundant with the
    six-field check that follows -- the only falsy strings are `""` and runs of
    `$`, and every one of them splits into fewer than six parts -- so the guard
    is defence in depth: it refuses for the right reason instead of relying on
    a later parse to notice. The test states the boundary so a future reordering
    of those checks cannot quietly turn a blank row into a parsed one.
    """
    assert not verify_password("hunter2", "")
    assert not verify_password("hunter2", "$")
    assert not verify_password("", hash_password("hunter2"))
    assert not verify_password("", "")


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
# The stored hash's on-disk format
#
# `hash_password`'s output is written to the credential row and read back by
# `verify_password` on every login, including across a database restore from
# backup. The property tests above cannot see the format: they generate a
# random string for `stored`, and a random string fails the length and scheme
# checks anyway, so a mutant that loosens those checks still passes them.
#
# That is the failure mode these pin. `verify_password` is fail-closed by
# design -- a hash it does not recognise returns False -- and the recognition
# test is exactly what a mutation turns into "accept". A loosened check would
# verify an arbitrary string as a valid credential.
# --------------------------------------------------------------------------

# A fixed salt makes the whole stored string deterministic, so the format is
# asserted literally rather than reconstructed.
_FIXED_SALT = bytes(range(16))


def test_the_stored_hash_format_is_pinned() -> None:
    """The literal wire format of a credential row.

    Self-describing on purpose (ADR 0005): the cost parameters travel with the
    hash so a future parameter change can be rolled out without guessing what
    an existing row used. If this string changes, every stored credential stops
    verifying -- so it is a migration, not an edit.
    """
    assert hash_password("hunter2", salt=_FIXED_SALT) == (
        "scrypt$16384$8$1$000102030405060708090a0b0c0d0e0f$"
        "c82fc216ae0ee83285a3ef4cf4dd5d754fe7d0bd00bcf9cc64be33ce199b4c0d"
    )


def test_a_hash_with_a_wrong_number_of_fields_is_refused() -> None:
    """Six fields, no more and no fewer.

    Each of these is a plausible corruption -- a truncated write, an extra
    separator -- and each must fail the login rather than be parsed as some
    prefix of a real credential.
    """
    real = hash_password("hunter2", salt=_FIXED_SALT)
    fields = real.split("$")
    assert len(fields) == 6

    for count in (0, 1, 4, 5, 7, 8):
        fields_of_count = fields[:count] if count <= 6 else [*fields, "x"]
        assert not verify_password("hunter2", "$".join(fields_of_count)), count


def test_a_hash_with_an_unknown_scheme_is_refused() -> None:
    """The scheme field is checked, not merely parsed around.

    A hash whose *contents* are a valid scrypt record but whose tag names
    another algorithm must not be accepted: the tag is what a future migration
    dispatches on, so ignoring it would make the field decorative.
    """
    real = hash_password("hunter2", salt=_FIXED_SALT)
    _, n, r, p, salt, digest = real.split("$")

    for scheme in ("", "bcrypt", "scryptX", "SCRYPT", "argon2id"):
        assert not verify_password("hunter2", f"{scheme}${n}${r}${p}${salt}${digest}")


def test_a_hash_with_a_non_hex_field_is_refused_rather_than_crashing() -> None:
    """`bytes.fromhex` raises on bad input; the caller must not see that.

    A credential row damaged in transit must fail the login, not 500 the
    request -- the password is attacker-influenced to the extent that anything
    reaching this function is, so an exception here is a crash an unauthenticated
    caller can trigger.
    """
    real = hash_password("hunter2", salt=_FIXED_SALT)
    _, n, r, p, salt, digest = real.split("$")

    assert not verify_password("hunter2", f"scrypt${n}${r}${p}$zzzz${digest}")
    assert not verify_password("hunter2", f"scrypt${n}${r}${p}${salt}$zzzz")
    assert not verify_password("hunter2", f"scrypt$notanint${r}${p}${salt}${digest}")


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
