"""The child environment allowlist (P2b hardening).

A sandboxed child must not be able to read the host's whole environment:
each collector sees the base runtime variables and its own credential
prefixes, and nothing else.
"""

from __future__ import annotations

import pytest

from app.plugin_runtime import child_environment


@pytest.mark.unit
def test_a_child_sees_its_own_credentials_and_the_shared_cookiecloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PEARSS_ZHIHU_COOKIES", "d_c0=x")
    monkeypatch.setenv("PEARSS_COOKIECLOUD_TOKEN", "t")
    monkeypatch.setenv("PEARSS_X_AUTH_TOKEN", "x-token")
    monkeypatch.setenv("PEARSS_API_TOKEN", "host-write-token")

    env = child_environment("zhihu")

    assert env["PEARSS_ZHIHU_COOKIES"] == "d_c0=x"
    assert env["PEARSS_COOKIECLOUD_TOKEN"] == "t"
    assert "PEARSS_X_AUTH_TOKEN" not in env, "another collector's session leaked"
    assert "PEARSS_API_TOKEN" not in env, "a host secret leaked"


@pytest.mark.unit
def test_base_runtime_variables_survive_the_narrowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/op")
    monkeypatch.setenv("PEARSS_TICK_SECONDS", "60")

    env = child_environment("twitter")

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/op"
    assert "PEARSS_TICK_SECONDS" not in env, (
        "host-side operational config is not a collector credential"
    )
    assert env["PEARSS_SANDBOXED"] == "1"


@pytest.mark.unit
def test_an_unknown_collector_gets_only_the_base_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PEARSS_ZSXQ_ACCESS_TOKEN", "t")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = child_environment("no-such-collector")

    assert env["PATH"] == "/usr/bin"
    assert "PEARSS_ZSXQ_ACCESS_TOKEN" not in env


@pytest.mark.unit
def test_pythonpath_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child is spawned as `python -m plugins.collectors`, so it needs the
    path to the code.

    This was a real deployment failure: with the source tree installed into the
    interpreter (the development venv), the child found `plugins` through the
    venv's `.pth` and the omission was invisible. The packaged deployment
    (`nix/package.nix`, `--no-install-project`) reaches the code only through
    PYTHONPATH, so without this the child died at import with ModuleNotFoundError
    and every sandboxed collector reported a crash.
    """
    monkeypatch.setenv("PYTHONPATH", "/opt/pearss/src")

    env = child_environment("telegram")

    assert env["PYTHONPATH"] == "/opt/pearss/src"
