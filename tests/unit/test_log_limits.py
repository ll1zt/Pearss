"""Scenario register (normative source: SCENARIOS.md).

AC-291: the rate-limited logger lets one line per key through per window and
summarises the rest, at the level of the lines it summarises.
AC-303: the bound on tracked keys is real -- past it the least recently seen key
is forgotten (which costs one line, never a lost one), and a bound of zero is a
limit rather than a crash.
"""

from __future__ import annotations

import logging

import pytest

from adapters.log_limits import rate_limited_logger

pytestmark = pytest.mark.unit

LOGGER = logging.getLogger("tests.log_limits")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "tests.log_limits"]


def test_one_line_per_window_and_a_summary_of_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    emit = rate_limited_logger(LOGGER, window_seconds=60, clock=clock)

    with caplog.at_level(logging.INFO, logger="tests.log_limits"):
        for _ in range(5):
            emit(logging.INFO, "fetch", "flaky fetch to %s", "host")
        emit.flush()

    messages = _messages(caplog)
    assert messages.count("flaky fetch to host") == 1
    assert [m for m in messages if "suppressed 4" in m] == [
        "fetch: suppressed 4 line(s) in the last 60s"
    ]


def test_a_new_window_logs_again_and_reports_the_gap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    emit = rate_limited_logger(LOGGER, window_seconds=60, clock=clock)

    with caplog.at_level(logging.INFO, logger="tests.log_limits"):
        emit(logging.INFO, "fetch", "line")
        emit(logging.INFO, "fetch", "line")
        clock.now = 61.0
        emit(logging.INFO, "fetch", "line")

    messages = _messages(caplog)
    assert messages.count("line") == 2
    assert any("suppressed 1" in message for message in messages)


def test_distinct_keys_do_not_suppress_each_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    emit = rate_limited_logger(LOGGER, window_seconds=60, clock=clock)

    with caplog.at_level(logging.INFO, logger="tests.log_limits"):
        emit(logging.INFO, "a", "line a")
        emit(logging.INFO, "b", "line b")
        emit.flush()

    messages = _messages(caplog)
    assert "line a" in messages
    assert "line b" in messages


def test_a_debug_summary_stays_debug(caplog: pytest.LogCaptureFixture) -> None:
    """A DEBUG-only path must not leak its summary into the default level."""
    clock = _Clock()
    emit = rate_limited_logger(LOGGER, window_seconds=60, clock=clock)

    with caplog.at_level(logging.INFO, logger="tests.log_limits"):
        emit(logging.DEBUG, "fetch", "debug line")
        emit(logging.DEBUG, "fetch", "debug line")
        emit.flush()

    assert _messages(caplog) == []


def test_the_bound_forgets_the_least_recent_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-303: past max_keys the oldest key is forgotten, which costs one line.

    The table exists to collapse a flood, so it must not grow with one. The
    price of forgetting a key is that its next line is logged rather than
    suppressed -- the side to err on.
    """
    clock = _Clock()
    emit = rate_limited_logger(LOGGER, window_seconds=60, clock=clock, max_keys=2)

    with caplog.at_level(logging.INFO, logger="tests.log_limits"):
        emit(logging.INFO, "a", "line a")
        emit(logging.INFO, "a", "line a")  # suppressed: "a" is in its window
        emit(logging.INFO, "b", "line b")
        emit(logging.INFO, "c", "line c")  # evicts the least recent key
        emit(logging.INFO, "a", "line a")  # forgotten, so it speaks again

    messages = _messages(caplog)
    assert messages.count("line a") == 2, messages


def test_a_key_in_use_is_not_the_one_evicted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-303: eviction is by least-recent use, or a busy key would flap.

    Every key here shares one frozen window start, so an eviction rule that
    compared window starts alone picked the first-inserted key -- the busy one
    -- and let a flood of one-off keys push it out repeatedly.
    """
    clock = _Clock()
    emit = rate_limited_logger(LOGGER, window_seconds=60, clock=clock, max_keys=2)

    with caplog.at_level(logging.INFO, logger="tests.log_limits"):
        emit(logging.INFO, "busy", "busy line")
        for index in range(5):
            emit(logging.INFO, "busy", "busy line")  # keeps "busy" recent
            emit(logging.INFO, f"flood-{index}", "flood line")
        emit.flush()

    messages = _messages(caplog)
    assert messages.count("busy line") == 1, messages
    assert any("busy: suppressed" in message for message in messages), messages


def test_a_bound_of_zero_is_a_limit_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-303: max_keys=0 raised ValueError, from min() over an empty table."""
    clock = _Clock()
    emit = rate_limited_logger(LOGGER, window_seconds=60, clock=clock, max_keys=0)

    with caplog.at_level(logging.INFO, logger="tests.log_limits"):
        emit(logging.INFO, "k", "line")
        emit(logging.INFO, "k", "line")

    assert _messages(caplog).count("line") == 1
