"""Tests for error classification precedence (B5).

The precedence order is the part most likely to be reimplemented wrongly in a
collector's except block, so it is pinned here with an explicit test per rule.
"""

from __future__ import annotations

import pytest

from core.enums import ErrorClass
from core.errors import classify, merge

PRECEDENCE_ORDER = [
    ErrorClass.BLOCKED,
    ErrorClass.AUTH,
    ErrorClass.NETWORK,
    ErrorClass.PARSE,
    ErrorClass.STRUCTURE_CHANGED,
]


@pytest.mark.unit
def test_class_has_exactly_five_members() -> None:
    assert len(ErrorClass) == 5
    assert {c.value for c in ErrorClass} == {
        "network",
        "parse",
        "structure_changed",
        "blocked",
        "auth",
    }


@pytest.mark.unit
def test_precedence_is_a_total_order() -> None:
    ranks = [c.precedence for c in PRECEDENCE_ORDER]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, ErrorClass.BLOCKED),
        (429, ErrorClass.BLOCKED),
        (401, ErrorClass.AUTH),
        (500, ErrorClass.NETWORK),
        (502, ErrorClass.NETWORK),
        (503, ErrorClass.NETWORK),
    ],
)
def test_http_status_classification(status: int, expected: ErrorClass) -> None:
    assert classify(http_status=status) is expected


@pytest.mark.unit
def test_structure_changed_requires_a_selector_miss() -> None:
    assert classify(http_status=200, matched_expected_structure=False) is (
        ErrorClass.STRUCTURE_CHANGED
    )


@pytest.mark.unit
def test_http_200_with_matching_structure_is_not_an_error() -> None:
    assert classify(http_status=200, matched_expected_structure=True) is None


@pytest.mark.unit
def test_no_evidence_returns_unclassified() -> None:
    assert classify() is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        (
            [ErrorClass.AUTH, ErrorClass.STRUCTURE_CHANGED],
            ErrorClass.AUTH,
        ),
        (
            [ErrorClass.STRUCTURE_CHANGED, ErrorClass.NETWORK],
            ErrorClass.NETWORK,
        ),
        (
            [ErrorClass.PARSE, ErrorClass.BLOCKED, ErrorClass.NETWORK],
            ErrorClass.BLOCKED,
        ),
        ([None, ErrorClass.PARSE], ErrorClass.PARSE),
    ],
)
def test_merge_picks_highest_precedence(
    candidates: list[ErrorClass | None], expected: ErrorClass
) -> None:
    assert merge(*candidates) is expected


@pytest.mark.unit
def test_merge_of_nothing_is_unclassified() -> None:
    assert merge() is None
    assert merge(None, None) is None


@pytest.mark.unit
def test_retry_policy_matches_the_documented_table() -> None:
    retryable = {c for c in ErrorClass if c.allows_automatic_retry}
    assert retryable == {ErrorClass.NETWORK, ErrorClass.PARSE}
