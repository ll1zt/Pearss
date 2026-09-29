"""One loop reporting another's death through the alert channel (issue #61).

A loop cannot report its own death: the code that would notice is the code that
stopped. So the two loops report each other's, and this holds the half that is
the same for both directions -- read the other's `liveness`, decide, and send
through one sender and one edge state. Writing it twice is how the two
directions would come to disagree about what they say or how often they say it.

What this does not solve, and what ADR 0039 writes down: if *both* loops die,
neither watch runs and the alert channel stays silent. The backstop is the
passive surface -- `/healthz` is answered by the HTTP layer, not by these loops
(ADR 0019) -- plus the unit's restart policy, and the runbook step that polls it.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from adapters.alerting import send_alert
from core.alerting import ALERT_DEFAULTS, AlertState, alert_body, decide_health

# An empty reading: this watch is about a loop, not about a success rate, and
# `decide_health` treats "too few runs to judge" as "no opinion" -- so the
# verdict here is decided entirely by the loop's liveness, which is the point.
_NO_RATE: dict[str, object] = {}


def always_alive() -> tuple[bool, str]:
    """The liveness of a loop that is not being watched.

    A loop that was handed nothing to watch reports on nothing: naming a loop it
    never checked would be a guess presented as a fact.
    """
    return True, ""


@dataclass(slots=True)
class LoopWatch:
    """Watches one other loop, and says so when it stops.

    `name` is *this* loop's name, used only to identify the watcher in a log
    line. The dead loop is named from its own `liveness` answer, so the message
    names the thing that stopped rather than the thing that noticed.
    """

    name: str
    watch: Callable[[], tuple[bool, str]]
    state: AlertState = field(default_factory=AlertState)

    def check(self) -> bool:
        """Send an alert (or a recovery) if the watched loop's state changed.

        Returns whether anything was sent, so a caller can assert on the
        behaviour rather than on a side effect it cannot see.
        """
        alive, watched_name = self.watch()
        verdict = decide_health(
            _NO_RATE,
            dead_loops=() if alive else (watched_name or self.name,),
            subject="background processing",
        )
        # The same rate limit the health pass honours, from the same variable:
        # a dead loop is worth saying once, not once per tick, and an operator
        # who tuned repeats for one condition meant it for both.
        min_interval = float(
            os.environ.get(
                "PEARSS_ALERT_MIN_INTERVAL_SECONDS",
                str(ALERT_DEFAULTS.min_interval_seconds),
            )
        )
        if not self.state.should_send(
            verdict, now=time.monotonic(), min_interval_seconds=min_interval
        ):
            return False
        return send_alert(
            alert_body(verdict),
            title=(
                "Pearss loop stopped" if verdict.degraded else "Pearss loop recovered"
            ),
            source=f"pearss@{socket.gethostname()}",
            severity="error" if verdict.degraded else "info",
        )
