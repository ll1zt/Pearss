"""The retention windows, the work lease, and what a malformed setting means.

All three are environment settings read on every pass, and all three have the
same three shapes: a default, an explicit value, and `0` meaning the permissive
end of the scale (`0` days keeps everything; `0` seconds of lease disables the
lease).

The failure mode they share is why they are tested together: a *typo* must not
silently become "delete everything" or "delete nothing".
`PEARSS_WORK_RETENTION_DAYS=thrity` has to mean 30, and so does `-5` -- neither
may reach the delete with a surprising cutoff.

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
from app.work_worker import work_lease_seconds

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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 900.0),
        ("60", 60.0),
        ("0", 0.0),
        ("-5", 0.0),
        ("oops", 900.0),
        ("", 900.0),
        (" 120 ", 120.0),
        ("12.5", 12.5),
    ],
)
def test_the_work_lease_is_read_as_seconds(
    raw: str | None, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PEARSS_WORK_LEASE_SECONDS`, including the two ends that matter.

    `0` must reach the claim as *zero* (take any `running` work: the pre-#212
    behaviour, and the escape hatch for a stranded one) and a typo must fall back
    to the default rather than to zero -- a malformed setting that silently
    removed the protection would be the worst possible reading.
    """
    if raw is None:
        monkeypatch.delenv("PEARSS_WORK_LEASE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PEARSS_WORK_LEASE_SECONDS", raw)
    assert work_lease_seconds() == expected
