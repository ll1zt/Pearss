"""Tests for the Reader API item id contract.

These encode what NetNewsWire actually does, read from its
ReaderAPICaller.swift rather than assumed:

    stream/items/ids  response -> Int(id)!                    (decimal, Int64)
    stream/items/contents req  -> tag:.../item/<hex>           (unpadded)
    edit-tag            req    -> tag:.../item/%.16llx         (zero padded)
    stream/items/contents resp -> Int(lastPathComponent, 16)   (hex)

Getting any of these wrong fails silently: the client shows the subscription
but no articles, because unparseable ids are dropped rather than reported.
"""

from __future__ import annotations

import pytest

from adapters.reader_api import (
    READER_ID_HEX_DIGITS,
    encode_item_id,
    parse_item_id_param,
    reader_id,
    reader_id_hex,
)

# A realistic internal id: 32 hex characters, 128 bits.
INTERNAL = "c33c81a851a1c76a77514213352d77af"


@pytest.mark.unit
def test_external_id_fits_a_signed_64_bit_int() -> None:
    # The whole reason for narrowing: Swift's Int is 64-bit, and a value above
    # 2**63-1 makes Int(id)! trap, which is what hid every article from NNW.
    external = reader_id(INTERNAL)
    assert 0 <= external < 2**63


@pytest.mark.unit
def test_external_id_is_deterministic() -> None:
    assert reader_id(INTERNAL) == reader_id(INTERNAL)


@pytest.mark.unit
def test_different_items_get_different_external_ids() -> None:
    other = "134d2a9eea63041e01cb8736c48b01e4"
    assert reader_id(INTERNAL) != reader_id(other)


@pytest.mark.unit
def test_external_id_round_trips_to_its_hex_prefix() -> None:
    # The reverse lookup depends on this: external id -> the leading hex
    # digits of the internal id.
    external = reader_id(INTERNAL)
    assert INTERNAL.startswith(reader_id_hex(external))
    assert len(reader_id_hex(external)) == READER_ID_HEX_DIGITS


@pytest.mark.unit
def test_ids_endpoint_form_is_decimal() -> None:
    # NNW parses these with Int(), so the wire form must be decimal digits.
    assert str(reader_id(INTERNAL)).isdigit()


@pytest.mark.unit
def test_contents_response_is_long_form_hex() -> None:
    encoded = encode_item_id(reader_id(INTERNAL))
    assert encoded.startswith("tag:google.com,2005:reader/item/")
    tail = encoded.rsplit("/", 1)[-1]
    # NNW reads it back with Int(part, radix: 16).
    assert int(tail, 16) == reader_id(INTERNAL)


@pytest.mark.unit
def test_parse_accepts_decimal_from_the_ids_endpoint() -> None:
    external = reader_id(INTERNAL)
    assert parse_item_id_param(str(external)) == external


@pytest.mark.unit
def test_parse_accepts_edit_tag_zero_padded_hex() -> None:
    external = reader_id(INTERNAL)
    # edit-tag sends String(format: "%.16llx", value).
    padded = f"tag:google.com,2005:reader/item/{external:016x}"
    assert parse_item_id_param(padded) == external


@pytest.mark.unit
def test_parse_accepts_contents_unpadded_hex() -> None:
    external = reader_id(INTERNAL)
    # contents sends String(value, radix: 16), which drops leading zeros.
    unpadded = f"tag:google.com,2005:reader/item/{external:x}"
    assert parse_item_id_param(unpadded) == external


@pytest.mark.unit
def test_padded_and_unpadded_forms_agree() -> None:
    external = reader_id(INTERNAL)
    padded = f"tag:google.com,2005:reader/item/{external:016x}"
    unpadded = f"tag:google.com,2005:reader/item/{external:x}"
    assert parse_item_id_param(padded) == parse_item_id_param(unpadded)


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not-an-id", "tag:google.com,2005:reader/item/", "zz"],
)
def test_unparseable_ids_return_none_rather_than_raising(raw: str) -> None:
    # One malformed id must not fail a batch that contains valid ones.
    assert parse_item_id_param(raw) is None


@pytest.mark.unit
def test_hex_that_looks_all_digits_is_still_read_as_hex() -> None:
    # 12345 in hex is "3039": every character is a digit, so a naive
    # isdigit() check after stripping the prefix would read it as decimal and
    # resolve to the wrong item. The prefix must decide the base.
    assert format(12345, "x") == "3039"
    long_form = f"tag:google.com,2005:reader/item/{12345:016x}"
    assert parse_item_id_param(long_form) == 12345
    # A bare value with no prefix is decimal, per the ids endpoint contract.
    assert parse_item_id_param("12345") == 12345


@pytest.mark.unit
def test_all_three_client_forms_survive_a_full_round_trip() -> None:
    external = reader_id(INTERNAL)
    forms = [
        str(external),
        f"tag:google.com,2005:reader/item/{external:016x}",
        f"tag:google.com,2005:reader/item/{external:x}",
    ]
    assert {parse_item_id_param(form) for form in forms} == {external}


@pytest.mark.unit
def test_contents_response_long_form_is_zero_padded_to_16() -> None:
    """ReadYou drops every article whose id is not exactly 16 hex digits.

    ReadYou's id validator is `startsWith(PREFIX) && removePrefix(PREFIX)
    .length == 16`, and its own comment table says "Long form needs
    0-padding". An unpadded id parses fine as a number, so NetNewsWire (which
    reads it back with Int(radix: 16)) is unaffected -- but ReadYou filters
    the whole contents response out, leaving the subscription visible and
    every article invisible, which is the failure the user reported.

    The first 15 hex digits of the internal id are the external id, so an
    internal id whose 15th digit is 0 produces a sub-16-digit tail whenever
    the 16th is dropped by `:x` -- the exact case observed on the deployed
    host: `.../item/6bb2584330b6d0d` (15 chars).
    """
    # An external id whose hex form is 15 digits: the padding is load-bearing.
    external = 0x6BB2584330B6D0D
    encoded = encode_item_id(external)
    tail = encoded.rsplit("/", 1)[-1]
    assert len(tail) == 16, f"{encoded} lost its padding"
    assert tail == f"{external:016x}"
    assert int(tail, 16) == external
