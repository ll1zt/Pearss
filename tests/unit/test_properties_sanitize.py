"""Property tests for the HTML sanitiser (the security boundary).

`sanitize_html` is the one function whose single missed vector is a security
incident: every stored body passes through it exactly once, and every output
path renders whatever it returns. Example-based tests enumerate the vectors we
thought of; these properties assert what must hold for *every* input, which is
the actual requirement for an attacker-controlled boundary.

The properties are chosen to be decidable on the output string alone:

- execution/track vectors never survive as emitted markup
- sanitising is idempotent, so a double-sanitised pipeline cannot reintroduce
  danger (and a later re-sanitise of stored bodies stays safe)
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from adapters.sanitize import sanitize_html

pytestmark = pytest.mark.unit

# Text drawn from a nasty-but-realistic alphabet: quotes, brackets, entities,
# scheme-like fragments, attribute-shaped noise. Pure `st.text()` spends its
# budget on filler that never stresses a parser, so the strategy leans toward
# the characters that make HTML parsing interesting.
_NASTY_ALPHABET = (
    "abcXYZ </>&'\"=;:()!#%$@[]{}`~^*+-_,.?\\\n\r\t"
    "javascript: data: vbscript: onclick onerror srcdoc &#x6A; &#106; &lt; &gt;"
)
_NASTY = st.text(alphabet=_NASTY_ALPHABET, max_size=200)

# Fragments that look like real attack payloads when concatenated.
_PAYLOAD_PARTS = st.sampled_from(
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<a href='javascript:alert(1)'>x</a>",
        "<a href='JaVaScRiPt:alert(1)'>x</a>",
        "<a href='&#106;avascript:alert(1)'>x</a>",
        "<iframe srcdoc='<script>alert(1)</script>'></iframe>",
        "<svg onload=alert(1)>",
        "<body onload=alert(1)>",
        "<form action='javascript:alert(1)'><input></form>",
        "<object data='javascript:alert(1)'>",
        "<embed src='data:text/html,<script>alert(1)</script>'>",
        "<!--<script>alert(1)</script>-->",
        "<style>body{background:url(javascript:alert(1))}</style>",
        "<div onclick='alert(1)'>x</div>",
        "<math><mtext></mtext></math>",
        "<base href='javascript:'>",
        "<meta http-equiv='refresh' content='0;url=javascript:'>",
        "<template><script>alert(1)</script></template>",
        "<textarea><script>alert(1)</script></textarea>",
        "<![CDATA[<script>alert(1)</script>]]>",
    ]
)
_PAYLOAD = st.lists(_PAYLOAD_PARTS, max_size=6).map("".join)


_FORBIDDEN_TAG = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|form|base|meta|style|svg|math|template)\b",
    re.IGNORECASE,
)
_EVENT_HANDLER = re.compile(r"\son[a-z0-9_-]+\s*=", re.IGNORECASE)
_SCRIPT_SCHEME_ATTR = re.compile(
    r"(href|src|action|data|srcdoc|poster)\s*=\s*[\"']?\s*(javascript|vbscript)\s*:",
    re.IGNORECASE,
)


def _assert_inert(out: str) -> None:
    assert _FORBIDDEN_TAG.search(out) is None, repr(out)
    assert _EVENT_HANDLER.search(out) is None, repr(out)
    assert _SCRIPT_SCHEME_ATTR.search(out) is None, repr(out)


@settings(max_examples=300)
@given(st.one_of(_NASTY, _PAYLOAD, st.text(max_size=300)))
def test_output_never_contains_an_execution_or_tracking_vector(html: str) -> None:
    _assert_inert(sanitize_html(html))


@settings(max_examples=200)
@given(st.one_of(_NASTY, _PAYLOAD, st.text(max_size=300)))
def test_sanitising_is_idempotent(html: str) -> None:
    """A body sanitised twice equals a body sanitised once.

    Stored bodies are sanitised at ingest; if any output path ever sanitises
    again (a re-export, a migration re-clean), the second pass must be a
    no-op -- a sanitiser that "stabilises" only after two passes is one where
    the first pass left something the second pass re-interpreted as markup.
    """
    once = sanitize_html(html)
    twice = sanitize_html(once)

    assert twice == once, (repr(html), repr(once), repr(twice))


@settings(max_examples=100)
@given(_PAYLOAD)
def test_payload_built_from_known_vectors_is_inert(html: str) -> None:
    """The concatenated known-vector corpus is inert, in every combination.

    Vectors interact: a payload harmless alone can close a context another one
    opened. Concatenating the corpus exercises those interactions, which
    single-vector example tests structurally cannot.
    """
    _assert_inert(sanitize_html(html))
