"""Google Reader compatible API surface.

This is a compatibility layer. Everything Google-Reader-shaped lives here —
stream ids, `tag:google.com,2005:reader/item/...` forms, `T=`/`Authorization:`
plumbing — so none of it leaks into core/, per the boundary rule in
docs/api/style-guide.md. Internally this calls the same core services the
rest of the system uses.

Scope is what NetNewsWire's "FreshRSS account" type actually calls, derived
from the maintained reference implementation documented in
miniflux's internal/googlereader README, not a full Google Reader reissue.
See ADR 0004.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Stream ids
# --------------------------------------------------------------------------

READ_TAG = "user/-/state/com.google/read"
STARRED_TAG = "user/-/state/com.google/starred"
KEPT_UNREAD_TAG = "user/-/state/com.google/kept-unread"
READING_LIST = "user/-/state/com.google/reading-list"

# NNW asks for these; anything else is treated as unknown.
KNOWN_STATE_TAGS = frozenset({READ_TAG, STARRED_TAG, KEPT_UNREAD_TAG, READING_LIST})

_FEED_STREAM = re.compile(r"^feed/(?P<id>.+)$")
_STATE_STREAM = re.compile(r"^user/[^/]+/state/com\.google/(?P<state>[a-z-]+)$")
_USER_STATE_STREAM = re.compile(r"^user/-/state/com\.google/(?P<state>[a-z-]+)$")


def feed_stream_id(source_id: str) -> str:
    """Stream id for one of our sources."""
    return f"feed/{source_id}"


def parse_feed_stream_id(stream_id: str) -> str | None:
    """Extract our source id from a feed stream id, or None if not one."""
    match = _FEED_STREAM.match(stream_id)
    return match.group("id") if match else None


def is_state_stream(stream_id: str, *, state: str) -> bool:
    """Whether `stream_id` denotes the given built-in state."""
    return _normalise_state_stream(stream_id) == state


def _normalise_state_stream(stream_id: str) -> str | None:
    """Reduce a state stream id to its bare state name, or None."""
    for pattern in (_USER_STATE_STREAM, _STATE_STREAM):
        match = pattern.match(stream_id)
        if match:
            return match.group("state")
    return None


# --------------------------------------------------------------------------
# Item ids
# --------------------------------------------------------------------------

# Our item ids are 32 hex characters (128 bits). The historical Google Reader
# convention is 16, but truncating would both risk collisions and desynchronise
# the ids clients have already stored. ADR 0005 records this: we emit and
# accept our full 32-character ids, and NNW treats them as opaque.
ITEM_ID_LENGTH = 32
ITEM_ID_PATTERN = re.compile(rf"^(?P<id>[0-9a-fA-F]{{{ITEM_ID_LENGTH}}})$")

_LONG_FORM_PREFIX = "tag:google.com,2005:reader/item/"


def encode_item_id(item_id: str) -> str:
    """Render an item id in the long Google Reader form NNW expects."""
    return f"{_LONG_FORM_PREFIX}{item_id}"


def decode_item_id(raw: str) -> str | None:
    """Recover our item id from whatever form a client sent.

    Accepts the long form we emit, a bare id, and the short prefixed form
    other clients sometimes send. Returns None for anything unrecognised
    rather than raising: a malformed id is a client bug, and one bad entry
    should not fail a batch that contains valid ones.
    """
    candidate = raw.strip()
    if candidate.startswith(_LONG_FORM_PREFIX):
        candidate = candidate.removeprefix(_LONG_FORM_PREFIX)
    # A short prefixed form may carry leading zeros; normalise to our width.
    match = ITEM_ID_PATTERN.match(candidate)
    if match:
        return match.group("id").lower()
    return None


# --------------------------------------------------------------------------
# Tag changes
# --------------------------------------------------------------------------


def classify_tags(
    *, added: list[str], removed: list[str]
) -> tuple[bool | None, bool | None]:
    """Map Google Reader tag streams onto (is_read, is_starred).

    Returns None for a flag the request did not mention, so calling this
    cannot turn an absent instruction into a state change.

    Raises ValueError on a genuinely contradictory request, which is a client
    bug worth surfacing rather than resolving arbitrarily.
    """
    is_read: bool | None = None
    is_starred: bool | None = None

    for tag in added:
        state = _normalise_state_stream(tag)
        if state == "read":
            is_read = True
        elif state == "kept-unread":
            is_read = False
        elif state == "starred":
            is_starred = True

    for tag in removed:
        state = _normalise_state_stream(tag)
        if state == "read":
            is_read = False
        elif state == "kept-unread":
            is_read = True
        elif state == "starred":
            is_starred = False

    # Google Reader treats these as mutually exclusive within one request.
    added_states = {_normalise_state_stream(tag) for tag in added}
    removed_states = {_normalise_state_stream(tag) for tag in removed}
    if "starred" in added_states and "starred" in removed_states:
        raise ValueError("starred cannot be both added and removed")
    if "read" in added_states and "kept-unread" in added_states:
        raise ValueError("read and kept-unread cannot both be added")

    return is_read, is_starred
