"""The processor stores a vector as text, and stores the same text every time.

Scenario register (normative source: ADR 0035 §9; issue #82).

The `store` callable is the protocol's own boundary -- the child passes its own
and the parent stores the body -- so these drive it with the real thing rather
than with a stand-in for the module under test.
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path
from typing import Any

import pytest

from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError
from plugins.processors.embed import processor

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(FIXTURES))
    processor._ENCODERS.clear()


def _stored(item_ctx: dict[str, Any]) -> str:
    """Run the processor and return the body it handed to the store."""
    bodies: list[str] = []

    def store(content: str) -> str:
        bodies.append(content)
        return "processor-artifact"

    result = processor.process(item_ctx, target_lang="en", store=store)

    # The child refuses anything else, so returning the store's own value is the
    # contract rather than a convention (app/processor_runtime.py).
    assert result == "processor-artifact"
    (body,) = bodies
    return body


def _vector(body: str) -> list[float]:
    packed = base64.b64decode(body)
    return list(struct.unpack(f"<{len(packed) // 4}f", packed))


def test_the_artifact_is_a_384_float_vector_as_base64_text() -> None:
    body = _stored({"title": "内核调度器", "content": "<p>如何工作</p>"})

    assert isinstance(body, str)
    vector = _vector(body)
    assert len(vector) == 384
    assert all(isinstance(value, float) for value in vector)


def test_markup_does_not_change_the_vector() -> None:
    """The same words in the same order embed the same, tags or not.

    Without this, the tokens spent on markup would silently change every vector
    for every source that publishes HTML -- which is all of them.
    """
    assert _stored({"title": "", "content": "<p>linux scheduler</p>"}) == _stored(
        {"title": "", "content": "linux   scheduler"}
    )


def test_a_title_only_item_still_gets_a_vector() -> None:
    """AC-103: a title-only item is a fact about the item, not a crash."""
    assert len(_vector(_stored({"title": "linux scheduler", "content": ""}))) == 384


def test_an_unconfigured_model_directory_is_a_structure_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-104: the class says the directory is wrong, not the text."""
    monkeypatch.setenv("PEARSS_EMBEDDING_MODEL_DIR", str(tmp_path / "absent"))
    processor._ENCODERS.clear()

    with pytest.raises(ProcessorError) as failure:
        _stored({"title": "linux", "content": ""})

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "no manifest" in failure.value.detail
