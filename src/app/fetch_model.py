"""Fetch the embedding model: pinned URLs, verified hashes, a manifest.

The model is ~135 MB, which is why it is not in the repository and why this
exists. A deployment -- and CI, which verifies the semantics that need the real
weights -- fetches it once into the directory the encoder reads, and every byte
is checked against a hash pinned here.

That check is not hygiene. A model's *identity* decides whether a stored vector
may be reused (ADR 0035 decision 6), so a download that quietly produced
different weights would be a correctness failure rather than an inconvenience:
the vectors would come from a model the row's key does not name, and nothing
downstream could tell. The hash is the only thing that notices.

Quantised on purpose: fp32 is 470 MB against 118 MB for int8, and the vectors
differ only in the last digits. The revision string records which one it is, so a
later switch re-keys every product rather than mixing the two spaces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from adapters.embedding_model import (
    MANIFEST_NAME,
    ModelManifest,
    load_manifest,
    model_directory,
)
from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError

LOGGER = logging.getLogger(__name__)

# The upstream revision every URL below is pinned to, plus the variant: a
# revision alone would not distinguish the quantised export from the fp32 one,
# and the two do not produce interchangeable vectors.
PINNED_REVISION: Final = "761b726dd34fb83930e26aab4e9ac3899aa1fa78-int8"

# e5-family models are trained with these prefixes on the two sides of a
# retrieval. Using the wrong one degrades ranking while looking perfectly
# healthy, which is why they travel with the model instead of being a caller's
# decision.
QUERY_PREFIX: Final = "query: "
PASSAGE_PREFIX: Final = "passage: "

DEFAULT_ENDPOINT: Final = "https://huggingface.co"

_UPSTREAM_REVISION: Final = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
_REPO_PATH: Final = "/Xenova/multilingual-e5-small/resolve"
_MODEL_PATH: Final = f"{_REPO_PATH}/{_UPSTREAM_REVISION}/onnx/model_quantized.onnx"
_TOKENIZER_PATH: Final = f"{_REPO_PATH}/{_UPSTREAM_REVISION}/tokenizer.json"

# A generous ceiling per artifact. The pinned size is the real bound; this only
# stops a redirect to something that would stream forever.
MAX_ARTIFACT_BYTES: Final = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PinnedFile:
    """One artifact: where it comes from, what it must hash to, where it goes."""

    name: str
    url: str
    sha256: str
    size: int


def pinned_files(endpoint: str | None = None) -> tuple[PinnedFile, ...]:
    """The artifacts to install, resolved against a Hugging Face endpoint.

    `HF_ENDPOINT` is the variable the Hugging Face tooling already reads, so a
    deployment that mirrors the hub gets the mirror here too, and CI sets it
    explicitly for the same reason it points uv at a mirror: the international
    route is slow and periodically unusable from this host (measured: pypi.org
    took 4.8s for a HEAD, which cost a run 40 of its 60 minutes).

    Serving these bytes from somewhere else is safe, and the reason is the pin:
    it is a **hash**, not a host. A mirror that served different weights would
    fail the check and install nothing, so the only thing a mirror can change is
    how fast the correct bytes arrive.
    """
    base = (endpoint or os.environ.get("HF_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
    return (
        PinnedFile(
            name="model.onnx",
            url=f"{base}{_MODEL_PATH}",
            sha256="f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
            size=118_308_185,
        ),
        PinnedFile(
            name="tokenizer.json",
            url=f"{base}{_TOKENIZER_PATH}",
            sha256="0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
            size=17_082_730,
        ),
    )


PINNED_FILES: Final[tuple[PinnedFile, ...]] = pinned_files()


def fetch(
    directory: Path | None = None,
    *,
    files: Sequence[PinnedFile] | None = None,
    client: httpx.Client | None = None,
) -> ModelManifest:
    """Download and verify every pinned file, then write the manifest.

    Idempotent by content: a file that already matches its pinned hash and size
    is left alone, so re-running costs nothing and a half-finished download is
    repaired rather than trusted.
    """
    artifacts = pinned_files() if files is None else files
    target_dir = directory or model_directory()
    target_dir.mkdir(parents=True, exist_ok=True)
    owned = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(60.0, read=600.0), follow_redirects=True
    )
    try:
        for pinned in artifacts:
            target = target_dir / pinned.name
            if _matches(target, pinned):
                LOGGER.info("%s already matches the pinned artifact", pinned.name)
                continue
            _download(http, pinned, target)
        _write_manifest(target_dir)
    finally:
        if owned:
            http.close()
    return load_manifest(target_dir)


def _matches(path: Path, pinned: PinnedFile) -> bool:
    """Whether the file on disk is already exactly the pinned artifact."""
    try:
        if path.stat().st_size != pinned.size:
            return False
        return _digest(path) == pinned.sha256
    except OSError:
        return False


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(http: httpx.Client, pinned: PinnedFile, target: Path) -> None:
    """Stream one artifact into place, or refuse and leave nothing behind.

    The bytes land in a temporary file beside the destination and are renamed
    only after the hash agrees. Without that, an interrupted or tampered download
    would leave a file for the next run to check -- whose check would be of this
    attempt's leftovers rather than of the artifact.
    """
    LOGGER.info("fetching %s (%d bytes)", pinned.name, pinned.size)
    digest = hashlib.sha256()
    written = 0
    descriptor, name = tempfile.mkstemp(prefix=f".{pinned.name}.", dir=target.parent)
    temporary = Path(name)
    try:
        try:
            with http.stream("GET", pinned.url) as response:
                response.raise_for_status()
                with os.fdopen(descriptor, "wb") as handle:
                    for chunk in response.iter_bytes():
                        written += len(chunk)
                        if written > MAX_ARTIFACT_BYTES:
                            raise ProcessorError(
                                ErrorClass.NETWORK,
                                f"{pinned.name} exceeded {MAX_ARTIFACT_BYTES} bytes",
                            )
                        digest.update(chunk)
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            raise ProcessorError(
                ErrorClass.NETWORK, f"could not fetch {pinned.name}: {exc}"
            ) from exc

        if written != pinned.size or digest.hexdigest() != pinned.sha256:
            raise ProcessorError(
                ErrorClass.STRUCTURE_CHANGED,
                f"{pinned.name} does not match the pinned artifact "
                f"({written} bytes, sha256 {digest.hexdigest()[:12]})",
            )
        os.replace(temporary, target)
    finally:
        # Only this writer's temporary file, including on every failure above.
        temporary.unlink(missing_ok=True)


def _write_manifest(directory: Path) -> None:
    """Describe the model that is now on disk, for the encoder and for the key."""
    manifest = {
        "name": "multilingual-e5-small",
        "dimensions": 384,
        "revision": PINNED_REVISION,
        "model_file": "model.onnx",
        "tokenizer_file": "tokenizer.json",
        "query_prefix": QUERY_PREFIX,
        "passage_prefix": PASSAGE_PREFIX,
    }
    path = directory / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """`python -m app.fetch_model [directory]`."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    arguments = sys.argv[1:] if argv is None else argv
    directory = Path(arguments[0]) if arguments else None
    try:
        manifest = fetch(directory)
    except ProcessorError as exc:
        print(f"fetch-model failed: {exc.detail}", file=sys.stderr)
        return 1
    print(f"model ready: {manifest.identity} ({manifest.model_file})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
