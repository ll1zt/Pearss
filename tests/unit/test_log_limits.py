"""Scenario register (normative source: SCENARIOS.md).

AC-291: the rate-limited logger lets one line per key through per window and
summarises the rest, at the level of the lines it summarises.
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
