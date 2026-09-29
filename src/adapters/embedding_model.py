"""Where the embedding model lives, and what it says about itself.

The manifest is read here rather than in the processor because two very different
places need the same answer and they sit on opposite sides of the layering:
`parse_chain` keys a row by the model identity *when the row is enqueued*, and
the encoder needs the weights, later and in a child process. Both read this, so
there is one description of the model and no chance of the row's key describing
something other than the file that produced the vector.

Reading a JSON file is I/O, which is why this is an adapter and not core -- and
why `ModelIdentity` (the domain object that goes into a key) stays in core.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from core.enrichment import ModelIdentity
from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError

# A model directory, laid out by whatever downloaded it. The default matches the
# service user's state directory; a deployment that keeps models elsewhere sets
# the variable rather than editing this.
DEFAULT_MODEL_DIR = "/var/lib/pearss/models/multilingual-e5-small"

MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """What a model directory says about itself.

    Written next to the weights when they are downloaded, and the source of the
    identity a product is keyed by -- so the directory cannot disagree with
    itself about which model it holds. That disagreement is what lets a vector
    from one model be served as if it came from another.
    """

    name: str
    dimensions: int
    revision: str
    model_file: str = "model.onnx"
    tokenizer_file: str = "tokenizer.json"
    query_prefix: str = ""
    passage_prefix: str = ""

    @property
    def identity(self) -> ModelIdentity:
        """The triple a product is keyed by (ADR 0035 decision 6)."""
        return ModelIdentity(
            name=self.name, dimensions=self.dimensions, revision=self.revision
        )


def model_directory() -> Path:
    """Where this deployment keeps its embedding model."""
    configured = os.environ.get("PEARSS_EMBEDDING_MODEL_DIR", DEFAULT_MODEL_DIR)
    return Path(configured).expanduser()


def load_manifest(directory: Path) -> ModelManifest:
    """Read a model directory's manifest, or refuse with a classified failure."""
    path = directory / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProcessorError(
            ErrorClass.STRUCTURE_CHANGED, f"no manifest at {path}"
        ) from exc
    except ValueError as exc:
        raise ProcessorError(
            ErrorClass.STRUCTURE_CHANGED, f"unreadable manifest at {path}: {exc}"
        ) from exc
    try:
        return ModelManifest(
            name=str(raw["name"]),
            dimensions=int(raw["dimensions"]),
            revision=str(raw["revision"]),
            model_file=str(raw.get("model_file", "model.onnx")),
            tokenizer_file=str(raw.get("tokenizer_file", "tokenizer.json")),
            query_prefix=str(raw.get("query_prefix", "")),
            passage_prefix=str(raw.get("passage_prefix", "")),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ProcessorError(
            ErrorClass.STRUCTURE_CHANGED, f"incomplete manifest at {path}: {exc}"
        ) from exc
