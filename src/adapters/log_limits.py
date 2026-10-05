"""Rate limiting for log lines a misbehaving path could emit per request.

Issue #297's rule: INFO/WARNING must be event-driven and O(1) per job or
failure, never per poll, tick or item. A path that cannot meet that -- a fetch
that keeps failing, a client retrying a rejected request -- should say so once
per window and then summarise what it suppressed, rather than either flooding
the journal or going silent.

The summary carries the level of the lines it summarises, deliberately: a
DEBUG-only path whose summary was a WARNING would leak into the default level
and defeat the rule it exists to enforce.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

DEFAULT_WINDOW_SECONDS = 60.0
# Bound on tracked keys. The table exists to collapse a flood, so it must not
# itself grow without limit; past the bound the oldest key is forgotten, which
# starts a fresh window for it -- one extra line, never a lost one.
MAX_TRACKED_KEYS = 1024


class RateLimitedLogger:
    """One line per key per window, plus a summary of what was dropped."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self._logger = logger
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max(1, max_keys)
        self._lock = threading.Lock()
        # key -> (window_start, suppressed, level), ordered by last use so the
        # bound can drop the least recently seen key in O(1). The previous
        # min()-over-the-table was O(max_keys) on exactly the flood path this
        # class exists to collapse, and raised on an empty table when
        # max_keys was 0 (issue #315).
        self._state: OrderedDict[str, tuple[float, int, int]] = OrderedDict()

    def __call__(
        self, level: int, key: str, message: str, *args: Any, **kwargs: Any
    ) -> None:
        """Log `message` unless `key` already spoke this window.

        The first call in a window logs; the rest are counted. When the window
        has elapsed, the pending summary is emitted before the new line, so a
        reader sees "N were suppressed" next to the line that resumes.
        """
        now = self._clock()
        summary: str | None = None
        summary_level = level
        emit = False
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                self._remember(key, now, level)
                emit = True
            else:
                start, suppressed, summary_level = entry
                if now - start < self._window:
                    self._state[key] = (start, suppressed + 1, level)
                    self._state.move_to_end(key)
                else:
                    if suppressed:
                        summary = self._summary(key, suppressed)
                    self._remember(key, now, level)
                    emit = True
        if summary is not None:
            self._logger.log(summary_level, summary)
        if emit:
            self._logger.log(level, message, *args, **kwargs)

    def flush(self) -> None:
        """Emit the pending summaries now (shutdown, or a test's end)."""
        with self._lock:
            pending = [
                (key, suppressed, level)
                for key, (_start, suppressed, level) in self._state.items()
                if suppressed
            ]
            for key, _suppressed, level in pending:
                start, _suppressed, _level = self._state[key]
                self._state[key] = (start, 0, level)
        for key, suppressed, level in pending:
            self._logger.log(level, self._summary(key, suppressed))

    def _remember(self, key: str, now: float, level: int) -> None:
        if key in self._state:
            self._state.move_to_end(key)
        elif len(self._state) >= self._max_keys:
            # last=False drops the least recently used key. Forgetting one
            # costs a single extra line next time it speaks, never a lost one.
            self._state.popitem(last=False)
        self._state[key] = (now, 0, level)

    def _summary(self, key: str, suppressed: int) -> str:
        return f"{key}: suppressed {suppressed} line(s) in the last {self._window:g}s"


def rate_limited_logger(
    logger: logging.Logger,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    max_keys: int = MAX_TRACKED_KEYS,
) -> RateLimitedLogger:
    """Build a per-key rate limiter over `logger` (see `RateLimitedLogger`)."""
    return RateLimitedLogger(
        logger, window_seconds=window_seconds, clock=clock, max_keys=max_keys
    )
