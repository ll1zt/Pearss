"""The schedule policy's JSON form: the domain's own wire format.

`schedule_policy_from_json` / `to_json` sit between two real callers: the API,
which receives a JSON body, and the scheduler, which reads the text column the
API wrote. The module docstring in `core/models.py` calls the shape domain
structure rather than a serialisation detail, and the reason is that the
scheduler trusts what it parses -- a policy that round-trips lossily schedules
work at the wrong time.

These are pure functions, so they are tested directly at the input/output
boundary (docs/guides/testing.md allows this where there is no I/O path to walk
through). Declaring them here also puts them inside the mutation gate: mutmut
selects `tests/unit` only, and the only other coverage these had was through
`tests/integration/test_scheduler.py`, which the mutation runner never loads.
"""

from __future__ import annotations

import json

import pytest

from core.models import (
    SchedulePolicy,
    schedule_policy_from_json,
    schedule_policy_to_json,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# The documented forms, parsed
# --------------------------------------------------------------------------


def test_a_fixed_policy_parses_from_its_documented_form() -> None:
    policy = schedule_policy_from_json('{"type": "fixed", "interval": "30m"}')

    assert policy.type == "fixed"
    assert policy.interval_seconds == 1800


def test_an_adaptive_policy_parses_from_its_documented_form() -> None:
    policy = schedule_policy_from_json(
        '{"type": "adaptive", "min": "1h", "max": "24h"}'
    )

    assert policy.type == "adaptive"
    assert policy.min_seconds == 3600
    assert policy.max_seconds == 86400


def test_an_already_decoded_mapping_is_accepted() -> None:
    """The API hands over a dict; the database hands over text.

    Both must go through this one function, or the two callers would validate
    differently -- which is exactly what the module docstring says the shared
    path exists to prevent.
    """
    from_text = schedule_policy_from_json('{"type": "fixed", "interval": "30m"}')
    from_mapping = schedule_policy_from_json({"type": "fixed", "interval": "30m"})

    assert from_text == from_mapping


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30s", 30),
        ("30m", 1800),
        ("24h", 86400),
        ("1d", 86400),
        ("1.5h", 5400),
        ("500ms", 0),  # truncated toward zero; refused below for being <= 0
    ],
)
def test_duration_suffixes_convert_at_the_documented_factor(
    text: str, expected: int
) -> None:
    if expected == 0:
        # Sub-second durations collapse to 0 and are refused as non-positive.
        with pytest.raises(ValueError):
            schedule_policy_from_json({"type": "fixed", "interval": text})
        return

    assert schedule_policy_from_json({"type": "fixed", "interval": text}) == (
        SchedulePolicy(type="fixed", interval_seconds=expected)
    )


def test_a_bare_integer_is_seconds() -> None:
    assert schedule_policy_from_json({"type": "fixed", "interval": 45}) == (
        SchedulePolicy(type="fixed", interval_seconds=45)
    )


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_non_object_policy_is_refused() -> None:
    for raw in ('"fixed"', "42", "[]", "null", "true"):
        with pytest.raises(ValueError):
            schedule_policy_from_json(raw)


def test_an_unknown_key_is_refused_rather_than_ignored() -> None:
    """Silently dropping a key would accept a typo as a working policy.

    `{"type":"fixed","intervall":"30m"}` would then parse as a fixed policy
    with no interval -- a source that never becomes due -- instead of the
    error the caller needs to see.
    """
    with pytest.raises(ValueError) as excinfo:
        schedule_policy_from_json({"type": "fixed", "interval": "30m", "typo": 1})

    assert "typo" in str(excinfo.value)


def test_a_missing_or_non_string_type_is_refused() -> None:
    malformed: list[dict[str, object]] = [
        {"interval": "30m"},
        {"type": 7},
        {"type": None},
    ]
    for raw in malformed:
        with pytest.raises(ValueError):
            schedule_policy_from_json(raw)


def test_an_unknown_type_is_refused() -> None:
    with pytest.raises(ValueError) as excinfo:
        schedule_policy_from_json({"type": "cron", "interval": "30m"})

    assert "cron" in str(excinfo.value)


