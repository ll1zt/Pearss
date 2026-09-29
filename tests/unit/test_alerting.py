"""When pearss decides to call for help, and what it says (ADR 0034).

Scenario register (this file is the normative list; the integration suite drives
the same scenarios through the scheduler and a real HTTP endpoint):

  AC-29 全局成功率塌陷时告警, 且只告警一次
    Given 最近 N 次 run 的成功率低于阈值, 样本量足够
    When 连续多个 tick 都处于该状态
    Then 恰好发出一条 DEGRADED 告警(边沿触发, 不是水平触发)

  AC-30 恢复时发一条恢复通知
    Given 已经告警过 DEGRADED
    When 成功率回到阈值之上
    Then 发出一条恢复通知
    And 恢复通知不受 MIN_INTERVAL 限制(恢复要立即知道)

  AC-31 样本量不足时不误报
    Given 只有很少几次 run(启动瞬间)
    When 判定
    Then 不告警 —— 1/1 失败不是"全局故障"

  AC-32 长时间中断不轰炸
    Given 状态一直是 DEGRADED
    When MIN_INTERVAL 未到
    Then 不重复发送; 超过 MIN_INTERVAL 后才再发一条

  AC-33 告警体带已算好的聚合, 且不含凭据
    Given 一个多源失败的世界
    When 构造告警体
    Then 正文含成功率/样本数/主错误分类与计数
    And 不含 token, Cookie, Authorization, 完整 URL 或错误详情原文

  AC-34 发送失败不打断采集
    Given 告警端点不可达(或未配置)
    When 一个 tick 需要告警
    Then 采集本身照常完成, 告警失败只记 warning

  AC-35 退避期间的日志能区分"失败中"与"一切正常"
    Given 某源连续失败并处于退避期
    When 一个 tick 评估它
    Then journal 里有一条指出连续失败次数与下次探测时间的日志
    And 健康的未到期源不产生这类日志
"""

from __future__ import annotations

import pytest

from core.alerting import (
    ALERT_DEFAULTS,
    AlertState,
    HealthVerdict,
    alert_body,
    decide_health,
)


def _verdict(
    snapshot: dict[str, object],
    *,
    success_rate_threshold: float = ALERT_DEFAULTS.success_rate_threshold,
    min_runs: int = ALERT_DEFAULTS.min_runs,
) -> HealthVerdict:
    """`decide_health` at the shipped defaults, overridable per test."""
    return decide_health(
        snapshot,
        success_rate_threshold=success_rate_threshold,
        min_runs=min_runs,
        min_interval_seconds=ALERT_DEFAULTS.min_interval_seconds,
    )


pytestmark = pytest.mark.unit


def _snapshot(
    *,
    success_rate: float | None,
    considered: int,
    error_class: str | None = None,
    error_count: int = 0,
) -> dict[str, object]:
    """The shape `system_snapshot` produces, reduced to what alerting reads."""
    by_error = {
        k: 0 for k in ("network", "parse", "structure_changed", "blocked", "auth")
    }
    if error_class:
        by_error[error_class] = error_count
    return {
        "recent_run_success_rate": success_rate,
        "recent_runs_considered": considered,
        "runs_by_error_class": by_error,
    }


# --------------------------------------------------------------------------
# AC-29 / AC-31: when collapse is real
# --------------------------------------------------------------------------


def test_a_collapsed_success_rate_is_degraded() -> None:
    """AC-29: 56 of 56 failing is exactly the case that went unnoticed for 15h."""
    verdict = _verdict(
        _snapshot(
            success_rate=0.0, considered=50, error_class="network", error_count=377
        ),
    )

    assert verdict.degraded is True
    assert verdict.reason, "a degraded verdict must say why"


def test_a_healthy_success_rate_is_not_degraded() -> None:
    """The other side of the same threshold, so the test can fail both ways."""
    verdict = _verdict(
        _snapshot(
            success_rate=1.0, considered=50, error_class="blocked", error_count=2
        ),
    )

    assert verdict.degraded is False


