"""The chosen embedding runtime must actually run here, in the pinned environment.

Scenario register (normative source: ADR 0035 §9; issue #82).

D10a chose onnxruntime for local inference, and the feasibility question in issue
#120 was settled with probes in a scratch shell. A probe is not a regression test:
it proves a fact once, on one machine, and then stops proving it. This loads a
real graph and runs it, so a system library missing from the loader path, a wheel
that no longer matches the interpreter, or a `flake.lock` bump that breaks the
pair fails here rather than at first use in production.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    REPO_ROOT
    / "src"
    / "plugins"
    / "processors"
    / "embed"
    / "fixtures"
    / "tiny-encoder.onnx"
)


def test_the_onnx_runtime_runs_a_real_graph_under_the_pinned_environment() -> None:
    """AC: a real inference, 384 float32 outputs, the same vector twice.

    The shape and dtype are asserted exactly, not just "it returned something":
    the vector is stored as float32 and consumed by a dot product that assumes
    the width it was produced at, so a silently different width or precision is
    the failure this test exists to catch.
    """
    import onnxruntime as ort

    session = ort.InferenceSession(str(FIXTURE), providers=["CPUExecutionProvider"])
    row = np.ones((1, 8), dtype=np.float32)

    first = session.run(None, {"text_features": row})[0]
    second = session.run(None, {"text_features": row})[0]

    assert first.shape == (1, 384), first.shape
    assert first.dtype == np.float32, first.dtype
    assert (first == second).all(), "the same input must produce the same vector"
