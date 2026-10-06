"""Scenario register (normative source: SCENARIOS.md, AC-323..326; issue #348).

These pin what `reader_api` already does with stream ids, so a later change to
the patterns in that module has to argue with a test rather than slip past one.
The two shapes that matter in practice:

    state streams belong to the `user/<id>/state/com.google/<state>` family,
    and only that family. A bare `com.google/starred` reads as "unknown", not
    as "starred" -- a client naming a state we cannot parse gets an empty
    filter, never the unfiltered list.

    label streams carry folders, and only the literal `user/-/label/` prefix
    is a folder. `user/123/label/Work` names another user's label and is not
    one of ours.

Both are pins of existing behaviour (already correct), not fixes: the defect in
issue #348 was the documentation promising a tolerance the code never had, plus
a prefix string (`user/%s/label/`) that could never match anything.
"""

from __future__ import annotations

import pytest

from adapters.reader_api import label_stream_name, state_stream_name


@pytest.mark.unit
def test_bare_state_stream_is_unknown_rather_than_starred() -> None:
    """AC-323: `com.google/starred` alone names no state we can act on.

    Reading it as `starred` would be a quiet widening: a filter the caller
    cannot interpret must become an empty stream (see `stream_items_ids` in
    `app/reader/routes.py`), not the whole reading list.
    """
    assert state_stream_name("com.google/starred") is None


@pytest.mark.unit
def test_fully_qualified_state_stream_reduces_to_its_state_name() -> None:
    """AC-324: the `user/<id>/state/com.google/<state>` family is recognised."""
    assert state_stream_name("user/-/state/com.google/starred") == "starred"
    assert state_stream_name("user/123/state/com.google/read") == "read"


@pytest.mark.unit
def test_another_users_label_prefix_is_not_a_folder_of_ours() -> None:
    """AC-325: only `user/-/label/<name>` names one of our folders.

    `user/<id>/label/<name>` belongs to a different account's namespace. The
    prefix table used to carry a literal `user/%s/label/` entry that could
    never match (no formatting was ever applied), which read as though it were
    supported; this pins the deliberate refusal.
    """
    assert label_stream_name("user/123/label/Work") is None
    assert label_stream_name("user/%s/label/Work") is None  # the dead prefix


@pytest.mark.unit
def test_our_label_stream_yields_its_folder_name() -> None:
    """AC-326: `user/-/label/<name>` is our folder stream."""
    assert label_stream_name("user/-/label/Work") == "Work"
