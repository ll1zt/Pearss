"""Sending an alert through the deployment's notify gateway (ADR 0034).

The gateway (see `host-config`'s docs/manuals/notify-gateway.md) is the one place
that holds a Telegram token and the one place that handles retries, backoff and
rate limiting. pearss therefore posts **plain text** to it and holds no
credentials of its own: a second service holding the bot token would be a second
copy of a secret in a second backup, for no gain.

The contract this module owes its caller is narrow, and the narrowness is the
point (ADR 0034 decision 6): **a notification is a side channel**. Sending must
never raise into the scheduler, never block it for long, and never turn an
alerting problem into a collection problem. Every failure here is a log line.

Unconfigured is a valid state, not an error: `PEARSS_ALERT_URL` (or the
deployment's `NOTIFY_URL`) empty means this deployment has chosen not to receive
alerts, and the first time we notice we say so once at warning level so it is
discoverable in the journal without being a recurring complaint.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import httpx

LOGGER = logging.getLogger(__name__)

# The gateway's own variable, so a deployment that already injects it for other
# services needs no second setting. `PEARSS_ALERT_URL` wins when set, which lets
# pearss point somewhere else without touching the shared value.
URL_ENV: Final = "PEARSS_ALERT_URL"
GATEWAY_URL_ENV: Final = "NOTIFY_URL"
TIMEOUT_ENV: Final = "PEARSS_ALERT_TIMEOUT_SECONDS"

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
# The gateway caps a body at 4093 characters; a longer one is a 400 there. Ours
# are a handful of lines, so this is a backstop against a future body growing
# silently past the limit and being rejected in the one place we cannot see.
MAX_BODY_CHARS: Final = 4093

_UNCONFIGURED_WARNED = False


def alert_url() -> str:
    """The configured notify endpoint, or "" when alerts are switched off."""
    for name in (URL_ENV, GATEWAY_URL_ENV):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def alert_timeout_seconds() -> float:
    """The per-request timeout, with a bounded default.

    Short on purpose: this runs inside a scheduler tick, and an unreachable
    gateway must not hold the tick open. The gateway's own retry policy (the
    `notify` CLI's NOTIFY_RETRIES/BACKOFF) is what makes delivery reliable; this
    timeout only bounds *our* wait.
    """
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        LOGGER.warning(
            "%s=%r is not a number; using %s", TIMEOUT_ENV, raw, DEFAULT_TIMEOUT_SECONDS
        )
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        LOGGER.warning(
            "%s=%r is not positive; using %s", TIMEOUT_ENV, raw, DEFAULT_TIMEOUT_SECONDS
        )
        return DEFAULT_TIMEOUT_SECONDS
    return value


def send_alert(
    body: str,
    *,
    title: str,
    source: str,
    severity: str = "warn",
    client: httpx.Client | None = None,
) -> bool:
    """Post one alert; return whether the gateway accepted it.

    Never raises: every failure mode (unconfigured, unreachable, refused,
    timed out, a body the gateway rejects) is a log line and a `False`. The
    caller is a scheduler tick, and ADR 0034 decision 6 makes this explicit --
    an alert that cannot be delivered must not become a collection outage.

    `202` means *queued* at the gateway, not delivered to a human; that is the
    gateway's contract and is stated here so nobody later reads `True` as proof
    of delivery.
    """
    url = alert_url()
    if not url:
        _warn_unconfigured_once()
        return False

    text = body[:MAX_BODY_CHARS]
    payload = {
        "text": text,
        "title": title,
        "source": source,
        "severity": severity,
    }
    try:
        if client is not None:
            response = client.post(url, json=payload, timeout=alert_timeout_seconds())
        else:
            response = httpx.post(url, json=payload, timeout=alert_timeout_seconds())
    except httpx.HTTPError as exc:
        LOGGER.warning("alert not delivered (%s): %s", type(exc).__name__, exc)
        return False
    except Exception as exc:  # a side channel never propagates
        LOGGER.warning("alert not delivered (%s): %s", type(exc).__name__, exc)
        return False

    if 200 <= response.status_code < 300:
        return True
    LOGGER.warning("alert rejected by the gateway: HTTP %s", response.status_code)
    return False


def _warn_unconfigured_once() -> None:
    """Say once that alerts are off, so the journal explains the silence.

    Once per process on purpose: the alternative is a warning every tick, which
    is its own version of the noise ADR 0034 decision 2 exists to avoid.
    """
    global _UNCONFIGURED_WARNED
    if _UNCONFIGURED_WARNED:
        return
    _UNCONFIGURED_WARNED = True
    LOGGER.warning(
        "no alert endpoint configured (%s or %s); alerts are disabled",
        URL_ENV,
        GATEWAY_URL_ENV,
    )
