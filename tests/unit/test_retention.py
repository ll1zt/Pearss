"""The two retention windows, and what a malformed setting means.

Both windows are environment settings read on every maintenance pass, and both
have the same three shapes: a default, an explicit number of days, and `0`
meaning "keep everything". The failure mode they share is the reason they are
tested together: a *typo* must not silently become "delete everything" or
"delete nothing". `PEARSS_WORK_RETENTION_DAYS=thrity` has to mean 30, and so does
`-5` -- neither may reach the delete with a surprising cutoff.

Scenario register (issue #203; ADR 0016 for the audit window, ADR 0046 D-F for
the ledger):

  AC-224 保留窗口读得对, 且坏值不改变删除行为
    Given PEARSS_*_RETENTION_DAYS 未设置 / 正整数 / 0 / 负数 / 非数字
    When  解析保留天数
    Then  分别是 默认 30 / 该天数 / 0 / 0 / 30

The pruning behaviour itself is asserted against a real database in
`tests/integration/test_scheduler_maintenance.py`; this file only pins what the
number means.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.scheduler import audit_retention_days, work_retention_days

pytestmark = pytest.mark.unit

PARSERS = [
    pytest.param(audit_retention_days, "PEARSS_AUDIT_RETENTION_DAYS", id="audit"),
    pytest.param(work_retention_days, "PEARSS_WORK_RETENTION_DAYS", id="ledger"),
]


@pytest.mark.parametrize(("parser", "variable"), PARSERS)
def test_an_unset_window_is_thirty_days(
    parser: Callable[[], int], variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(variable, raising=False)
    assert parser() == 30


@pytest.mark.parametrize(("parser", "variable"), PARSERS)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7", 7),
        ("0", 0),
        ("-5", 0),
        ("thrity", 30),
        ("", 30),
        (" 30 ", 30),
    ],
)
def test_a_window_is_read_as_days_and_a_typo_means_the_default(
    parser: Callable[[], int],
    variable: str,
    raw: str,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(variable, raw)
    assert parser() == expected
