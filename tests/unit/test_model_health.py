"""The model-health registry: prefer, demote, escalate, recover.

The state is in memory on purpose (ADR 0048): a restart forgetting which model
was misbehaving costs one extra failed attempt, while a table would be a second
fact source beside the provider's own behaviour. Time is injected, so the
cooldown arithmetic is asserted exactly instead of waited out.

A row reports per model `(model, asked, failed, last_error_class)` -- how many
requests it sent that model and how many failed -- and the registry folds the
row by its failure share (ADR 0056, issue #268). The half-failure cases below
are the ones the old "answered anything is healthy" fold got wrong.
"""

from __future__ import annotations

import threading

import pytest

from app.model_health import HEALTH_WINDOW_SAMPLES, ModelHealth

pytestmark = pytest.mark.unit


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cooling_span(health: ModelHealth, clock: _Clock, model: str) -> float:
    """How long `model` stays excluded, measured in whole seconds."""
    started = clock.now
    for _ in range(10_000):
        if model not in health.policy()["skip"]:
            return clock.now - started
        clock.advance(1.0)
    raise AssertionError(f"{model} never came back")


def test_a_healthy_process_prefers_the_configured_order() -> None:
    """AC-225: nothing is excluded while nothing has failed."""
    assert ModelHealth(clock=_Clock()).policy() == {
        "skip": [],
        "attempts_per_model": 2,
    }


def test_a_failed_model_is_skipped_for_five_minutes() -> None:
    """AC-227: the first cooldown is the floor, and recovery is automatic."""
    clock = _Clock()
    health = ModelHealth(clock=clock)
    health.record([("primary", 1, 1, "network")])
    assert health.policy()["skip"] == ["primary"]
    clock.advance(299.0)
    assert health.policy()["skip"] == ["primary"]
    clock.advance(2.0)
    assert health.policy()["skip"] == [], "the model is preferred again by itself"


def test_each_returning_episode_doubles_the_cooldown_up_to_hour() -> None:
    """AC-227: 5 -> 10 -> 20 -> 40 -> 60 (capped) minutes."""
    clock = _Clock()
    health = ModelHealth(clock=clock)
    spans = []
    for _ in range(6):
        health.record([("primary", 1, 1, "network")])
        spans.append(_cooling_span(health, clock, "primary"))
    assert spans == [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0]


def test_a_clean_row_starts_from_the_floor() -> None:
    """AC-228, AC-257: a row the model answers in full resets the escalation."""
    clock = _Clock()
    health = ModelHealth(clock=clock)
    health.record([("primary", 1, 1, "network")])
    health.record([("primary", 1, 1, "network")])
    assert _cooling_span(health, clock, "primary") == 600.0

    health.record([("primary", 1, 0, None)])
    assert health.policy()["skip"] == [], "the model that answered is healthy"

    health.record([("primary", 1, 1, "network")])
    assert _cooling_span(health, clock, "primary") == 300.0, "five again, not twenty"


def test_a_chronic_partial_failure_escalates_instead_of_resetting() -> None:
    """AC-256: failing *some* asks every row must not look like recovery.

    The defect (#268): the row report was folded to one boolean per model, so a
    model that answered any block was reported healthy and its escalation was
    cleared every row. A model that timed out on part of every document stayed
    at the five-minute floor forever and was asked first again and again -- 114
    of 132 live fallbacks in the motivating journal came from that one model.

    Two of three asks failing is a degraded row, and three in a row are
    5 -> 10 -> 20 minutes rather than the floor each time.
    """
    clock = _Clock()
    health = ModelHealth(clock=clock)
    spans = []
    for _ in range(3):
        health.record([("primary", 3, 2, "network"), ("fallback", 3, 0, None)])
        spans.append(_cooling_span(health, clock, "primary"))

    assert spans == [300.0, 600.0, 1200.0], "the escalation survived every row"


def test_a_model_that_never_answers_escalates_to_the_ceiling() -> None:
    """AC-258: monotonic to the cap, and it holds there."""
    clock = _Clock()
    health = ModelHealth(clock=clock)
    spans = []
    for _ in range(7):
        health.record([("primary", 2, 2, "network")])
        spans.append(_cooling_span(health, clock, "primary"))

    assert spans == [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0, 3600.0]


