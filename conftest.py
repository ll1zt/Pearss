"""Root conftest: hermetic DNS for the fetch layer's private-address guard.

The SSRF guard (ADR 0016) classifies fetch targets by resolving their hosts.
The test suite runs without a DNS guarantee — and must not depend on the
network — so the default resolver answers with a public address for every
name. The SSRF-specific tests override `resolve_host` explicitly with the
addresses they exercise.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import http as pearss_http

    monkeypatch.setattr(pearss_http, "resolve_host", lambda host: {"93.184.216.34"})
