"""Pure schedule decisions must not turn weekly failures into daily retries."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.models import SchedulePolicy
from core.scheduling import decide_schedule, next_interval

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("seconds", "failures", "expected"),
    [
        (604800, 0, 604800),
        (604800, 1, 604800),
        (604800, 19, 604800),
        (600, 1, 1200),
        (600, 50, 86400),
        (86399, 1, 86400),
        (86400, 1, 86400),
        (86401, 1, 86401),
    ],
)
def test_user_failed_fixed_source_respects_configured_cadence(
    seconds: int, failures: int, expected: int
) -> None:
    """Given a fixed cadence, when failures accrue, then cap without shortening."""
    assert (
        next_interval(
            SchedulePolicy(type="fixed", interval_seconds=seconds),
            consecutive_failures=failures,
        )
        == expected
    )


def test_user_weekly_source_is_not_due_the_day_after_failure() -> None:
    """Given a failed weekly fetch, when a day passes, then wait until week end."""
    decision = decide_schedule(
        SchedulePolicy(type="fixed", interval_seconds=604800),
        consecutive_failures=1,
        last_fetch_at=datetime(2026, 9, 1, tzinfo=UTC),
        now=datetime(2026, 9, 2, tzinfo=UTC),
    )
    assert decision.due is False
    assert decision.interval_seconds == 604800
    assert decision.next_due_at == datetime(2026, 9, 8, tzinfo=UTC)