def test_a_non_positive_duration_is_refused() -> None:
    """Zero and negative intervals are how a source silently stops collecting."""
    for value in (0, -1, "0m", "-30m", "0s"):
        with pytest.raises(ValueError):
            schedule_policy_from_json({"type": "fixed", "interval": value})


def test_a_boolean_is_not_a_duration() -> None:
    """`True` is an `int` in Python, so the isinstance order is load-bearing.

    Without the explicit bool check, `{"interval": true}` would parse as one
    second and schedule the source every second.
    """
    with pytest.raises(ValueError):
        schedule_policy_from_json({"type": "fixed", "interval": True})


def test_an_unrecognised_duration_is_refused() -> None:
    for value in ("30", "soon", "30 minutes", "m30"):
        with pytest.raises(ValueError):
            schedule_policy_from_json({"type": "fixed", "interval": value})


def test_a_non_string_non_integer_duration_is_refused() -> None:
    for value in (1.5, None, ["30m"], {"s": 1}):
        with pytest.raises(ValueError):
            schedule_policy_from_json({"type": "fixed", "interval": value})


# --------------------------------------------------------------------------
# The rendered form
# --------------------------------------------------------------------------


def test_a_fixed_policy_renders_in_the_documented_form() -> None:
    rendered = schedule_policy_to_json(
        SchedulePolicy(type="fixed", interval_seconds=1800)
    )

    assert json.loads(rendered) == {"type": "fixed", "interval": "30m"}


def test_an_adaptive_policy_renders_in_the_documented_form() -> None:
    rendered = schedule_policy_to_json(
        SchedulePolicy(type="adaptive", min_seconds=3600, max_seconds=86400)
    )

    assert json.loads(rendered) == {"type": "adaptive", "min": "1h", "max": "1d"}


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (86400, "1d"),
        (172800, "2d"),
        (3600, "1h"),
        (7200, "2h"),
        (1800, "30m"),
        (60, "1m"),
        (90, "90s"),  # not a whole minute, so it stays in seconds
        (1, "1s"),
        (45, "45s"),
    ],
)
def test_seconds_render_in_the_largest_exact_unit(seconds: int, expected: str) -> None:
    """The documented style, and the property that makes it safe:

    the rendered string must parse back to the same number of seconds, or a
    read-modify-write of a source's policy would drift the schedule.
    """
    assert (
        json.loads(
            schedule_policy_to_json(
                SchedulePolicy(type="fixed", interval_seconds=seconds)
            )
        )["interval"]
        == expected
    )

    assert schedule_policy_from_json(
        {"type": "fixed", "interval": expected}
    ) == SchedulePolicy(type="fixed", interval_seconds=seconds)


def test_a_rendered_policy_parses_back_unchanged() -> None:
    """Round-trip: what the API writes is what the scheduler reads.

    A lossy round trip is how a source's interval changes without anyone
    editing it.
    """
    for policy in (
        SchedulePolicy(type="fixed", interval_seconds=1800),
        SchedulePolicy(type="fixed", interval_seconds=86400),
        SchedulePolicy(type="adaptive", min_seconds=900, max_seconds=43200),
    ):
        assert schedule_policy_from_json(schedule_policy_to_json(policy)) == policy


def test_keys_are_rendered_in_sorted_order() -> None:
    """`sort_keys` is what makes the stored column comparable.

    Scheduling decisions are recorded in history and two recordings of the same
    policy must be byte-identical, or a re-save looks like a change. Comparing
    two renderings of the same policy does not test this -- both would use the
    same insertion order -- so the literal order is asserted. The adaptive
    payload is the one that shows it: insertion order is type, min, max, and
    sorted order is max, min, type.
    """
    rendered = schedule_policy_to_json(
        SchedulePolicy(type="adaptive", min_seconds=3600, max_seconds=86400)
    )

    assert rendered == '{"max": "1d", "min": "1h", "type": "adaptive"}'


def test_two_equal_policies_render_byte_identically() -> None:
    first = schedule_policy_to_json(
        SchedulePolicy(type="adaptive", min_seconds=60, max_seconds=120)
    )
    second = schedule_policy_to_json(
        SchedulePolicy(type="adaptive", min_seconds=60, max_seconds=120)
    )

    assert first == second
