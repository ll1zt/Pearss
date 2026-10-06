"""Scenario register (normative source: SCENARIOS.md, AC-327..331; issue #350).

`_parse_ts` reads the Reader `ts` parameter, which clients send either as
seconds or as microseconds. Google Reader's own clients send seconds; some
newer ones send microseconds. The two ranges do not overlap -- 1e12 seconds is
year 33658 and 1e12 microseconds is 2001 -- so the value itself names its unit
and no guess about the client is needed.

The defect this pins out: the unit used to be chosen by the *width of the
string* (`len(raw) >= 16`), which read a 15-digit microsecond value as seconds.
That value is out of `datetime`'s range, so it raised and the endpoint answered
500. The rule is now magnitude-based, which is a property of the value rather
than a property of how it happens to be written.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.reader.routes import _parse_ts


@pytest.mark.unit
def test_seconds_are_read_as_seconds() -> None:
    """AC-327: a plain Unix-seconds value is the instant it names."""
    assert _parse_ts("1700000000") == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


@pytest.mark.unit
def test_microseconds_are_read_as_microseconds() -> None:
    """AC-328: the microsecond form names the same instant as its seconds form."""
    assert _parse_ts("1700000000000000") == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=UTC
    )


@pytest.mark.unit
def test_the_unit_comes_from_magnitude_not_from_string_length() -> None:
    """AC-329: a 15-digit value is microseconds because it is > 1e12.

    Under the old length rule this value (15 digits, < 16) was read as
    seconds -- year 31690708 -- and `datetime.fromtimestamp` raised, turning a
    timestamp into a 500. The 16-digit case lands on the same reading, which is
    why string length could never have been the right question: both values say
    "microseconds" by magnitude. (As seconds they would be year 39 million,
    which no client can mean.)
    """
    assert _parse_ts("999999999999999") == datetime(2001, 9, 9, 1, 46, 39, tzinfo=UTC)
    assert _parse_ts("1234567890123456") == datetime(
        2009, 2, 13, 23, 31, 30, tzinfo=UTC
    )


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["not-a-ts", "12.5", "1e9", "1700000000s"])
def test_non_numeric_values_are_no_cutoff_rather_than_an_error(raw: str) -> None:
    """AC-330: an unreadable `ts` means "no cutoff", never a crash.

    The caller turns None into `now()`, so a malformed value degrades to the
    documented default instead of failing the write.
    """
    assert _parse_ts(raw) is None


@pytest.mark.unit
@pytest.mark.parametrize("raw", [None, "", "   ", "\t"])
def test_absent_or_blank_values_are_no_cutoff(raw: str | None) -> None:
    """AC-331: an absent or blank `ts` is the same "no cutoff" as a bad one."""
    assert _parse_ts(raw) is None
