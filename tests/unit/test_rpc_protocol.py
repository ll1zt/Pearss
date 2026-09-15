"""The RPC wire parsers, unit-level.

`decode_request` / `decode_message` / `decode_response` are the boundary a
sandboxed child process speaks through. Every branch that accepts or rejects
a byte sequence is a security-relevant decision (the host must not be confused
into treating an attacker-influenced child message as a host instruction, or
vice versa), and the mutation baseline showed most of these branches had no
unit-level test -- the crash-recovery integration tests exercise only the
happy path.
"""

from __future__ import annotations

import json

import pytest

from core.plugins.protocol import (
    ProtocolError,
    Request,
    Response,
    ReverseCall,
    decode_message,
    decode_request,
    decode_response,
    error_result,
)

pytestmark = pytest.mark.unit


def _line(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


# --------------------------------------------------------------------------
# decode_request (host -> child)
# --------------------------------------------------------------------------


def test_a_valid_request_decodes() -> None:
    request = decode_request(
        _line({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}})
    )

    assert request == Request(id=7, method="ping", params={})


@pytest.mark.parametrize(
    "payload",
    [
        b"not json at all",
        b"\xff\xfe garbage",
        b"[1, 2, 3]",
        b'"a string"',
        b"null",
        b'{"jsonrpc": "1.0", "id": 1, "method": "ping"}',
        b'{"jsonrpc": "2.0", "id": 1, "method": "no-such-method"}',
        b'{"jsonrpc": "2.0", "id": "seven", "method": "ping"}',
        b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}',
        b'{"jsonrpc": "2.0", "method": "ping"}',
    ],
)
def test_a_malformed_request_is_refused(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_request(payload)


# --------------------------------------------------------------------------
# decode_response (child -> host)
# --------------------------------------------------------------------------


def test_a_result_response_decodes() -> None:
    response = decode_response(_line({"jsonrpc": "2.0", "id": 3, "result": {"a": 1}}))

    assert response.ok
    assert response.id == 3
    assert response.result == {"a": 1}
    assert response.error is None


def test_an_error_response_decodes() -> None:
    response = decode_response(
        _line({"jsonrpc": "2.0", "id": 3, "error": {"code": 5, "message": "no"}})
    )

    assert not response.ok
    assert response.error == {"code": 5, "message": "no"}


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"42",
        b'"x"',
        b'{"jsonrpc": "2.0", "id": 1.5, "result": null}',
        b'{"jsonrpc": "2.0", "result": null}',
        b'{"jsonrpc": "2.0", "id": 1, "error": "a string"}',
    ],
)
def test_a_malformed_response_is_refused(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_response(payload)


def test_a_null_result_is_a_valid_response() -> None:
    response = decode_response(_line({"jsonrpc": "2.0", "id": 1, "result": None}))

    assert response.ok
    assert response.result is None


# --------------------------------------------------------------------------
# decode_message (child -> host routing)
# --------------------------------------------------------------------------


def test_a_reverse_call_is_recognised_as_a_request_shape() -> None:
    message = decode_message(
        _line({"jsonrpc": "2.0", "id": 0, "method": "fetch", "params": {"url": "u"}})
    )

    assert isinstance(message, ReverseCall)
    assert message.method == "fetch"
    assert message.params == {"url": "u"}


def test_an_unknown_reverse_call_method_is_refused() -> None:
    with pytest.raises(ProtocolError, match="unknown method"):
        decode_message(
            _line({"jsonrpc": "2.0", "id": 0, "method": "system.exec", "params": {}})
        )


def test_a_reverse_call_without_an_object_params_is_refused() -> None:
    with pytest.raises(ProtocolError, match="params"):
        decode_message(
            _line({"jsonrpc": "2.0", "id": 0, "method": "fetch", "params": "url"})
        )


def test_a_message_carrying_a_result_is_a_response_even_with_a_method() -> None:
    """`method` + `result` is a response: the routing rule is the key set.

    If this accepted it as a reverse call, a child could smuggle a host
    instruction inside a response-shaped message.
    """
    message = decode_message(
        _line({"jsonrpc": "2.0", "id": 0, "method": "ping", "result": {}})
    )

    assert isinstance(message, Response)


def test_an_error_carrying_message_is_a_response() -> None:
    message = decode_message(
        _line(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "ping",
                "error": {"code": 1, "message": "m"},
            }
        )
    )

    assert isinstance(message, Response) and not message.ok


@pytest.mark.parametrize("payload", [b"", b"garbage", b"[]"])
def test_undecodable_messages_are_refused(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_message(payload)


# --------------------------------------------------------------------------
# Response.encode: exactly one of result / error, and it round-trips
# --------------------------------------------------------------------------


def test_an_encoded_result_round_trips_without_an_error_key() -> None:
    encoded = json.loads(Response(id=1, result={"ok": True}).encode())

    assert "error" not in encoded
    assert encoded["result"] == {"ok": True}
    assert decode_response(json.dumps(encoded).encode()).result == {"ok": True}


def test_an_encoded_error_round_trips_without_a_result_key() -> None:
    encoded = json.loads(Response(id=1, error={"code": 2, "message": "m"}).encode())

    assert "result" not in encoded
    assert decode_response(json.dumps(encoded).encode()).error == {
        "code": 2,
        "message": "m",
    }


def test_encode_is_utf8_with_unicode_preserved() -> None:
    encoded = Response(id=1, result={"text": "汉[data]字"}).encode().decode("utf-8")

    assert "汉[data]字" in json.loads(encoded)["result"]["text"]


# --------------------------------------------------------------------------
# error_result: the error object the child puts on the wire
#
# A classified collector failure travels as a *result* carrying this shape, and
# the host reads `code` to decide the ErrorClass. It is therefore a wire
# contract between two processes, and a renamed key would not raise anywhere:
# the host would read `None` for the class and record an unclassified failure.
# --------------------------------------------------------------------------


def test_the_error_result_uses_the_agreed_key_names() -> None:
    assert error_result("blocked", "403 from origin") == {
        "code": "blocked",
        "message": "403 from origin",
    }


def test_an_error_result_round_trips_as_a_response() -> None:
    """The child sends it inside a Response; the host must read it back.

    Asserted through the encoder and decoder rather than the dict alone,
    because the keys have to survive JSON and the response wrapper both.
    """
    line = Response(id=7, error=error_result("auth", "token rejected")).encode()

    decoded = decode_response(line)
    assert decoded.ok is False
    assert decoded.error == {"code": "auth", "message": "token rejected"}


def test_a_decoded_error_result_is_not_an_ok_response() -> None:
    """`ok` keys off `error` being present, which is what routes a failure.

    A child reporting a 403 must not be read as a successful fetch returning a
    dictionary -- the host would store the error object as content.
    """
    decoded = decode_response(
        _line({"jsonrpc": "2.0", "id": 1, "error": error_result("blocked", "x")})
    )

    assert decoded.ok is False
    assert decoded.error is not None


def test_an_error_code_is_not_mistaken_for_a_missing_one() -> None:
    """An empty code is still a code.

    The host distinguishes "classified as <x>" from "no classification"; an
    empty string must reach it as an empty string rather than being dropped,
    which is what a truthiness check on the way out would do.
    """
    assert error_result("", "") == {"code": "", "message": ""}
    assert "code" in error_result("", "")
