"""Error classification.

Single source of truth for mapping an acquisition failure onto the five
categories required by B5. Collectors raise whatever exception their client
produces; this module decides which ErrorClass it is, so that classification
precedence and retry policy live in one testable place instead of being
reimplemented in each collector's `except` block.
"""

from __future__ import annotations

from core.enums import ErrorClass


def classify(
    http_status: int | None = None, *, matched_expected_structure: bool = True
) -> ErrorClass | None:
    """Classify a failed fetch from the strongest available evidence.

    Precedence is blocked > auth > network > parse > structure_changed, per
    docs/domain/error-taxonomy.md. Returns None when the evidence does not
    point at any category, which callers must treat as "unclassified" rather
    than defaulting to something.

    `matched_expected_structure` says whether the collector's selectors still
    find what they expect. It is the only signal that distinguishes
    `structure_changed` from a successful-but-empty page, and it is why HTTP
    200 alone is never enough to conclude a fetch succeeded.
    """
    # Signals, then precedence -- rather than an if-chain whose *order* is the
    # precedence. The order was a second copy of a rule `ErrorClass.precedence`
    # already states, and `merge` is the one that reads it (issue #87).
    signals: list[ErrorClass] = []
    if http_status is not None:
        if http_status in (403, 429):
            signals.append(ErrorClass.BLOCKED)
        if http_status == 401:
            signals.append(ErrorClass.AUTH)
        if http_status >= 500:
            signals.append(ErrorClass.NETWORK)
    if not matched_expected_structure:
        signals.append(ErrorClass.STRUCTURE_CHANGED)

    return merge(*signals)


def merge(*candidates: ErrorClass | None) -> ErrorClass | None:
    """Pick the governing class among several signals.

    When one failure produces multiple signals, the winner is the one with the
    highest precedence — an unauthenticated response that also failed to parse
    is an auth problem, not a parse problem.
    """
    present = [c for c in candidates if c is not None]
    if not present:
        return None
    return min(present, key=lambda c: c.precedence)
