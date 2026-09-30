"""Real embedding weights: "cannot look" is the same answer as "not there".

`PEARSS_EMBEDDING_MODEL_DIR` may point at a directory this process cannot read.
On a multi-user host that is the normal case: the service's copy is owned by
another account, so `(directory / "manifest.json").is_file()` raises
`PermissionError` for whoever runs the suite -- and four tests that document
themselves as skipping instead failed (issue #179).

A caller that needs real weights can only skip, whether the directory is
absent, holds no manifest, or cannot be looked into. Deciding that once here
keeps the three sites (`test_embedding_semantics.py`, `test_vector_search.py`)
from each answering it slightly differently.
"""

from __future__ import annotations

import json
from pathlib import Path

from adapters.embedding_model import model_directory


def configured_path() -> Path:
    """Where the weights are looked for, whether or not they can be read.

    The deployment's own resolution (`PEARSS_EMBEDDING_MODEL_DIR`, else the
    default) rather than a second copy of it: a skip message that names a
    different path than the code reads is worse than no message.
    """
    return model_directory()


def readable_weights(directory: Path | None = None) -> Path | None:
    """`directory` (or the configured one) when readable weights live there.

    None means every way a caller can fail to find them: not there, no
    manifest, or no permission to look.
    """
    target = configured_path() if directory is None else directory
    try:
        present = (target / "manifest.json").is_file()
    except OSError:
        return None
    return target if present else None


def declared_name(directory: Path) -> str | None:
    """The name a directory's manifest declares, or None when it cannot be read.

    Used to tell the commit-time stand-in from real weights; an unreadable
    manifest is "not the real model" rather than an exception, for the same
    reason as above.
    """
    try:
        declared = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(declared, dict):
        return None
    name = declared.get("name")
    return name if isinstance(name, str) else None
