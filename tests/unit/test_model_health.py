"""The model-health registry: prefer, demote, escalate, recover.

The state is in memory on purpose (ADR 0048): a restart forgetting which model
was misbehaving costs one extra failed attempt, while a table would be a second
fact source beside the provider's own behaviour. Time is injected, so the
cooldown arithmetic is asserted exactly instead of waited out.
"""

from __future__ import annotations

import threading

import pytest

from app.model_health import ModelHealth

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
    """AC-224: nothing is excluded while nothing has failed."""
    assert ModelHealth(clock=_Clock()).policy() == {
        "skip": [],
        "attempts_per_model": 2,
    }


def test_a_failed_model_is_skipped_for_five_minutes() -> None:
    """AC-226: the first cooldown is the floor, and recovery is automatic."""
    clock = _Clock()
    health = ModelHealth(clock=clock)
    health.record([("primary", "network")])
    assert health.policy()["skip"] == ["primary"]
    clock.advance(299.0)
    assert health.policy()["skip"] == ["primary"]
    clock.advance(2.0)
    assert health.policy()["skip"] == [], "the model is preferred again by itself"


def test_each_returning_episode_doubles_the_cooldown_up_to_hour() -> None:
    """AC-226: 5 -> 10 -> 20 -> 40 -> 60 (capped) minutes."""
    clock = _Clock()
    health = ModelHealth(clock=clock)
    spans = []
    for _ in range(6):
        health.record([("primary", "network")])
        spans.append(_cooling_span(health, clock, "primary"))
    assert spans == [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0]


def test_a_model_that_answers_again_starts_from_the_floor() -> None:
    """AC-227: a success resets the escalation, not merely the cooldown."""
    clock = _Clock()
    health = ModelHealth(clock=clock)
    health.record([("primary", "network")])
    health.record([("primary", "network")])
    assert _cooling_span(health, clock, "primary") == 600.0

    health.record([("primary", None)])
    assert health.policy()["skip"] == [], "the model that answered is healthy"

    health.record([("primary", "network")])
    assert _cooling_span(health, clock, "primary") == 300.0, "five again, not twenty"


def test_only_the_model_that_failed_cools_down() -> None:
    """AC-226: the fallback answering does not excuse the primary's failure."""
    health = ModelHealth(clock=_Clock())
    health.record([("primary", "blocked"), ("fallback", None)])
    assert health.policy()["skip"] == ["primary"]


def test_the_row_budget_and_cooldowns_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-225: the operator can widen the budget without a code change."""
    monkeypatch.setenv("PEARSS_MODEL_ATTEMPTS_PER_MODEL", "3")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_MAX_SECONDS", "120")
    clock = _Clock()
    health = ModelHealth.from_env(clock=clock)
    assert health.policy()["attempts_per_model"] == 3
    health.record([("primary", "network")])
    assert _cooling_span(health, clock, "primary") == 60.0


def test_the_ceiling_bounds_the_escalation_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_MAX_SECONDS", "90")
    clock = _Clock()
    health = ModelHealth.from_env(clock=clock)
    spans = []
    for _ in range(3):
        health.record([("primary", "network")])
        spans.append(_cooling_span(health, clock, "primary"))
    assert spans == [60.0, 90.0, 90.0]


def test_a_zero_cooldown_keeps_every_model_in_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment may choose retries over waiting: 0 means "never cool down"."""
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "0")
    health = ModelHealth.from_env(clock=_Clock())
    health.record([("primary", "network")])
    assert health.policy()["skip"] == []


def test_an_unparseable_knob_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PEARSS_MODEL_ATTEMPTS_PER_MODEL", "many")
    monkeypatch.setenv("PEARSS_MODEL_COOLDOWN_SECONDS", "soon")
    health = ModelHealth.from_env(clock=_Clock())
    assert health.policy()["attempts_per_model"] == 2
    health.record([("primary", "network")])
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
        health.record([(model, "network")])

    threads = [
        threading.Thread(target=fail, args=(model,))
        for model in ("primary", "fallback")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert sorted(health.policy()["skip"]) == ["fallback", "primary"]
