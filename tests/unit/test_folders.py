"""Folder names: the one place a sidebar label becomes a stream id.

A folder name reaches a client inside `user/-/label/<name>`, so the
constraints here are not cosmetic -- a name that cannot round-trip through
that encoding shows the user a subscription they cannot select. Both the /v1
API and the Reader API call this, and `core/folders.py` says a single
definition is what keeps them agreeing.

Tested directly because the function is pure and its whole contract is
input/output. Declared in tests/unit so it falls inside the mutation gate;
before this, `normalize_folder` was reached only through integration routes.
"""

from __future__ import annotations

import pytest

from core.folders import DEFAULT_FOLDER, MAX_FOLDER_LENGTH, normalize_folder

pytestmark = pytest.mark.unit


def test_a_plain_name_passes_through() -> None:
    assert normalize_folder("Tech News") == "Tech News"


def test_surrounding_whitespace_is_trimmed() -> None:
    """The common paste mistake: a name copied with a trailing newline."""
    assert normalize_folder("  Tech News\n") == "Tech News"


def test_interior_whitespace_is_preserved() -> None:
    """Multi-word names are normal, so interior spacing is not collapsed."""
    assert normalize_folder("Tech  News") == "Tech  News"


def test_a_name_at_the_length_limit_is_accepted() -> None:
    name = "x" * MAX_FOLDER_LENGTH

    assert normalize_folder(name) == name


def test_a_name_past_the_length_limit_is_refused() -> None:
    with pytest.raises(ValueError):
        normalize_folder("x" * (MAX_FOLDER_LENGTH + 1))


def test_an_empty_name_is_refused() -> None:
    for value in ("", "   ", "\n", "\t"):
        with pytest.raises(ValueError):
            normalize_folder(value)


def test_a_slash_is_refused() -> None:
    """A slash makes a stream id no client can round-trip.

    The folder would appear under one name and answer under none, which is a
    user-visible breakage rather than an inconvenience.
    """
    for value in ("a/b", "/leading", "trailing/", "a b/c"):
        with pytest.raises(ValueError):
            normalize_folder(value)


def test_the_stream_id_prefix_is_refused() -> None:
    """`user/-/label/` is the encoding, not a name.

    Accepting it would nest the encoding inside itself and produce a stream id
    that matches nothing.
    """
    with pytest.raises(ValueError):
        normalize_folder("user/-/label/Tech")


def test_a_control_character_is_refused() -> None:
    for value in ("bad\x00name", "bad\nname", "bad\x1fname", "bell\x07"):
        with pytest.raises(ValueError):
            normalize_folder(value)


def test_a_non_ascii_name_is_accepted() -> None:
    """Folder names are user text, not identifiers.

    A Chinese or accented name is printable and round-trips through the stream
    id, so refusing it would be an unfounded restriction.
    """
    assert normalize_folder("科技新闻") == "科技新闻"
    assert normalize_folder("Café") == "Café"


def test_the_default_folder_is_a_legal_name() -> None:
    """The constant the code falls back to must itself pass validation.

    Otherwise the default path would raise on the one value the system
    supplies by itself.
    """
    assert normalize_folder(DEFAULT_FOLDER) == DEFAULT_FOLDER