def test_too_few_runs_is_not_degraded() -> None:
    """AC-31: one failed run at startup is not a global outage.

    `recent_run_success_rate` is computed over whatever has finished, so with a
    single run it is 0.0 or 1.0. Applying the threshold without a minimum sample
    would alert on the first collection attempt of a fresh deployment -- and an
    alert that fires when nothing is wrong is how alerting gets turned off.
    """
    verdict = _verdict(
        _snapshot(success_rate=0.0, considered=1, error_class="network", error_count=1),
    )

    assert verdict.degraded is False, (
        "a single failed run must not be reported as a global failure"
    )


def test_no_runs_yet_is_not_degraded() -> None:
    """A deployment that has never collected has no success rate at all."""
    verdict = _verdict(_snapshot(success_rate=None, considered=0))

    assert verdict.degraded is False


def test_the_threshold_is_the_configured_one() -> None:
    """The knob is real: same snapshot, two thresholds, two verdicts."""
    snapshot = _snapshot(success_rate=0.6, considered=50)

    assert (
        decide_health(snapshot, success_rate_threshold=0.5, min_runs=10).degraded
        is False
    )
    assert (
        decide_health(snapshot, success_rate_threshold=0.9, min_runs=10).degraded
        is True
    )


def test_the_minimum_sample_is_the_configured_one() -> None:
    snapshot = _snapshot(success_rate=0.0, considered=3)

    assert (
        decide_health(snapshot, success_rate_threshold=0.5, min_runs=2).degraded is True
    )
    assert (
        decide_health(snapshot, success_rate_threshold=0.5, min_runs=10).degraded
        is False
    )


# --------------------------------------------------------------------------
# AC-33: the body carries the aggregate and nothing sensitive
# --------------------------------------------------------------------------


def test_the_body_carries_the_already_computed_aggregate() -> None:
    """AC-33: the numbers that make the direction obvious at a glance.

    In the incident that motivated this, "all network, all in the same minute"
    pointed straight at a shared dependency (proxy/credentials) instead of
    sending the operator through 56 sources one by one.
    """
    verdict = _verdict(
        _snapshot(
            success_rate=0.0, considered=50, error_class="network", error_count=377
        ),
    )

    body = alert_body(verdict)

    assert "0" in body, "the success rate must appear"
    assert "50" in body, f"the sample size must appear: {body}"
    assert "network" in body, f"the dominant error class must appear: {body}"
    assert "377" in body, f"the error count must appear: {body}"


def test_the_body_never_carries_credentials_or_urls() -> None:
    """AC-33 (the red line): AGENTS.md forbids tokens, cookies and private URLs.

    Built from fixed fields rather than from an exception repr, so a future
    "include the error detail" change cannot smuggle a token or a private feed
    URL into a notification that leaves the host.
    """
    verdict = _verdict(
        _snapshot(success_rate=0.0, considered=50, error_class="auth", error_count=3),
    )

    body = alert_body(verdict)

    for forbidden in (
        "Authorization",
        "Bearer",
        "Cookie",
        "token",
        "http://",
        "https://",
        "password",
    ):
        assert forbidden.lower() not in body.lower(), (
            f"the alert body must not contain {forbidden!r}: {body}"
        )


def test_the_body_names_the_reason_not_just_a_number() -> None:
    """A bare "success rate low" leaves the operator guessing what broke."""
    verdict = _verdict(
        _snapshot(
            success_rate=0.0, considered=50, error_class="blocked", error_count=9
        ),
    )

    body = alert_body(verdict)

    assert "blocked" in body
    assert verdict.reason in body or "success" in body.lower()


# --------------------------------------------------------------------------
# The verdict is a value, not a side effect
# --------------------------------------------------------------------------


