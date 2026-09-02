"""Core domain structures.

These are deliberately free of ORM and framework machinery: they are the
shapes that docs/domain/data-model.md describes, expressed as frozen
dataclasses so they can be constructed in tests without a database and passed
across the layer boundary without leaking SQLAlchemy state into core.

Persistence lives in adapters/; only this module decides what an ItemDraft or
a schedule policy *means*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from core.enums import ErrorClass, PublishStatus, SourceType


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemDraft:
    """The universal pre-normalisation shape every ingest path produces.

    RSS, web collectors and the future ingest API all converge here before
    normalisation, which is what keeps one dedupe rule and one publish gate
    covering every source.
    """

    source_id: str
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
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")


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
    # Consecutive failures so far; drives exponential backoff.
    consecutive_failures: int = 0

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
    source_id: str
    guid: str
    content_hash: str
    title: str
    url: str
    published_at: datetime | None
    publish_status: PublishStatus
    content_ref: str | None = None
    published_to_feed_at: datetime | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("published_at", self.published_at),
            ("published_to_feed_at", self.published_to_feed_at),
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
    status: str
    canonical_url: str | None = None
    parser_plugin_id: str | None = None


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
