# `tiny-encoder.onnx`

A test fixture, not a model: an 8 -> 384 `MatMul` with deterministic weights, used
to prove that the ONNX runtime runs a real graph in this project's environment
(ADR 0035 decision 5, issue #82). The real encoder is downloaded and verified at
first use, not committed.

It exists because a probe in a scratch shell is not a regression test. Loading a
graph and running it here is what fails if a system library is missing from the
loader path, if the wheel stops matching the interpreter, or if a `flake.lock`
bump breaks the pair.

Regenerate with the same generator that made it:

```bash
nix develop --command python - <<'PY'
import numpy as np, onnx
from onnx import TensorProto, helper

W = np.random.default_rng(20260929).standard_normal((8, 384)).astype(np.float32)
graph = helper.make_graph(
    [helper.make_node("MatMul", ["text_features", "W"], ["vector"])],
    "tiny-encoder",
    [helper.make_tensor_value_info("text_features", TensorProto.FLOAT, [1, 8])],
    [helper.make_tensor_value_info("vector", TensorProto.FLOAT, [1, 384])],
    [helper.make_tensor("W", TensorProto.FLOAT, [8, 384], W.ravel())],
)
onnx.save(
    helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)]),
    "src/plugins/processors/embed/fixtures/tiny-encoder.onnx",
)
PY
```

`onnx` is only needed to *build* the fixture; running it needs `onnxruntime`
alone. The seed is fixed so a regeneration produces the same bytes.
