"""Tests for correlation ids and audit redaction.

Both modules had real defects found only by testing: the inbound-id allowlist
was built with `set("...a-zA-Z...")`, which yields only the characters actually
present and so rejected most legitimate ids; and ASGI's byte headers were
passed to redaction unchanged, where a bytes key never equals a str entry in
the sensitive set — so Authorization slipped toward the audit log, and the row
could not be serialised at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters import correlation
from adapters.audit import (
    headers_from_scope,
    redact_args,
    redact_headers,
    redact_url,
)
from adapters.repository import timestamp

# --------------------------------------------------------------------------
# Correlation ids
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "trace-abc-123",
        "req_2026.01:001",
        "Zs8K9dQ2",
        "a" * correlation.MAX_INBOUND_LENGTH,
    ],
)
def test_ordinary_ids_are_accepted(value: str) -> None:
    assert correlation.sanitize_inbound(value) == value


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "has space",
        "has\r\nnewline",
        "inj\nection",
        "héllo",
        "<script>",
        "x" * (correlation.MAX_INBOUND_LENGTH + 1),
    ],
)
def test_unsafe_ids_are_rejected(value: str | None) -> None:
    # Rejected, not sanitised: a mangled id would look like it came from the
    # client while no longer being the client's.
    assert correlation.sanitize_inbound(value) is None


@pytest.mark.unit
def test_generated_ids_are_unique_and_hex() -> None:
    ids = {correlation.new_correlation_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(item) == 16 for item in ids)


@pytest.mark.unit
def test_current_outside_a_request_still_yields_an_id() -> None:
    # A scheduler run or a CLI call has no middleware to install an id, and
    # every recorded event still needs one to be traceable.
    assert correlation.current()


@pytest.mark.unit
def test_set_and_reset_scope_the_value() -> None:
    token = correlation.set_correlation_id("explicit-id")
    try:
        assert correlation.current() == "explicit-id"
    finally:
        correlation.reset(token)
    assert correlation.current() != "explicit-id"


# --------------------------------------------------------------------------
# Header redaction
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_authorization_is_redacted_but_still_visible_as_present() -> None:
    out = redact_headers({"authorization": "Bearer s3cr3t", "user-agent": "nnw"})
    assert out["authorization"] == "<redacted>"
    assert out["user-agent"] == "nnw"
    assert "s3cr3t" not in str(out)


@pytest.mark.unit
def test_sensitive_header_matching_is_case_insensitive() -> None:
    assert redact_headers({"Authorization": "x"})["Authorization"] == "<redacted>"


@pytest.mark.unit
def test_headers_from_scope_decodes_bytes_pairs() -> None:
    raw = [(b"authorization", b"Bearer abc"), (b"accept", b"application/json")]
    out = headers_from_scope(raw)
    assert out == {"authorization": "Bearer abc", "accept": "application/json"}
    # The point of decoding: the redaction allowlist matches on text keys.
    assert redact_headers(out)["authorization"] == "<redacted>"


# --------------------------------------------------------------------------
# Argument redaction
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    ["password", "token", "secret", "authorization", "cookie", "content", "body"],
)
def test_sensitive_arguments_are_dropped_entirely(key: str) -> None:
    # Truncating is not enough: a prefix of a credential is still a credential,
    # and a body prefix is still user content.
    assert redact_args({key: "super-sensitive-value"})[key] == "<redacted>"


@pytest.mark.unit
def test_ordinary_arguments_are_kept() -> None:
    assert redact_args({"source_id": "src-1"}) == {"source_id": "src-1"}


@pytest.mark.unit
def test_long_values_are_truncated_not_stored_whole() -> None:
    out = redact_args({"url": "https://example.com/" + "a" * 5000})
    assert len(out["url"]) < 300
    assert "chars)" in out["url"]


@pytest.mark.unit
def test_argument_count_is_bounded() -> None:
    out = redact_args({f"k{i}": i for i in range(200)})
    assert len(out) <= 25


@pytest.mark.unit
def test_non_string_values_are_summarised() -> None:
    assert redact_args({"n": 5, "flag": True}) == {"n": "5", "flag": "true"}


# --------------------------------------------------------------------------
# URL redaction
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_private_query_parameters_are_stripped() -> None:
    # Private feed URLs carry tokens in the query; the path identifies the
    # resource without the secret.
    out = redact_url("https://feeds.example/x?token=abc&feed=7")
    assert "abc" not in out
    assert "feed=7" in out


@pytest.mark.unit
def test_url_without_query_is_unchanged() -> None:
    assert redact_url("https://example.com/v1/items") == "https://example.com/v1/items"


@pytest.mark.unit
def test_query_of_only_secrets_leaves_no_marker() -> None:
    assert redact_url("https://example.com/f?token=x") == "https://example.com/f"


@pytest.mark.unit
def test_stored_timestamps_are_fixed_width() -> None:
    # Regression: sources_routes and the audit writer used isoformat(), which
    # omits the fractional part at microsecond zero and mixes widths inside a
    # column — the exact defect migration 0005 exists to repair.
    assert timestamp(datetime(2026, 9, 6, 10, 0, 0, tzinfo=UTC)) == (
        "2026-09-06T10:00:00.000000Z"
    )
    assert timestamp(datetime(2026, 9, 6, 10, 0, 0, 500000, tzinfo=UTC)) == (
        "2026-09-06T10:00:00.500000Z"
    )