def test_one_bad_ask_among_several_is_left_in_the_chain() -> None:
    """AC-256 (the other half): a blip is not a degradation.

    One failed block out of three is a fact about that block, not about the
    model, so the ratio gate leaves the chain order alone -- no cooldown at all,
    which is the "偶发抖动" half of the proportional rule (ADR 0056).
    """
    clock = _Clock()
    health = ModelHealth(clock=clock)

    health.record([("primary", 3, 1, "network")])

    assert health.policy()["skip"] == [], "one ask in three is not a degradation"


def test_only_the_model_that_failed_cools_down() -> None:
    """AC-227: the fallback answering does not excuse the primary's failure."""
    health = ModelHealth(clock=_Clock())
    health.record([("primary", 1, 1, "blocked"), ("fallback", 1, 0, None)])
    assert health.policy()["skip"] == ["primary"]


def test_a_model_the_row_never_asked_is_not_reported() -> None:
    """`asked == 0` is not a zero-failure sample; it is no sample at all."""
    health = ModelHealth(clock=_Clock())
    health.record([("primary", 0, 0, None)])
    assert health.policy()["skip"] == []
    assert health.snapshot()["preferred"] is None, "nothing has been asked yet"


def test_the_row_budget_and_cooldowns_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-226: the operator can widen the budget without a code change."""
    monkeypatch.setenv("PEARSS_MODEL_ATTEMPTS_PER_MODEL", "3")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_MAX_SECONDS", "120")
    clock = _Clock()
    health = ModelHealth.from_env(clock=clock)
    assert health.policy()["attempts_per_model"] == 3
    health.record([("primary", 1, 1, "network")])
    assert _cooling_span(health, clock, "primary") == 60.0


def test_the_failure_ratio_gate_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment may demand more evidence before demoting a model."""
    monkeypatch.setenv("PEARSS_MODEL_FAILURE_RATIO", "1")
    clock = _Clock()
    health = ModelHealth.from_env(clock=clock)
    health.record([("primary", 3, 2, "network")])

    assert health.policy()["skip"] == [], "0.67 is below a gate of 1.0"


@pytest.mark.parametrize("value", ["0", "-0.5", "1.5", "often"])
def test_a_ratio_outside_the_range_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """0 would demote every model that failed once; >1 would demote none.

    Both would silently turn the knob into a different feature, so out of range
    is treated like unparseable.
    """
    monkeypatch.setenv("PEARSS_MODEL_FAILURE_RATIO", value)
    health = ModelHealth.from_env(clock=_Clock())

    health.record([("primary", 3, 2, "network")])

    assert health.policy()["skip"] == ["primary"], "the default 0.5 gate applies"


def test_the_ceiling_bounds_the_escalation_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_MAX_SECONDS", "90")
    clock = _Clock()
    health = ModelHealth.from_env(clock=clock)
    spans = []
    for _ in range(3):
        health.record([("primary", 1, 1, "network")])
        spans.append(_cooling_span(health, clock, "primary"))
    assert spans == [60.0, 90.0, 90.0]


