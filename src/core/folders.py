"""Subscription folders: one per source, in the FreshRSS model.

A folder is how the Reader API groups subscriptions in a client's sidebar
(`user/-/label/<name>` streams, one `categories` entry per subscription).
FreshRSS constrains a feed to a single category — its own code says "a feed
can only be in one category" — and clients are built to that shape, so the
model here is one folder per source, never many-to-many.

The names live in core because two layers handle them: the /v1 API validates
what an operator or agent sends, and the Reader API turns them into stream
ids. A single definition keeps those two agreeing about what "default" means
and what a legal name is.
"""

from __future__ import annotations

from typing import Final

DEFAULT_FOLDER: Final = "default"

MAX_FOLDER_LENGTH: Final = 64


def normalize_folder(value: str) -> str:
    """Clean and validate one folder name; raise ValueError when unusable.

    Whitespace outside is trimmed (a pasted name with a trailing newline is
    the common mistake), interior whitespace is preserved (multi-word names
    are normal), and control characters are refused (they cannot render in a
    sidebar and are never what a caller meant).

    `/` is refused because the name becomes a stream id
    (`user/-/label/<name>`), and a slash inside it produces a path no client
    can round-trip: the folder would appear under one name and answer under
    none. That encoding is the adapter's concern, so the check here is only
    about the slash; whatever prefix the adapter prepends contains one.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("a folder name must not be empty")
    if len(cleaned) > MAX_FOLDER_LENGTH:
        raise ValueError(f"a folder name is at most {MAX_FOLDER_LENGTH} characters")
    if any(not char.isprintable() for char in cleaned):
        raise ValueError("a folder name must not contain control characters")
    if "/" in cleaned:
        raise ValueError("a folder name must not contain '/'")
    return cleaned
