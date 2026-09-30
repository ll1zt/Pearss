"""Real embedding weights: "cannot look" is "not there" (issue #179).

Scenario register (normative source: `SCENARIOS.md`, AC-179).

`PEARSS_EMBEDDING_MODEL_DIR` may point at a directory this process cannot read --
the shape a multi-user host has, where the service's copy belongs to another
account. `Path.is_file()` raises there instead of answering "no", which turned
four documented skips into failures. These pin the shared answer the three call
sites now use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import model_weights

pytestmark = pytest.mark.integration


def test_a_readable_model_directory_is_reported_as_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "model"
    directory.mkdir()
    (directory / "manifest.json").write_text(
        '{"name": "multilingual-e5-small"}', encoding="utf-8"
    )
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(directory))

    assert model_weights.readable_weights() == directory
    assert model_weights.declared_name(directory) == "multilingual-e5-small"


def test_a_missing_model_directory_is_reported_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(tmp_path / "nothing-here"))

    assert model_weights.readable_weights() is None


def test_an_unreadable_model_directory_is_reported_as_absent(
    unreadable_model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-179: the weights are there and the answer is still "none".

    A caller that needs real weights can only skip in that case; a
    `PermissionError` escaping the check is what made four documented skips into
    failures on a machine whose service owns the model directory.
    """
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(unreadable_model_dir))

    assert model_weights.readable_weights() is None
    # Reading the manifest is the other half: it must not raise either, or the
    # "is this the stand-in?" question would crash instead of skipping.
    assert model_weights.declared_name(unreadable_model_dir) is None
