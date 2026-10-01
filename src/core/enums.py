"""Domain enumerations.

These are the single source of truth for their respective taxonomies. Per
AGENTS.md, no part of the system may substitute bare strings: the database
constraint, the OpenAPI schema and the MCP tool schema all derive from these
enums. Adding a value here is a deliberate, reviewable act.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    """The five mandatory acquisition failure categories (B5).

    Members are ordered by the classification precedence defined in
    docs/domain/error-taxonomy.md: when a single failure matches several
    signals, the first match in this order wins.
    """

    BLOCKED = "blocked"
    AUTH = "auth"
    NETWORK = "network"
    PARSE = "parse"
    STRUCTURE_CHANGED = "structure_changed"

    @property
    def precedence(self) -> int:
        """Rank in the classification precedence order; lower wins."""
        return _ERROR_CLASS_PRECEDENCE[self]

    @property
    def allows_automatic_retry(self) -> bool:
        """Whether the scheduler may retry this class without user action.

        See the retry table in docs/domain/error-taxonomy.md. Retrying
        `blocked` or `auth` would mean hammering a service that has already
        refused us, so those wait for a human.
        """
        return _RETRY_POLICY[self]


# Classification precedence: blocked > auth > network > parse >
# structure_changed. Access is blocked before anything else can be inferred;
# a credentials problem must not be misread as a structure change; and a
# transport failure leaves no content to analyse.
_ERROR_CLASS_PRECEDENCE: dict[ErrorClass, int] = {
    ErrorClass.BLOCKED: 0,
    ErrorClass.AUTH: 1,
    ErrorClass.NETWORK: 2,
    ErrorClass.PARSE: 3,
    ErrorClass.STRUCTURE_CHANGED: 4,
}

_RETRY_POLICY: dict[ErrorClass, bool] = {
    ErrorClass.NETWORK: True,
    ErrorClass.PARSE: True,
    ErrorClass.STRUCTURE_CHANGED: False,
    ErrorClass.BLOCKED: False,
    ErrorClass.AUTH: False,
}


class PublishStatus(StrEnum):
    """Lifecycle of an Item with respect to external visibility (C5).

    The gate is the point: because NetNewsWire caches whatever it fetched
    first, an item must not appear in a feed until every processor in its
    chain has succeeded. `published` is terminal for the item's content.
    """

    PENDING = "pending"
    READY = "ready"
    PUBLISHED = "published"


class ItemEvent(StrEnum):
    """Events that drive the Item publish state machine.

    Defined in core rather than inferred from HTTP endpoints so that the
    transition table is the only place that decides legality.
    """

    MARK_READY = "mark_ready"
    ENRICHMENT_FAILED = "enrichment_failed"
    TIMEOUT = "timeout"
    FORCE_PUBLISH = "force_publish"
    FIRST_PUBLISH = "first_publish"
    WITHDRAW = "withdraw"
    READ_STATE_CHANGED = "read_state_changed"


class TimeoutPolicy(StrEnum):
    """What a pending item does when its processing chain exceeds its budget."""

    PUBLISH_ORIGINAL = "publish_original"
    HOLD = "hold"


class JobRunStatus(StrEnum):
    """Lifecycle of a single job execution."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class SourceStatus(StrEnum):
    """Lifecycle of a content source.

    There is no `deleted`. Deletion removes the row (ADR 0021), and a status
    meaning "this row is really gone" cannot describe a row that exists --
    keeping one invited every reader to handle a state that was never durable.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class PluginStatus(StrEnum):
    """Lifecycle of a registered plugin (P2, ADR 0017)."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class SourceType(StrEnum):
    """How a source is acquired."""

    RSS = "rss"
    WEB = "web"
    AGENT = "agent"


class AuditOutcome(StrEnum):
    """Result of an audited operation."""

    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


class TrustLevel(StrEnum):
    """Recorded operator intent about a source.

    The hard rule, from docs/domain/data-model.md: this never makes fetched
    content trusted. A `trusted` source's body is still untrusted input —
    AGENTS.md is explicit that external pages and RSS bodies must never be
    treated as instructions, and a per-source flag that silently flipped that
    would be the most dangerous possible reading of the field.

    Nothing currently branches on this value. It is stored and surfaced so the
    distinction exists in the data before any behaviour depends on it; giving
    it effects requires an ADR, because the only behaviour it could plausibly
    change is a security default.
    """

    STANDARD = "standard"
    TRUSTED = "trusted"
    RESTRICTED = "restricted"


class WorkStatus(StrEnum):
    """Lifecycle of one requested batch of work (ADR 0046).

    `partial` is a first-class terminal state on purpose: "some targets
    succeeded and some did not" is the normal outcome of a batch aimed at a
    third party. Collapsing it into `failed` would either hide the successes
    or force every caller to re-derive the split from the ledger.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UnitOutcome(StrEnum):
    """What happened to one target inside a work (ADR 0046).

    `rejected` (this deployment refused the target before acting: policy,
    capability, quota) is deliberately distinct from `failed` (the action ran
    and the world said no). One is our decision and is not worth retrying; the
    other is the origin's and is. `skipped` is "nothing to do" (an already
    absent row), which is not a failure either.
    """

    OK = "ok"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    FAILED = "failed"