def test_a_zero_cooldown_keeps_every_model_in_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment may choose retries over waiting: 0 means "never cool down"."""
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "0")
    health = ModelHealth.from_env(clock=_Clock())
    health.record([("primary", 1, 1, "network")])
    assert health.policy()["skip"] == []


def test_an_unparseable_knob_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PEARSS_MODEL_ATTEMPTS_PER_MODEL", "many")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "soon")
    health = ModelHealth.from_env(clock=_Clock())
    assert health.policy()["attempts_per_model"] == 2
    health.record([("primary", 1, 1, "network")])
    assert health.policy()["skip"] == ["primary"], "the default cooldown applies"


def test_rows_running_in_parallel_do_not_lose_each_other_failures() -> None:
    """The worker runs several rows at once; the registry is shared state.

    Two models failing in two threads must both end up excluded: a lost write
    here is a model that keeps being preferred after it broke.
    """
    started = threading.Barrier(2)

    health = ModelHealth(clock=_Clock())

    def fail(model: str) -> None:
        started.wait(5)
        health.record([(model, 1, 1, "network")])

    threads = [
        threading.Thread(target=fail, args=(model,))
        for model in ("primary", "fallback")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert sorted(health.policy()["skip"]) == ["fallback", "primary"]


def test_the_window_shows_a_partially_failing_model_that_is_not_cooling() -> None:
    """AC-259: the half-broken model the `models` cell cannot show.

    One failed ask of three is below the demotion gate (ADR 0056), so this model
    is not cooling and appears nowhere under `models` -- yet the failure has to
    be visible, or the diagnostic surface answers "healthy" while the journal
    fills with fallbacks. That is exactly the live shape of #269: 132 fallbacks
    in ten hours, 12 cooldowns, and a `model_health` cell that read completely
    healthy.
    """
    clock = _Clock()
    health = ModelHealth(clock=clock)
    health.record([("primary", 3, 1, "network"), ("secondary", 3, 0, None)])

    snapshot = health.snapshot()

    assert snapshot["models"] == {}, "a blip is not a cooldown"
    assert snapshot["skipped"] == []
    assert snapshot["window"] == {
        "primary": {
            "attempts": 3,
            "failures": 1,
            "fallbacks": 1,
            "failure_rate": 0.333,
            "last_failure_class": "network",
            "since_last_failure_seconds": 0.0,
        }
    }
    assert "secondary" not in snapshot["window"], "a model that never failed is noise"


def test_the_window_folds_the_last_samples_only() -> None:
    """AC-260: bounded -- an old failure ages out instead of reporting forever."""
    health = ModelHealth(clock=_Clock())
    health.record([("primary", 3, 1, "network")])
    for _ in range(HEALTH_WINDOW_SAMPLES):
        health.record([("primary", 3, 0, None)])

    assert health.snapshot()["window"] == {}, "the failure left the window"


def test_a_cooling_model_still_appears_in_both_cells() -> None:
    """AC-261: the new cell must not replace the old one (no #231 regression)."""
    clock = _Clock()
    health = ModelHealth(clock=clock)
    health.record([("primary", 2, 2, "network")])
    clock.advance(60.0)

    snapshot = health.snapshot()

    assert snapshot["preferred"] is None, "the only known model is cooling"
    assert snapshot["skipped"] == ["primary"]
    assert snapshot["models"]["primary"]["cooling_seconds"] == 240.0
    assert snapshot["window"]["primary"] == {
        "attempts": 2,
        "failures": 2,
        "fallbacks": 1,
        "failure_rate": 1.0,
        "last_failure_class": "network",
        "since_last_failure_seconds": 60.0,
    }


def test_the_snapshot_reports_the_cooling_episode_and_what_is_preferred() -> None:
    """AC-233: the cell `:diagnose` projects, asserted exactly.

    Only what is currently cooling appears under `models`: a model that answered
    is healthy, and reporting a cleared episode would send an operator after a
    failure that is over.
    """
    clock = _Clock()
    health = ModelHealth(clock=clock)
    health.record([("primary", 1, 1, "network"), ("secondary", 1, 0, None)])
    clock.advance(60.0)

    snapshot = health.snapshot()

    assert snapshot["preferred"] == "secondary", "the first model that is not cooling"
    assert snapshot["skipped"] == ["primary"]
    assert snapshot["attempts_per_model"] == 2
    assert snapshot["models"] == {
        "primary": {
            "cooling_seconds": 240.0,
            "cooldown_seconds": 300.0,
            "failures": 1,
            "last_failure_class": "network",
            "since_last_failure_seconds": 60.0,
        }
    }


def test_a_recovered_model_leaves_the_snapshot() -> None:
    """A cleared episode must not look like a live one."""
    health = ModelHealth(clock=_Clock())
    health.record([("primary", 1, 1, "network")])
    health.record([("primary", 1, 0, None)])

    assert health.snapshot()["models"] == {}
    assert health.snapshot()["preferred"] == "primary"
