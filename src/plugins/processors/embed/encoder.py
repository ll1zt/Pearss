"""Turning text into a vector: the model files, the tokenizer, the pooling.

The encoder is the part of an embedding that is *not* the pipeline. It owns a
model directory, tokenises, runs the ONNX graph and reduces the per-token
vectors to one. Where the vector then goes -- the row, the gate, the retry --
belongs to the chain around it.

Three things here are load-bearing rather than stylistic:

- The runtime is imported lazily. The worker imports every registered processor
  when it starts, and a 40 MB native library is not something to load in order
  to answer a `--help`.
- The width is checked against what the manifest declares, and a mismatch is
  refused rather than stored. A vector consumed at the wrong width is exactly the
  failure ADR 0035 decision 6 exists to prevent, and the shape is the last point
  at which it is still visible -- after this it is a dot product that quietly
  returns nonsense.
- Model problems are `structure_changed`, not `parse`. What is wrong is the
  directory we expected to find, not the text we were given, and an operator
  reading the run record needs to know which of the two to go and look at.

The manifest itself lives in `adapters.embedding_model`: it is read from disk and
`parse_chain` needs the same identity when it enqueues the row.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from adapters.embedding_model import ModelManifest, load_manifest
from core.enrichment import ModelIdentity
from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError

# e5-family models are trained on 512-token inputs; longer text is truncated
# rather than refused, because a long article still deserves a vector.
DEFAULT_MAX_TOKENS = 512

# The interface every ONNX export of this family is expected to expose. Checked
# up front so a different export fails with a sentence instead of a KeyError.
REQUIRED_INPUTS = ("input_ids", "attention_mask")


class Encoder:
    """One model directory, loaded on first use.

    `as_query` is not cosmetic: e5-family models are trained with distinct
    prefixes for the two sides of a retrieval, and using the passage prefix for
    a query (or none at all) measurably degrades ranking while looking perfectly
    healthy.
    """

    def __init__(
        self,
        directory: Path,
        *,
        manifest: ModelManifest | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._directory = Path(directory)
        self._manifest = (
            manifest if manifest is not None else load_manifest(self._directory)
        )
        self._max_tokens = max_tokens
        self._session: Any = None
        self._tokenizer: Any = None
        self._inputs: frozenset[str] = frozenset()

    @property
    def identity(self) -> ModelIdentity:
        return self._manifest.identity

    @property
    def dimensions(self) -> int:
        return self._manifest.dimensions

    def encode(self, text: str, *, as_query: bool = False) -> list[float]:
        """One vector for one text. L2-normalised, so a dot product is a cosine."""
        return self.encode_batch([text], as_query=as_query)[0]

    def encode_batch(
        self, texts: Sequence[str], *, as_query: bool = False
    ) -> list[list[float]]:
        """One vector per text, padded to the batch's longest input.

        Padding uses the mask rather than a fixed length so a short item does not
        pay for a long one, and the pooled result for a text is the same whether
        it was encoded alone or beside a longer neighbour.
        """
        if not texts:
            return []
        session, tokenizer = self._ensure_loaded()
        prefix = (
            self._manifest.query_prefix if as_query else self._manifest.passage_prefix
        )
        encodings = [tokenizer.encode(f"{prefix}{text}") for text in texts]

        width = min(max(len(encoding.ids) for encoding in encodings), self._max_tokens)
        ids = np.zeros((len(encodings), width), dtype=np.int64)
        mask = np.zeros((len(encodings), width), dtype=np.int64)
        for row, encoding in enumerate(encodings):
            length = min(len(encoding.ids), width)
            ids[row, :length] = encoding.ids[:length]
            mask[row, :length] = 1

        try:
            hidden = session.run(None, self._feed(ids, mask))[0]
        except Exception as exc:
            raise ProcessorError(
                ErrorClass.STRUCTURE_CHANGED,
                f"model {self._manifest.identity} could not run: {exc}",
            ) from exc
        return [self._pool(hidden[row], mask[row]) for row in range(len(encodings))]

    def _feed(self, ids: Any, mask: Any) -> dict[str, Any]:
        """The tensors this graph asks for, built from the two it always needs.

        Some exports also declare `token_type_ids` -- BERT-shaped graphs, which
        is what the real e5 export is. e5 does not use them, and every reference
        pipeline feeds zeros, so zeros is what an export that asks for them gets.
        The fixture declares two inputs and so never exercised this: the third
        one only appeared at the first inference against real weights, which is
        the argument for running one in CI rather than trusting the stand-in.
        """
        feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        return feed

    def _pool(self, hidden: Any, mask: Any) -> list[float]:
        """Masked mean over the tokens, then L2 normalisation."""
        weights = mask.astype(np.float32)
        total = float(weights.sum())
        if total <= 0:
            raise ProcessorError(
                ErrorClass.PARSE, "the text produced no tokens to pool"
            )
        pooled = (hidden * weights[:, None]).sum(axis=0) / total
        produced = int(pooled.shape[0])
        if produced != self._manifest.dimensions:
            raise ProcessorError(
                ErrorClass.STRUCTURE_CHANGED,
                f"model {self._manifest.identity} produced {produced} dimensions, "
                f"the manifest declares {self._manifest.dimensions}",
            )
        norm = float(np.linalg.norm(pooled))
        if norm == 0.0:
            # Refused rather than returned as zeros. A zero vector dots to zero
            # against every query, so it would rank last forever and look like a
            # relevance problem; the honest reading is that this model produced
            # nothing usable for this text, and that is a failure worth recording.
            raise ProcessorError(
                ErrorClass.PARSE,
                f"model {self._manifest.identity} produced a zero vector",
            )
        return [float(value) for value in pooled / norm]

    def _ensure_loaded(self) -> tuple[Any, Any]:
        """Load the session and tokenizer once per encoder."""
        if self._session is not None:
            return self._session, self._tokenizer

        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_path = self._directory / self._manifest.model_file
        tokenizer_path = self._directory / self._manifest.tokenizer_file
        for path in (model_path, tokenizer_path):
            if not path.is_file():
                raise ProcessorError(
                    ErrorClass.STRUCTURE_CHANGED, f"missing model file {path}"
                )

        # A corrupt or foreign file must leave as a classified failure, not as
        # whatever the runtime raises: the worker's retry policy and the
        # operator's diagnostics read `error_class`, and an unclassified
        # exception is the one shape that reaches neither (AC-104).
        try:
            session = ort.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"]
            )
        except Exception as exc:
            raise ProcessorError(
                ErrorClass.STRUCTURE_CHANGED,
                f"{model_path} could not be loaded as an ONNX model: {exc}",
            ) from exc
        try:
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        except Exception as exc:
            raise ProcessorError(
                ErrorClass.STRUCTURE_CHANGED,
                f"{tokenizer_path} could not be loaded as a tokenizer: {exc}",
            ) from exc

        available = {item.name for item in session.get_inputs()}
        self._inputs = frozenset(available)
        missing = [name for name in REQUIRED_INPUTS if name not in available]
        if missing:
            raise ProcessorError(
                ErrorClass.STRUCTURE_CHANGED,
                f"{model_path} does not take {missing}; it takes {sorted(available)}",
            )

        self._session = session
        return self._session, self._tokenizer
