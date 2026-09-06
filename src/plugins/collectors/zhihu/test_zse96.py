"""Vectors for the x-zse-96 port.

Generated with RSSHub's `x-zse-96-v3.ts` and a fixed `Math.random()` byte —
the same vectors the reference `earss_source_zhihu` (Elixir) port pins. They
make this module's correctness a comparison against the real browser
behaviour rather than an internal consistency check.
"""

from __future__ import annotations

import pytest

from plugins.collectors.zhihu import zse96

# (api_path, dc0, rand_byte, expected md5 of "101_3_3.0+path+dc0", expected signature)
VECTORS = [
    (
        "/api/v4/members/diygod/answers?limit=7&include=data[*].is_normal,content",
        "AATestDc0Value==",
        63,
        "156020f5542218e304539fd104c47d05",
        "m3KY1FUyGhKFxHO14Rv0qf=vX0RxkB8HXq7F+bFaLuDQWYcKudJBXZLNn8lOtxoc",
    ),
    (
        "/api/v4/members/diygod/answers?limit=7&include=data[*].is_normal,content",
        "AATestDc0Value==",
        0,
        "156020f5542218e304539fd104c47d05",
        "M+MtQZtCV=mpLmoiOhf/t0lGmv9ePp1xJ+hmodUad8IJj4rjcwTRH+U2lBC82MHP",
    ),
    (
        "/api/v3/moments/diygod/activities?limit=5&desktop=true&ws_qiangzhisafe=0",
        "d_c0_sample",
        31,
        "af4b38643fc3fe94039245aef202cf73",
        "kDcNmLino2g=nap3Kg9n2QOdraU5XgpY3m876FdeAm9sodhlDl7a5ly93Gx99Ts+",
    ),
    (
        "/api/v4/members/frederchen/articles?offset=0&limit=20&sort_by=created",
        "abc",
        12,
        "3ea70de167a18352a00eb67b907ea487",
        "ZsB/AsHF2ICeuAFJic/JgyIdoLhYD4IBM2XNpxv/Vx3CbUzRbkydoVX2uf7rsVlS",
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize("api_path,dc0,rand_byte,md5,signature", VECTORS)
def test_md5_of_the_sign_payload_matches_the_vectors(
    api_path: str, dc0: str, rand_byte: int, md5: str, signature: str
) -> None:
    assert zse96.md5_hex(f"{zse96.XZSE93}+{api_path}+{dc0}") == md5


@pytest.mark.unit
@pytest.mark.parametrize("api_path,dc0,rand_byte,md5,signature", VECTORS)
def test_encrypt_with_a_fixed_random_byte_matches_the_vectors(
    api_path: str, dc0: str, rand_byte: int, md5: str, signature: str
) -> None:
    assert zse96.encrypt(md5, rand_byte=rand_byte) == signature
    assert zse96.sign(api_path, dc0, rand_byte=rand_byte) == signature
    assert zse96.header_value(api_path, dc0, rand_byte=rand_byte) == f"2.0_{signature}"


@pytest.mark.unit
def test_random_signatures_stay_in_the_format() -> None:
    # Without an injected byte the leading random is 0..126; the output shape
    # must not depend on which value was drawn.
    for _ in range(20):
        value = zse96.header_value("/api/v4/me", "some-dc0")
        assert value.startswith("2.0_")
        assert len(value) == 64 + len("2.0_")


@pytest.mark.unit
def test_out_of_range_random_byte_is_refused() -> None:
    with pytest.raises(ValueError, match="rand_byte"):
        zse96.encrypt("0" * 32, rand_byte=127)


@pytest.mark.unit
def test_signed_headers_shape() -> None:
    headers = zse96.signed_headers(
        "/api/v4/me", dc0="x", cookie="d_c0=x; z_c0=y", user_agent="TestUA"
    )
    assert headers["cookie"] == "d_c0=x; z_c0=y"
    assert headers["x-zse-93"] == zse96.XZSE93
    assert headers["x-zse-96"].startswith("2.0_")
    assert headers["x-app-za"] == "OS=Web"
    assert headers["x-api-version"] == "3.0.91"
    assert headers["user-agent"] == "TestUA"


@pytest.mark.unit
def test_signed_headers_default_to_a_dc0_only_cookie() -> None:
    headers = zse96.signed_headers("/api/v4/me", dc0="abc")
    assert headers["cookie"] == "d_c0=abc"


@pytest.mark.unit
def test_signed_headers_require_dc0() -> None:
    with pytest.raises(ValueError, match="d_c0"):
        zse96.signed_headers("/api/v4/me", dc0="")
