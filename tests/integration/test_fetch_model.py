"""The downloader verifies what it downloads, and leaves nothing behind if not.

Scenario register (normative source: ADR 0035 §9; issue #82).

A real HTTP origin serves the bytes, because the thing under test is a download:
streaming, hashing and the atomic rename are exactly what a stand-in for the
transport would remove. The pinned list is a parameter rather than a patched
module constant, so these never touch the network or the 118 MB artifact.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.fetch_model import PinnedFile, fetch, pinned_files
from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError

pytestmark = pytest.mark.integration

MODEL_BYTES = b"a tiny stand-in for 118 MB of weights"
TOKENIZER_BYTES = b'{"model": "stand-in"}'


class _Origin:
    """A real HTTP origin that serves pinned bytes, or something else."""

    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {
            "/model.onnx": MODEL_BYTES,
            "/tokenizer.json": TOKENIZER_BYTES,
        }
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = outer.payloads.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def origin() -> Iterator[_Origin]:
    one = _Origin()
    yield one
    one.close()


def _pinned(origin: _Origin, path: str, body: bytes) -> PinnedFile:
    return PinnedFile(
        name=path.lstrip("/"),
        url=f"{origin.base}{path}",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )


def _files(origin: _Origin) -> tuple[PinnedFile, ...]:
    return (
        _pinned(origin, "/model.onnx", MODEL_BYTES),
        _pinned(origin, "/tokenizer.json", TOKENIZER_BYTES),
    )


def test_a_matching_artifact_is_installed_and_described(
    origin: _Origin, tmp_path: Path
) -> None:
    """AC: the bytes land, and the manifest says which model they are."""
    manifest = fetch(
        tmp_path, files=_files(origin), client=httpx.Client(follow_redirects=True)
    )

    assert (tmp_path / "model.onnx").read_bytes() == MODEL_BYTES
    assert (tmp_path / "tokenizer.json").read_bytes() == TOKENIZER_BYTES
    assert manifest.identity.name == "multilingual-e5-small"
    assert manifest.identity.dimensions == 384
    assert (manifest.query_prefix, manifest.passage_prefix) == ("query: ", "passage: ")


def test_a_tampered_artifact_is_refused_and_leaves_nothing(
    origin: _Origin, tmp_path: Path
) -> None:
    """The hash is the only thing between a wrong model and a silently wrong key.

    And the refusal has to leave the directory as it was: a half-written or
    mismatched file that stayed would be picked up by the next run, which checks
    the hash -- of a file this attempt wrote.
    """
    good = _pinned(origin, "/model.onnx", MODEL_BYTES)
    origin.payloads["/model.onnx"] = b"weights someone else produced"

    with pytest.raises(ProcessorError) as failure:
        fetch(tmp_path, files=(good,), client=httpx.Client(follow_redirects=True))

    assert failure.value.error_class is ErrorClass.STRUCTURE_CHANGED
    assert "does not match the pinned artifact" in failure.value.detail
    assert not (tmp_path / "model.onnx").exists()
    assert list(tmp_path.iterdir()) == [], list(tmp_path.iterdir())


def test_an_already_installed_artifact_is_not_downloaded_again(
    origin: _Origin, tmp_path: Path
) -> None:
    """Idempotent by content: the second run must not rewrite what is already right.

    Asserted on the file's own timestamp rather than on a request count: what
    matters is that the bytes were not touched, not how the transport was used.
    """
    files = _files(origin)
    client = httpx.Client(follow_redirects=True)
    fetch(tmp_path, files=files, client=client)
    before = (tmp_path / "model.onnx").stat().st_mtime_ns

    fetch(tmp_path, files=files, client=client)

    assert (tmp_path / "model.onnx").stat().st_mtime_ns == before


def test_a_missing_artifact_is_restored(origin: _Origin, tmp_path: Path) -> None:
    """A partially populated directory is repaired, not trusted."""
    files = _files(origin)
    client = httpx.Client(follow_redirects=True)
    fetch(tmp_path, files=files, client=client)
    (tmp_path / "model.onnx").unlink()

    fetch(tmp_path, files=files, client=client)

    assert (tmp_path / "model.onnx").read_bytes() == MODEL_BYTES


def test_an_unreachable_origin_is_a_network_failure(tmp_path: Path) -> None:
    """AC-104 for the download: the class says the network, not the artifact."""
    unreachable = PinnedFile(
        name="model.onnx",
        url="http://127.0.0.1:9/model.onnx",
        sha256="0" * 64,
        size=1,
    )

    with pytest.raises(ProcessorError) as failure:
        fetch(tmp_path, files=(unreachable,))

    assert failure.value.error_class is ErrorClass.NETWORK


def test_a_mirror_changes_where_the_bytes_come_from_and_nothing_else() -> None:
    """The endpoint is a knob; the pin is not.

    This is what makes pointing the fetch at a mirror safe: the same two
    artifacts are accepted, with the same hashes and sizes, whichever host
    serves them. A mirror cannot substitute weights -- it can only fail to serve
    the ones that were pinned.
    """
    canonical = pinned_files("https://huggingface.co")
    mirrored = pinned_files("https://hf-mirror.com/")

    assert [one.url for one in mirrored] != [one.url for one in canonical]
    assert all(one.url.startswith("https://hf-mirror.com/") for one in mirrored)
    assert [(one.name, one.sha256, one.size) for one in mirrored] == [
        (one.name, one.sha256, one.size) for one in canonical
    ]


def test_the_endpoint_follows_hf_endpoint_and_defaults_to_the_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that already mirrors the hub gets the mirror here too."""
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    assert pinned_files()[0].url.startswith("https://huggingface.co/")

    monkeypatch.setenv("HF_ENDPOINT", "https://mirror.example/")
    assert pinned_files()[0].url.startswith("https://mirror.example/")
