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

_LABEL_PREFIXES = ("user/-/label/", "user/%s/label/")

_FEED_STREAM = re.compile(r"^feed/(?P<id>.+)$")
_STATE_STREAM = re.compile(r"^user/[^/]+/state/com\.google/(?P<state>[a-z-]+)$")
_USER_STATE_STREAM = re.compile(r"^user/-/state/com\.google/(?P<state>[a-z-]+)$")


def feed_stream_id(source_id: str) -> str:
    """Stream id for one of our sources."""
    return f"feed/{source_id}"


def folder_stream_id(name: str) -> str:
    """Stream id for a subscription folder ("user/-/label/<name>")."""
    return f"user/-/label/{name}"


def parse_feed_stream_id(stream_id: str) -> str | None:
    """Extract our source id from a feed stream id, or None if not one."""
    match = _FEED_STREAM.match(stream_id)
    return match.group("id") if match else None


def state_stream_name(stream_id: str) -> str | None:
    """The bare state name a stream id denotes, or None if it is not a state stream.

    `user/-/state/com.google/starred` and `com.google/starred` both reduce to
    `starred`. Callers branch on this so an unrecognised state can yield nothing
    rather than falling through to "everything".
    """
    return _normalise_state_stream(stream_id)


def label_stream_name(stream_id: str) -> str | None:
    """The folder a label stream denotes, or None when it is not one.

    `user/-/label/Work` -> `Work`. Whether the folder exists is the caller's
    question: a name no source carries resolves to an empty stream, which is
    the honest answer for a filter with no matches.
    """
    for prefix in ("user/-/label/", "user/%s/label/"):
        head = stream_id[: len(prefix)]
        if head == prefix:
            name = stream_id[len(prefix) :].strip()
            return name or None
    return None


def is_state_stream(stream_id: str, *, state: str) -> bool:
    """Whether `stream_id` denotes the given built-in state."""
    return _normalise_state_stream(stream_id) == state


# The states a tag filter can name, mapped to the read-state column that
# carries them. `read` is inverted at the call site (`is_read` false means
# unread) and `kept-unread` is the same column read the other way -- Google
# Reader's `kept-unread` marks an item the user deliberately left unread, and
# we model that as "not read", which is what the column already says.
TAG_STATE_COLUMNS = {"read": "is_read", "starred": "is_starred"}


def tag_states(stream_ids: list[str]) -> list[str]:
    """The state names among a list of tag stream ids, in order, deduped.

    Used for `it=`/`xt=` on `stream/items/ids`, where a client names states
    rather than streams. Unrecognised ids are dropped rather than rejected:
    a client sending a state we do not model (say `com.google/like`) should
    get the items matching the states we *do* model, or nothing -- but never
    an error, and never the unfiltered list. The caller decides which of
    those two emptiness rules applies based on whether any state survived.

    `kept-unread` maps to `read` because our model has one boolean: an item
    is either read or not, and `kept-unread` (in Remove) means "not read".
    """
    seen: list[str] = []
    for stream_id in stream_ids:
        state = _normalise_state_stream(stream_id)
        if state == "kept-unread":
            state = "read"
        if state in TAG_STATE_COLUMNS and state not in seen:
            seen.append(state)
    return seen



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

# --------------------------------------------------------------------------
# Item ids
# --------------------------------------------------------------------------
#
# NetNewsWire does `Int(articleID)!` on the values from stream/items/ids, so
# those must be decimal strings that fit a 64-bit signed Int. A 128-bit id
# cannot be parsed and NNW silently drops every entry — the subscription shows
# up but no articles do. Verified against NNW's ReaderAPICaller.swift.
#
# So the Reader API exposes a *narrowed* external id: the first 15 hex digits
# of the internal id (60 bits), which leaves headroom under Int64. The mapping
# is deterministic, so a client's stored id keeps resolving to the same item
# across restarts and rebuilds.
#
# Collision risk: 60 bits means two items collide at roughly the birthday
# bound, ~2^30 (about a billion) items for a 50% chance. A single-user feed
# reader will not approach that; ADR 0006 records the trade-off.
READER_ID_HEX_DIGITS = 15

_LONG_FORM_PREFIX = "tag:google.com,2005:reader/item/"
_HEX = re.compile(r"^(?P<hex>[0-9a-fA-F]{1,16})$")


def reader_id(item_id: str) -> int:
    """The external numeric id for an internal item id."""
    return int(item_id[:READER_ID_HEX_DIGITS], 16)


def reader_id_hex(external: int) -> str:
    """The hex prefix an external id corresponds to, for reverse lookup."""
    return format(external, f"0{READER_ID_HEX_DIGITS}x")


def encode_item_id(external: int) -> str:
    """Render an external id in the long form the contents response expects.

    Zero-padded to 16 hex digits, matching what edit-tag sends
    (`%.16llx` in NNW's Swift). The padding is contract, not decoration:
    ReadYou validates a long-form id by `length == 16` and filters out every
    article that fails it, so an unpadded 15-digit tail -- which parses fine
    everywhere -- leaves that client a subscription list with no articles.
    NNW reads the tail back with `Int(radix: 16)`, which padding does not
    disturb.
    """
    return f"{_LONG_FORM_PREFIX}{external:016x}"


def parse_item_id_param(raw: str) -> int | None:
    """Recover the external id from whatever form a client sent.

    NetNewsWire uses three different shapes, all of which must be accepted:

    - decimal, as handed back from stream/items/ids
    - `tag:google.com,2005:reader/item/<hex>`, zero padded to 16 by edit-tag
    - the same long form unpadded, which is what stream/items/contents sends

    Returns None for anything unrecognised rather than raising: a malformed id
    is a client bug, and one bad entry should not fail a batch of valid ones.
    """
    candidate = raw.strip()
    # The prefix decides the base, and that has to be checked before looking at
    # the characters: a hex value can be all digits (0x3039 is 12345), so
    # testing isdigit() after stripping the prefix would read it as decimal and
    # silently resolve to the wrong item.
    if candidate.startswith(_LONG_FORM_PREFIX):
        hex_part = candidate.removeprefix(_LONG_FORM_PREFIX).strip()
        match = _HEX.match(hex_part)
        if not match:
            return None
        return int(match.group("hex"), 16)

    if candidate.isdigit():
        return int(candidate, 10)

    # A bare hex value is not part of the NetNewsWire contract, but other
    # Google Reader clients send it, so accept it when it is unambiguous.
    match = _HEX.match(candidate)
    if not match:
        return None
    return int(match.group("hex"), 16)


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
