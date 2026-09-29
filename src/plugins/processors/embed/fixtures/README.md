# The embedding test fixtures

Test fixtures, not models. They are shaped like the real thing -- ids and an
attention mask in, one hidden state per token out -- rather than like the task,
because a fixture that pre-pooled would leave the pooling, the masking and the
normalisation untested. Those are the parts `encoder.py` actually implements.

They exist so the ONNX runtime, the tokenizer and the pooling are exercised in CI
(ADR 0040, issue #82), where a 120 MB model has no business being. The real
encoder is downloaded and hash-verified at first use, and the tests that need its
*semantics* (cross-lingual recall) are separate for that reason.

`tiny-encoder.onnx` is an embedding table and a `Gather`: `Gather(data, indices)`,
so the table is the data input and the ids are the indices. Reversed, ONNX rejects
the graph with a message about the *type* of the table, which is why the order is
worth a sentence here. Both leading dimensions are dynamic, like a real export --
fixed at 1, the second row of a batch fails with an `InvalidArgument`.

Regenerate both (the seed is fixed, so the bytes are reproducible):

```bash
nix develop --command python - <<'PY'
import numpy as np, onnx
from onnx import TensorProto, helper

VOCAB, HIDDEN = 32, 384
table = np.random.default_rng(20260929).standard_normal((VOCAB, HIDDEN)).astype(np.float32)
node = helper.make_node("Gather", ["embedding_table", "input_ids"], ["last_hidden_state"], axis=0)
graph = helper.make_graph(
    [node], "tiny-encoder",
    [
        helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "sequence"]),
        helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["batch", "sequence"]),
    ],
    [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["batch", "sequence", HIDDEN])],
    [helper.make_tensor("embedding_table", TensorProto.FLOAT, [VOCAB, HIDDEN], table.ravel())],
)
model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
model.ir_version = 9
onnx.save(model, "src/plugins/processors/embed/fixtures/tiny-encoder.onnx")
PY

nix develop --command uv run python - <<'PY'
from tokenizers import Tokenizer, models, pre_tokenizers, processors

vocab = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3}
vocab.update({piece: index for index, piece in enumerate("内核调度器如何工作", start=4)})
tok = Tokenizer(models.WordPiece(vocab=vocab, unk_token="[UNK]", max_input_chars_per_word=64))
tok.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
tok.post_processor = processors.TemplateProcessing(
    single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)]
)
tok.save("src/plugins/processors/embed/fixtures/tiny-tokenizer.json")
PY
```

`onnx` is only needed to *build* the model fixture; running it needs `onnxruntime`
alone.
