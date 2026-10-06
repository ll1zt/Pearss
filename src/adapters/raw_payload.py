"""Raw payload from a fetch.

Deliberately free of httpx types so that parsing stays testable without a
network client: collectors receive this, never a `Response`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from core.enums import ErrorClass


@dataclass(frozen=True, slots=True, kw_only=True)
class RawPayload:
    """The result of one fetch attempt.

    `body` is in memory because it is bounded by `max_bytes` at the adapter
    boundary; anything larger is rejected rather than stored. Callers persist
    it via the file store and keep only the reference.
    """

    url: str
    status_code: int
    body: bytes
    # Which origin this fetch belongs to. Carried here so parsing can stamp it
    # onto every draft without the collector needing database access.
    origin_id: str = ""
    # The RawFetch row this payload is stored under, once persisted.
    raw_fetch_id: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # Set when the origin reported the resource unchanged (HTTP 304).
    not_modified: bool = False
    # Conditional-request state to replay on the next fetch.
    etag: str | None = None
    last_modified: str | None = None
    # Populated only on failure; None means the fetch succeeded.
    error_class: ErrorClass | None = None
    error_detail: str | None = None
    correlation_id: str = ""

    @property
    def ok(self) -> bool:
        """Whether this payload can be handed to a parser."""
        return self.error_class is None and not self.not_modified and self.body != b""

    def as_rpc_dict(self) -> dict[str, Any]:
        """The wire form for the plugin RPC (P2b): fields the child needs."""
        return {
            "url": self.url,
            "status_code": self.status_code,
            "body": base64.b64encode(self.body).decode("ascii"),
            "origin_id": self.origin_id,
            "raw_fetch_id": self.raw_fetch_id,
            "headers": dict(self.headers),
            "not_modified": self.not_modified,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "error_class": (
                self.error_class.value if self.error_class is not None else None
            ),
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_rpc_dict(cls, data: dict[str, Any]) -> RawPayload:
        """Rebuild a payload from `as_rpc_dict` output."""
        return cls(
            url=str(data["url"]),
            status_code=int(data["status_code"]),
            body=base64.b64decode(str(data["body"])),
            origin_id=str(data.get("origin_id", "")),
            raw_fetch_id=data.get("raw_fetch_id"),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            not_modified=bool(data.get("not_modified", False)),
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            error_class=(
                ErrorClass(str(data["error_class"]))
                if data.get("error_class")
                else None
            ),
            correlation_id=str(data.get("correlation_id", "")),
        )

    def text(self, encoding: str | None = None) -> str:
        """Decode the body.

        Falls back to the charset declared in Content-Type, then to UTF-8 with
        replacement. Feed content in the wild declares its encoding
        incorrectly often enough that a hard failure here would drop otherwise
        usable feeds, so undecodable bytes are replaced rather than raising.
        """
        charset = encoding or _charset_from_content_type(
            self.headers.get("content-type", "")
        )
        return self.body.decode(charset or "utf-8", errors="replace")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConditionalState:
    """Conditional-request state carried between fetches of one source.

    Lives with RawPayload rather than with the HTTP client so that a caller
    which only reads stored state — the repository, for instance — does not
    have to import the network layer to name the type.
    """

    etag: str | None = None
    last_modified: str | None = None


def _charset_from_content_type(content_type: str) -> str | None:
    for part in content_type.split(";"):
        candidate = part.strip().lower()
        if candidate.startswith("charset="):
            return candidate.removeprefix("charset=").strip("\"'") or None
    return None