def test_deciding_health_has_no_io(tmp_path: object) -> None:
    """The decision is pure: the same snapshot always yields the same verdict.

    `core/` may not do I/O (AGENTS.md layering), and the send is the adapter's
    job. Asserted by construction -- calling it twice with the same input gives
    equal verdicts, which a hidden clock or DB would break.
    """
    snapshot = _snapshot(success_rate=0.0, considered=50, error_class="network")

    first = _verdict(snapshot)
    second = _verdict(snapshot)

    assert first == second


def test_verdict_is_a_frozen_value() -> None:
    """It crosses from core to the adapter, so it must not be mutable."""
    verdict = _verdict(_snapshot(success_rate=0.0, considered=50))

    assert isinstance(verdict, HealthVerdict)
    with pytest.raises((AttributeError, TypeError)):
        verdict.degraded = False  # type: ignore[misc]


# --------------------------------------------------------------------------
# AC-32: the repeat interval, stated as the state machine's truth table
#
# `AlertState` is where "one message per state change" lives, so its cases are
# pinned here rather than only through the scheduler. Each is a distinct edge a
# regression could flatten: without these, "fires once" passes even if the
# re-notify path never works, and "recovery is prompt" passes even if recovery
# is suppressed entirely.
# --------------------------------------------------------------------------


def _verdict_with(degraded: bool) -> HealthVerdict:
    return HealthVerdict(degraded=degraded, reason="r" if degraded else "")


def test_a_continuing_outage_renotifies_after_the_interval() -> None:
    """AC-32: the *other* side of the interval -- it must not be a one-shot.

    A degradation that lasts for days and is never mentioned again is its own
    kind of silence: the operator who ignored the first message has no way to
    see that it is still true. Pinned here because the tests that assert "only
    once" pass equally against an implementation that can never repeat at all.
    """
    state = AlertState()

    assert (
        state.should_send(_verdict_with(True), now=0.0, min_interval_seconds=100)
        is True
    )
    assert (
        state.should_send(_verdict_with(True), now=50.0, min_interval_seconds=100)
        is False
    )
    assert (
        state.should_send(_verdict_with(True), now=101.0, min_interval_seconds=100)
        is True
    ), "past the interval, a still-broken system must say so again"


def test_the_first_degraded_verdict_always_sends() -> None:
    """No prior send means nothing to suppress: the first message is the point.

    Guards an off-by-one where a fresh process with `last_sent_at=None` treats
    the interval as already elapsed against a zero clock.
    """
    state = AlertState()
    # A large clock value with no history: the interval must not be measured
    # against it and suppress the very first alert.
    assert (
        state.should_send(_verdict_with(True), now=10_000.0, min_interval_seconds=3600)
        is True
    )


def test_a_recovered_then_degraded_again_pair_is_two_messages() -> None:
    """Two transitions, two messages -- even inside one interval window.

    The failure this pins is a rate limiter applied to transitions instead of to
    repeats: a system that flaps back into trouble within the hour must not have
    its second collapse swallowed.
    """
    state = AlertState()
    assert state.should_send(_verdict_with(True), now=0.0, min_interval_seconds=3600)
    assert state.should_send(_verdict_with(False), now=1.0, min_interval_seconds=3600)
    assert (
        state.should_send(_verdict_with(True), now=2.0, min_interval_seconds=3600)
        is True
    ), "a new collapse is news, not a repeat"


# --------------------------------------------------------------------------
# The adapter's own edges
# --------------------------------------------------------------------------


