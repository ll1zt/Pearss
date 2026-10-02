"""Root conftest: hermetic DNS for the fetch layer's private-address guard.

The SSRF guard (ADR 0016) classifies fetch targets by resolving their hosts.
The test suite runs without a DNS guarantee — and must not depend on the
network — so the default resolver answers with a public address for every
name. The SSRF-specific tests override `resolve_host` explicitly with the
addresses they exercise.
"""

from __future__ import annotations

import json

import pytest

# The answer every host resolves to under test. A public address, so the SSRF
# guard classifies it as reachable; the SSRF-specific tests override
# `resolve_host` themselves with the addresses they exercise.
_HERMETIC_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def _hermetic_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapters import http as pearss_http

    # The deadline kwarg exists so production can bound the resolver wait
    # (A09); the hermetic answer ignores it, as resolution is instant here.
    monkeypatch.setattr(
        pearss_http, "resolve_host", lambda host, *, deadline=None: {_HERMETIC_ADDRESS}
    )
    # A sandboxed collector runs in a separate interpreter, where the patch
    # above does not reach: it resolves for itself. Without this the child
    # answered real DNS, `t.me` came back as a private address, and every
    # sandboxed collector's fixture test failed as `blocked`. The variable is
    # read by resolve_host in the child and is on the child env allowlist.
    monkeypatch.setenv("PEARSS_TEST_DNS", json.dumps({"*": [_HERMETIC_ADDRESS]}))


@pytest.fixture(autouse=True)
def _fresh_model_health() -> None:
    """The model-health registry is process-wide, so a test must not inherit it.

    The worker is one process for a whole deployment, which is exactly why the
    registry is not per-row (ADR 0048: a processor's child cannot outlive its
    row). The suite runs many deployments in one process, so each test starts
    from a clean one -- without this, a test that makes a model fail leaves the
    next test's chain cooling down and answering nothing.
    """
    from app.model_health import reset_health

    reset_health()
