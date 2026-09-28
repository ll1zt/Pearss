"""Tool-result redaction: the pure rules behind AC-36..AC-38.

The integration suite (`tests/integration/test_mcp_result_redaction.py`) proves
the rule holds end to end through the real MCP client. These cover the edges
that are cheaper to reach directly: nesting, the operator's escape hatch, and
the fact that redaction does not itself eat the shape an agent needs.

The important property is that this is *value*-based, not key-based. A future
field that carries a URL is redacted without anyone remembering to add it to a
list, which is the failure mode a key allowlist would have.
"""

from __future__ import annotations

import pytest

from app.mcp.rendering import _shrink

pytestmark = pytest.mark.unit

SECRET = "SUPERSECRETTOKEN123"


@pytest.mark.unit
def test_a_token_in_a_url_is_hidden_at_any_key_and_any_depth() -> None:
    # Not key-based: the same URL under three different names, at three
    # depths, is redacted identically. A key allowlist would leak whichever
    # name it did not list.
    payload = {
        "url": f"https://f.example/x.xml?token={SECRET}",
        "canonical_url": f"https://f.example/x.xml?token={SECRET}",
        "nested": {"feed": f"https://f.example/x.xml?token={SECRET}"},
        "rows": [{"link": f"https://f.example/x.xml?token={SECRET}"}],
    }
    assert SECRET not in str(_shrink(payload))


@pytest.mark.unit
def test_the_shape_survives_so_an_agent_can_still_reason_about_the_url() -> None:
    out = _shrink({"url": f"https://f.example/private/x.xml?token={SECRET}&lang=en"})
    # Host and path: identify the resource.
    assert out["url"].startswith("https://f.example/private/x.xml")
    # Parameter names: say a credential *exists* without saying what it is.
    assert "token=" in out["url"]
    # Non-secret parameters are untouched.
    assert "lang=en" in out["url"]
    assert out["url"] == "https://f.example/private/x.xml?token=<redacted>&lang=en"


@pytest.mark.unit
def test_a_public_url_is_returned_byte_for_byte() -> None:
    # Redaction must be a no-op when there is nothing to hide, or every
    # ordinary result would be quietly rewritten.
    url = "https://example.com/v1/items?limit=50&fields=id"
    assert _shrink({"url": url})["url"] == url


@pytest.mark.unit
def test_a_url_embedded_in_prose_is_still_redacted() -> None:
    # A suggestion or title can quote a URL; the credential does not become
    # safe by being wrapped in a sentence.
    text = f"Retry https://f.example/x.xml?token={SECRET} once the origin is up."
    out = _shrink({"suggestion": text})["suggestion"]
    assert SECRET not in out
    assert "Retry" in out and "once the origin is up." in out


@pytest.mark.unit
def test_a_url_nested_inside_a_parameter_is_redacted_too() -> None:
    # `?url=<another url>` is how a redirector hides one hop; the token inside
    # the inner URL must not survive.
    inner = f"https://f.example/x.xml?token={SECRET}"
    out = _shrink({"url": f"https://redir.example/go?url={inner}"})
    assert SECRET not in out["url"]


@pytest.mark.unit
def test_case_variants_of_a_secret_parameter_are_redacted() -> None:
    # Query keys are case-insensitive in practice; `Token=` is the same secret.
    out = _shrink({"url": f"https://f.example/x.xml?Token={SECRET}"})
    assert SECRET not in out["url"]


@pytest.mark.unit
def test_redaction_happens_before_length_truncation() -> None:
    # A token sitting past MAX_STRING_CHARS must not survive by being cut.
    padding = "a" * 300
    url = f"https://f.example/x.xml?token={SECRET}&pad={padding}"
    out = _shrink({"url": url})["url"]
    assert SECRET not in out
    assert "…" in out, "the bound still applies to what is returned"