def test_an_unusable_timeout_setting_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in the environment must not remove the bound.

    The timeout is what keeps a wedged gateway from stalling a scheduler tick,
    so reading a bad value as 0 (connect immediately / never) would reintroduce
    the failure it exists to prevent.
    """
    from adapters.alerting import DEFAULT_TIMEOUT_SECONDS, alert_timeout_seconds

    for bad in ("abc", "0", "-1"):
        monkeypatch.setenv("PEARSS_ALERT_TIMEOUT_SECONDS", bad)
        assert alert_timeout_seconds() == DEFAULT_TIMEOUT_SECONDS, bad

    monkeypatch.setenv("PEARSS_ALERT_TIMEOUT_SECONDS", "7.5")
    assert alert_timeout_seconds() == 7.5


def test_sending_without_an_endpoint_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unconfigured endpoint is reported once, not once per tick.

    The journal should explain why nothing is being alerted, without becoming
    its own source of noise (the same reasoning as decision 2).
    """
    from adapters import alerting

    monkeypatch.delenv("PEARSS_ALERT_URL", raising=False)
    monkeypatch.delenv("NOTIFY_URL", raising=False)
    # The once-per-process flag is module state; reset it so the test does not
    # depend on which test ran first.
    monkeypatch.setattr(alerting, "_UNCONFIGURED_WARNED", False, raising=False)

    with caplog.at_level("WARNING", logger="adapters.alerting"):
        assert alerting.send_alert("body", title="t", source="s") is False
        assert alerting.send_alert("body", title="t", source="s") is False

    warnings = [r for r in caplog.records if "no alert endpoint" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


def test_an_unexpected_exception_is_also_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The side channel must survive *any* failure, not only httpx's.

    A gateway misconfigured to something that raises a different way (a bad
    scheme, a transport that raises ValueError) must not take the scheduler
    down with it. The broad handler exists for exactly this; here it is driven.
    """
    from adapters import alerting

    monkeypatch.setenv("PEARSS_ALERT_URL", "http://example.test/notify")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("transport blew up")

    # Patched on the module httpx itself, not on `alerting.httpx`: the adapter
    # does not re-export httpx as part of its surface, and reaching through
    # would make this test depend on an implementation detail (mypy flags it
    # for the same reason).
    import httpx as httpx_module

    monkeypatch.setattr(httpx_module, "post", explode)

    assert alerting.send_alert("body", title="t", source="s") is False


def test_an_unreadable_error_class_map_is_skipped_not_guessed() -> None:
    """A snapshot whose error map holds junk must not invent a dominant class.

    `_dominant_error` runs on a plain dict from the adapter layer. Skipping an
    unreadable entry is the honest move: coercing it to zero would be
    indistinguishable from "no failures", which is the reading this module
    exists to avoid manufacturing.
    """
    snapshot = _snapshot(success_rate=0.0, considered=50)
    snapshot["runs_by_error_class"] = {"network": "not-a-number", "parse": 7}

    verdict = _verdict(snapshot)

    assert verdict.degraded is True
    assert verdict.dominant_error_class == "parse", verdict
    assert verdict.dominant_error_count == 7


def test_an_empty_error_map_yields_no_dominant_class() -> None:
    """A snapshot with no error counts at all is a valid (if odd) input."""
    snapshot = _snapshot(success_rate=0.0, considered=50)
    snapshot["runs_by_error_class"] = {}

    verdict = _verdict(snapshot)

    assert verdict.degraded is True
    assert verdict.dominant_error_class is None
    assert verdict.dominant_error_count == 0


def test_a_degraded_verdict_with_no_rate_still_says_something_useful() -> None:
    """A degraded verdict without numbers must not produce an empty message.

    Reachable when the collapse is detected from a signal other than the rate
    (the ADR's other two conditions), or when the snapshot carried no rate at
    all. The message falls back to the reason and the action line, so the
    operator is never sent a blank notification.
    """
    verdict = HealthVerdict(degraded=True, reason="enrichment retries exhausted")

    body = alert_body(verdict)

    assert "enrichment retries exhausted" in body
    assert "action:" in body, body


def test_a_verdict_without_a_reason_falls_back_to_a_default_sentence() -> None:
    """`reason` is optional in the value, so the body supplies one.

    A notification whose first line is blank reads as a bug in the notifier
    rather than as a real condition.
    """
    body = alert_body(HealthVerdict(degraded=True))

    assert body.splitlines()[0].strip(), body
    assert "attention" in body, body


def test_a_renotify_after_the_interval_records_the_new_send_time() -> None:
    """The interval is measured from the *last send*, not from the first.

    Pinned because the alternative is a slow drumbeat that looks right in a
    single test: if the timestamp were never advanced, a system that stayed
    broken would notify on every tick after the first interval elapsed.
    """
    state = AlertState()
    assert state.should_send(_verdict_with(True), now=0.0, min_interval_seconds=100)
    assert state.should_send(_verdict_with(True), now=100.0, min_interval_seconds=100)
    # The second send moved the window, so 150 is inside it again.
    assert (
        state.should_send(_verdict_with(True), now=150.0, min_interval_seconds=100)
        is False
    )
    assert state.should_send(_verdict_with(True), now=201.0, min_interval_seconds=100)


def test_an_already_degraded_state_with_no_history_sends() -> None:
    """A state constructed as already-degraded with no send on record notifies.

    Reachable for a caller (or a future restart-handling path) that seeds the
    state from the current condition rather than from a transition. The wrong
    answer is to treat the missing timestamp as "we just sent one" and stay
    silent about a system that is broken right now.
    """
    state = AlertState(degraded=True, last_sent_at=None)

    assert (
        state.should_send(_verdict_with(True), now=500.0, min_interval_seconds=3600)
        is True
    )


def test_the_recovery_body_says_what_recovered() -> None:
    """The recovery message is its own string, and it must be about collection.

    Asserted on the body rather than on the payload: the earlier integration
    check matched the *title* ("Pearss recovered"), so the body could have been
    empty and the test would still have passed. A notification whose text says
    nothing is the same silence this module exists to end.
    """
    body = alert_body(HealthVerdict(degraded=False))

    assert "recovered" in body.lower(), body
    assert "collection" in body.lower(), body


def test_a_healthy_verdict_on_a_healthy_state_sends_nothing() -> None:
    """The most common call in production: healthy, and already was healthy.

    Pinned because it is the only path that neither transitions nor renotifies,
    and a regression that returned `True` here would notify on every tick of a
    perfectly working system -- the fastest possible way to get a channel muted.
    """
    state = AlertState()

    assert (
        state.should_send(_verdict_with(False), now=0.0, min_interval_seconds=3600)
        is False
    )
    assert (
        state.should_send(_verdict_with(False), now=10_000.0, min_interval_seconds=3600)
        is False
    )


# --------------------------------------------------------------------------
# The two conditions ADR 0034 named and left unimplemented (issue #61)
# --------------------------------------------------------------------------

# A state the *other* criterion is happy with, so the new conditions have to
# degrade it on their own rather than riding on a rate that was already bad.
_HEALTHY: dict[str, object] = {
    "recent_run_success_rate": 1.0,
    "recent_runs_considered": 50,
}


@pytest.mark.unit
def test_a_stuck_chain_degrades_an_otherwise_healthy_state() -> None:
    """Issue #61: a chain that cannot finish is not a slow success rate.

    The rate counts *runs*, and these are rows no future run clears on its own --
    so a perfectly healthy rate with a stuck chain is exactly the case the rate
    alone reports as fine. The condition has to be able to degrade a state the
    other criterion is happy with.
    """
    healthy = _HEALTHY
    assert decide_health(healthy).degraded is False

    verdict = decide_health(healthy, exhausted_sources=("src-a",))
    assert verdict.degraded is True
    assert verdict.exhausted_sources == ("src-a",)
    assert "src-a" in verdict.reason


@pytest.mark.unit
def test_a_dead_loop_degrades_and_is_named_in_the_message() -> None:
    """A loop cannot report its own death; whoever is still running says so."""
    healthy = _HEALTHY
    verdict = decide_health(healthy, dead_loops=("enrichment",))
    assert verdict.degraded is True
    assert verdict.dead_loops == ("enrichment",)
    assert "enrichment" in alert_body(verdict)


@pytest.mark.unit
def test_recovery_is_still_a_recovery_when_the_new_conditions_clear() -> None:
    """The edge the alert state reads must not be pinned degraded by a stale fact."""
    healthy = _HEALTHY
    assert decide_health(healthy, exhausted_sources=()).degraded is False
    assert decide_health(healthy, dead_loops=()).degraded is False
