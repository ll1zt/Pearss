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

__all__ = ["fixed_width_utc"]


def fixed_width_utc(moment: datetime) -> str:
    """Format `moment` as fixed-width RFC 3339 UTC text (see module doc)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
