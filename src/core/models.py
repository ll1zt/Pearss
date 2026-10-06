"""Core domain structures.

These are deliberately free of ORM and framework machinery: they are the
shapes that docs/domain/data-model.md describes, expressed as frozen
dataclasses so they can be constructed in tests without a database and passed
across the layer boundary without leaking SQLAlchemy state into core.

Persistence lives in adapters/; only this module decides what an ItemDraft or
a schedule policy *means*.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.enums import ErrorClass, PublishStatus, SourceStatus, SourceType, TrustLevel


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemDraft:
    """The universal pre-normalisation shape every ingest path produces.

    RSS, web collectors and the future ingest API all converge here before
    normalisation, which is what keeps one dedupe rule and one publish gate
    covering every source.
    """

    origin_id: str
    # Stable identifier assigned by the origin. Absent only when the origin
    # provides none, in which case `derivation_note` must explain how it was
    # synthesised so repeated fetches agree.
    external_id: str
    title: str
    canonical_url: str
    published_at: datetime | None
    content: str
    raw_fetch_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    # Required when `external_id` is derived rather than supplied.
    derivation_note: str | None = None

    def __post_init__(self) -> None:
        # Validation rather than assertion: these run under `python -O` in
        # production, where an assert would vanish.
        if not self.canonical_url:
            raise ValueError("ItemDraft.canonical_url must not be empty")
        if not self.derivation_note and not self.external_id:
            raise ValueError(
                "ItemDraft needs either an external_id or a derivation_note "
                "explaining how it was derived"
            )

    def as_rpc_dict(self) -> dict[str, Any]:
        """The wire form for the plugin RPC (P2b): JSON-safe fields only.

        `published_at` serialises verbatim: a naive datetime encodes without
        an offset and rebuilds naive. (Applying `.astimezone(UTC)` here would
        interpret naive values in the host's local zone and shift every
        zone-less feed timestamp across the RPC — a silent divergence from
        the in-process path, where the draft is the raw ingest boundary and
        zone-less values are recorded as evidence, never guessed.)
        """
        return {
            "origin_id": self.origin_id,
            "external_id": self.external_id,
            "title": self.title,
            "canonical_url": self.canonical_url,
            "published_at": (
                self.published_at.isoformat() if self.published_at is not None else None
            ),
            "content": self.content,
            "raw_fetch_id": self.raw_fetch_id,
            "metadata": dict(self.metadata),
            "derivation_note": self.derivation_note,
        }

    @classmethod
    def from_rpc_dict(cls, data: dict[str, Any]) -> ItemDraft:
        """Rebuild a draft from `as_rpc_dict` output."""
        published = data.get("published_at")
        return cls(
            origin_id=str(data["origin_id"]),
            external_id=str(data["external_id"]),
            title=str(data["title"]),
            canonical_url=str(data["canonical_url"]),
            published_at=(
                datetime.fromisoformat(str(published))
                if published is not None
                else None
            ),
            content=str(data["content"]),
            raw_fetch_id=data.get("raw_fetch_id"),
            metadata={str(k): str(v) for k, v in (data.get("metadata") or {}).items()},
            derivation_note=data.get("derivation_note"),
        )
        # published_at is deliberately NOT required to carry a zone: a draft is
        # the raw ingest boundary, and a great deal of real feed content omits
        # it. docs/guides/collector-guide.md says to record that evidence
        # rather than guess from local time — so the draft accepts the value as
        # found and normalize() attaches UTC. That is the recorded evidence.


@dataclass(frozen=True, slots=True, kw_only=True)
class SchedulePolicy:
    """Per-source scheduling decision: fixed interval or adaptive.

    Section 4 of the architecture spec: there is one scheduler and one policy
    per source. Adaptive sources compute their interval from fetch history via
    `adaptive_interval`, a pure function of the observed gaps.
    """

    type: str  # "fixed" | "adaptive"
    # Fixed: the interval in seconds.
    interval_seconds: int | None = None
    # Adaptive: bounds that the computed interval is clamped to.
    min_seconds: int | None = None
    max_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.type == "fixed":
            if self.interval_seconds is None or self.interval_seconds <= 0:
                raise ValueError("fixed policy requires a positive interval_seconds")
        elif self.type == "adaptive":
            if self.min_seconds is None or self.max_seconds is None:
                raise ValueError("adaptive policy requires min_seconds and max_seconds")
            if self.min_seconds <= 0 or self.max_seconds <= self.min_seconds:
                raise ValueError(
                    "adaptive policy requires 0 < min_seconds <= max_seconds"
                )
        else:
            raise ValueError(f"unknown schedule policy type: {self.type!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Item:
    """A normalised content item.

    Publish status is a first-class field from P1 even though the processor
    chain that gates on it arrives in P3: retrofitting the gate after clients
    have cached unprocessed items is exactly the failure it exists to prevent.
    """

    id: str
    origin_id: str
    guid: str
    content_hash: str
    title: str
    url: str
    published_at: datetime | None
    publish_status: PublishStatus
    # Who wrote it, as the origin named them. Empty when the origin names
    # nobody: a feed without <author> is not an error, and inventing a name
    # from the source would misattribute the byline.
    author: str = ""
    tenant_id: str = "default"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    content_ref: str | None = None
    published_to_feed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must not be empty")
        for name, value in (
            ("published_at", self.published_at),
            ("published_to_feed_at", self.published_to_feed_at),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """A content source."""

    id: str
    type: SourceType
    url: str | None
    schedule_policy: SchedulePolicy
    status: SourceStatus
    canonical_url: str | None = None
    parser_plugin_id: str | None = None
    trust_level: TrustLevel = TrustLevel.STANDARD
    tenant_id: str = "default"


# --------------------------------------------------------------------------
# schedule_policy serialisation
#
# The JSON column's shape is domain structure: a malformed policy must be
# refused rather than half-accepted, and the scheduler downstream reads the
# parsed form. Keeping the conversion in core means both the API and the
# scheduler validate through the same path.
# --------------------------------------------------------------------------


def schedule_policy_from_json(raw: str | dict[str, object]) -> SchedulePolicy:
    """Parse a stored or supplied schedule policy.

    Accepts already-decoded mappings as well as text, because the API receives
    a JSON body while the database yields a text column.
    """
    data: object = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("schedule_policy must be a JSON object")

    allowed = {"type", "interval", "min", "max"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(
            f"schedule_policy has unknown keys {sorted(unknown)}; "
            f"allowed are {sorted(allowed)}"
        )

    kind = data.get("type")
    if not isinstance(kind, str):
        raise ValueError("schedule_policy requires a string 'type'")

    if kind == "fixed":
        interval = data.get("interval")
        return SchedulePolicy(type="fixed", interval_seconds=_seconds(interval))
    if kind == "adaptive":
        return SchedulePolicy(
            type="adaptive",
            min_seconds=_seconds(data.get("min")),
            max_seconds=_seconds(data.get("max")),
        )
    raise ValueError(
        f"schedule_policy type must be 'fixed' or 'adaptive', got {kind!r}"
    )


def schedule_policy_to_json(policy: SchedulePolicy) -> str:
    """Render a policy in the documented `{type, interval}` / `{type, min, max}`
    form."""
    if policy.type == "fixed":
        payload: dict[str, object] = {
            "type": "fixed",
            "interval": _format_seconds(policy.interval_seconds or 0),
        }
    else:
        payload = {
            "type": "adaptive",
            "min": _format_seconds(policy.min_seconds or 0),
            "max": _format_seconds(policy.max_seconds or 0),
        }
    return json.dumps(payload, sort_keys=True)


def _seconds(value: object) -> int:
    """Accept either a duration string like `30m` or a whole number of seconds."""
    if isinstance(value, bool):
        raise ValueError("duration must be a string like '30m' or seconds")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("duration must be positive")
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        for suffix, factor in (
            ("ms", 0.001),
            ("s", 1),
            ("m", 60),
            ("h", 3600),
            ("d", 86400),
        ):
            if text.endswith(suffix) and len(text) > len(suffix):
                number = text[: -len(suffix)].strip()
                try:
                    parsed = float(number)
                except ValueError as exc:
                    raise ValueError(f"invalid duration {value!r}") from exc
                seconds = int(parsed * factor)
                if seconds <= 0:
                    raise ValueError(f"duration must be positive, got {value!r}")
                return seconds
        raise ValueError(f"unrecognised duration {value!r}; expected e.g. '30m', '24h'")
    raise ValueError(
        f"duration must be a string or integer, got {type(value).__name__}"
    )


def _format_seconds(total: int) -> str:
    """Render seconds in the largest exact unit, matching the documented style."""
    for suffix, factor in (("d", 86400), ("h", 3600), ("m", 60)):
        if total % factor == 0 and total >= factor:
            return f"{total // factor}{suffix}"
    return f"{total}s"


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRunRecord:
    """A single execution record, used for diagnostics and retry linkage.

    Retries create a new record linked by `retry_of` rather than rewriting a
    terminal one, so the first failure stays available for diagnosis.
    """

    id: str
    job_id: str
    status: str
    correlation_id: str
    error_class: ErrorClass | None = None
    retry_of: str | None = None

    def __post_init__(self) -> None:
        if not self.correlation_id:
            raise ValueError("JobRunRecord.correlation_id is mandatory")
