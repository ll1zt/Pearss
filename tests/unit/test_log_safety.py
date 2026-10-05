"""Scenario register (normative source: SCENARIOS.md).

AC-296: request- and feed-derived text is escaped before it reaches a log
line, so a control character in an untrusted target cannot end the line and
start a forged one. The end-to-end half of AC-296 lives in
``tests/integration/test_request_logs.py`` (an unmatched path with a decoded
``%0A``); this is the pure transformation underneath it.
"""

from __future__ import annotations

import pytest

from adapters.log_safety import escape_control_characters

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A decoded %0A / %0D is the forging vector: it must not survive.
        ("/v1/a\n1970-01-01 ERROR forged", "/v1/a\\x0a1970-01-01 ERROR forged"),
        ("a\rb", "a\\x0db"),
        ("a\tb", "a\\x09b"),
        ("a\x00b", "a\\x00b"),
        ("a\x7fb", "a\\x7fb"),
        # Ordinary text is untouched -- a space, a colon and CJK are printable.
        ("/v1/sources/abc:fetch-items", "/v1/sources/abc:fetch-items"),
        ("a b 中文", "a b 中文"),
        ("", ""),
    ],
)
def test_control_characters_are_escaped_and_printables_survive(
    raw: str, expected: str
) -> None:
    assert escape_control_characters(raw) == expected


def test_the_escaped_result_is_a_single_line() -> None:
    """The property the access log depends on, stated directly."""
    assert "\n" not in escape_control_characters("a\nb\nc")
