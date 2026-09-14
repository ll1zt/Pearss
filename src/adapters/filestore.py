"""Content-addressed body storage.

The data model requires large payloads to live outside the database, with only
a reference stored (docs/domain/data-model.md). This is the file-backed
implementation; P3 may swap in MinIO, and the `cas://` scheme on the reference
is what lets a stored row say which backend produced it.

Content-addressing means the reference is derived from the bytes, so two items
with identical bodies share one file, and a stored body can never be silently
rewritten — which matters because a published item's content is frozen.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path

LOGGER = logging.getLogger(__name__)

REF_SCHEME = "cas://"
DEFAULT_ROOT = "./data/storage"

# A reference is the scheme plus 64 lowercase hex characters. Validating the
# whole thing rather than stripping the scheme means a malformed or hostile
# reference cannot be turned into a path.
_REF_PATTERN = re.compile(r"^cas://(?P<digest>[0-9a-f]{64})$")

# Refuse to serve a body larger than this. Bodies are already bounded at fetch
# time, so this is a backstop against a corrupt or hand-edited store.
MAX_BODY_BYTES = 16 * 1024 * 1024


def storage_root() -> Path:
    """Where bodies live. Configurable so a deployment can point elsewhere."""
    return Path(os.environ.get("PEARSS_STORAGE_DIR", DEFAULT_ROOT)).expanduser()


def store_body(content: str, *, root: Path | None = None) -> str:
    """Write `content` and return its reference.

    Writing is atomic: the payload lands in a temporary file beside its final
    path and is then renamed, so a crash mid-write cannot leave a truncated
    body that a later reader would serve as if complete.

    Existing files are left alone rather than rewritten. Same content means
    same bytes, so the write is redundant, and skipping it also avoids a
    concurrent writer racing on an object that is already correct.
    """
    base = root or storage_root()
    data = content.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    target = base / digest[:2] / digest

    if target.exists():
        return f"{REF_SCHEME}{digest}"

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    except OSError:
        # A failed write must not leave a partial file behind for the next
        # process to mistake for a complete body.
        temporary.unlink(missing_ok=True)
        raise

    return f"{REF_SCHEME}{digest}"


def read_body(ref: str | None, *, root: Path | None = None) -> str:
    """Return the body a reference points at, or "" when it cannot be read.

    Returning empty rather than raising is deliberate: one missing or corrupt
    body must not take down an entire feed. The caller still has the title and
    link, and the item is logged so the gap is visible.
    """
    if not ref:
        return ""

    match = _REF_PATTERN.match(ref)
    if match is None:
        LOGGER.warning("ignoring unrecognised content_ref: %r", ref.split("://", 1)[0])
        return ""

    digest = match.group("digest")
    path = (root or storage_root()) / digest[:2] / digest
    if not path.is_file():
        LOGGER.warning("content_ref points at a missing file: %s", ref)
        return ""

    try:
        data = path.read_bytes()
    except OSError as exc:
        LOGGER.warning("could not read %s: %s", ref, exc.__class__.__name__)
        return ""

    if len(data) > MAX_BODY_BYTES:
        LOGGER.warning("refusing oversized body at %s", ref)
        return ""

    # errors="replace": the bytes came from a remote feed whose declared
    # encoding is frequently wrong, and one undecodable byte should not blank
    # the whole body.
    return data.decode("utf-8", errors="replace")


def path_for(ref: str, *, root: Path | None = None) -> Path | None:
    """Resolve a `cas://<digest>` reference to the file it names.

    Returns None for anything that is not a well-formed reference. The digest
    is validated by the same pattern `read_body` uses rather than pasted into a
    path: a hand-edited or hostile reference must not be able to name a file
    outside the store, and a caller that deletes files needs that guarantee
    more than a reader does.
    """
    match = _REF_PATTERN.match(ref)
    if match is None:
        return None
    digest = match.group("digest")
    return (root or storage_root()) / digest[:2] / digest


def remove_body(ref: str, *, root: Path | None = None) -> bool:
    """Delete the file `ref` names. Returns whether a file was removed.

    Unconditional: this does not know what references the body, and no caller
    should delete a shared body without checking first. Use it only where the
    reference count is already known to be zero -- `repository.deletion`
    owns the counting.
    """
    path = path_for(ref, root=root)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        LOGGER.warning("could not remove %s: %s", ref, exc.__class__.__name__)
        return False
    return True


def bodies_for(
    refs: Mapping[str, str | None], *, root: Path | None = None
) -> dict[str, str]:
    """Resolve a mapping of key -> content_ref into key -> body.

    Entries with no reference are omitted rather than mapped to "", so a caller
    can tell "no body stored" from "body is the empty string".
    """
    resolved: dict[str, str] = {}
    for key, ref in refs.items():
        if not ref:
            continue
        body = read_body(ref, root=root)
        if body:
            resolved[key] = body
    return resolved
