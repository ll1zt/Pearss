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
