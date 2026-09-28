"""Fixed-width UTC timestamps: the one place this format is defined.

The invariant (AGENTS.md): timestamps are stored and compared as
fixed-width RFC 3339 UTC text. `isoformat()` is not equivalent — it omits
the fractional part when microseconds happen to be zero, and a column
mixing `...T00:00:00Z` with `...T00:00:00.500000Z` sorts the latter
*before* the former ('.' is 0x2E, 'Z' is 0x5A). Every ORDER BY and every
keyset cursor in this project relies on six fractional digits always
being present, so this format has exactly one definition and every
writer imports it. A hand-rolled copy elsewhere is how the invariant
silently broke once already.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["EPOCH_FLOOR", "fixed_width_utc"]

# The floor a dateless item sorts at. Not a substitute for NULL: `published_at`
# stays nullable because "this item has no date" is a fact worth keeping, and
# this value exists only to give those rows a stable position in the feed. It
# lives here because it is part of the format -- it has to be fixed-width text
# in exactly this shape for its comparisons to be the same comparisons.
EPOCH_FLOOR = "0000-01-01T00:00:00.000000Z"


def fixed_width_utc(moment: datetime) -> str:
    """Format `moment` as fixed-width RFC 3339 UTC text (see module doc)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
