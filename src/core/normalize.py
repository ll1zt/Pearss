"""Normalisation: the single entry point from every ingest path to an Item.

RSS, web collectors and the P3 ingest API all produce an `ItemDraft`; this
module turns that draft into an `Item`. Keeping one entry point is what lets a
single dedupe rule and a single publish gate cover every source.

Everything here is pure: no I/O, no clock, no database. That matters because
normalisation decides the `content_hash` and the `guid`, and both are frozen
once an item is published — a client that has cached an item cannot be told it
was wrong, so these values must be reproducible from the draft alone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.enums import PublishStatus
from core.models import Item, ItemDraft

# --------------------------------------------------------------------------
# Identity derivation
# --------------------------------------------------------------------------

# Bumped only if the derivation algorithm itself changes. Embedding it in the
# digest input means a deliberate algorithm change produces new GUIDs rather
# than silently colliding with existing ones — and since the value is part of
# the hash input, old and new can coexist during a migration.
IDENTITY_SCHEME = "id1"
CONTENT_HASH_SCHEME = "ch1"

# Query parameters that identify a campaign or referrer rather than the
# resource. docs/guides/collector-guide.md requires removing these while
# "preserving query that carries real meaning".
TRACKING_PARAM_PREFIXES = ("utm_", "pk_", "mc_", "hsa_", "vero_", "yclid")
TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "twclid",
        "igshid",
        "ref",
        "ref_src",
        "ref_url",
        "referrer",
        "source",
        "spm",
        "_hsenc",
        "_hsmi",
        "si",
        "feature",
        "share_id",
    }
)

# Ports that are the default for their scheme and carry no information.
_DEFAULT_PORTS = {"http": "80", "https": "443"}

_WHITESPACE = re.compile(r"\s+")
# Control characters other than tab/newline: they corrupt feeds and are never
# legitimate article content.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_TAG = re.compile(r"<[^>]+>")
_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿]")

# Length of the truncated digests used for id/guid/content_hash. 32 base32
# characters is 160 bits, which makes an accidental collision implausible while
# keeping the values short enough to be readable in a feed and in logs.
_DIGEST_CHARS = 32

# A title derived from body text is capped here, and marked as derived.
DERIVED_TITLE_MAX_CHARS = 80


def canonicalize_url(url: str) -> str:
    """Return a normalised form of `url` used for identity and comparison.

    Lowercases scheme and host, drops default ports, removes tracking
    parameters and the fragment, and normalises an empty path to "/".

    Deliberately conservative: query parameters that are not known trackers are
    preserved, because for many sites the query is what identifies the
    resource. Dropping it would merge distinct articles.
    """
    if not url or not url.strip():
        raise ValueError("canonicalize_url requires a non-empty URL")

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        # Anything else (mailto:, javascript:, data:) is not a fetchable
        # article URL, and normalising it would lend it false legitimacy.
        raise ValueError(f"unsupported URL scheme: {scheme or '(none)'!r}")

    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"URL has no host: {url!r}")

    port = parts.port
    if port is not None and str(port) == _DEFAULT_PORTS.get(scheme):
        port = None

    netloc = host if port is None else f"{host}:{port}"

    path = parts.path or "/"
    # Collapse duplicate slashes, which otherwise make the same resource look
    # like two different URLs.
    path = re.sub(r"/{2,}", "/", path)

    query = _canonical_query(parts.query)
    # Fragment is client-side only and never identifies the resource.
    return urlunsplit((scheme, netloc, path, query, ""))


def _canonical_query(query: str) -> str:
    """Drop tracking parameters, keep the rest in a stable order."""
    if not query:
        return ""
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    if not kept:
        return ""
    # Sorting makes the canonical form independent of parameter order, so two
    # URLs that differ only in ordering produce the same identity.
    return urlencode(sorted(kept))


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PARAM_PREFIXES)


def normalize_text(text: str) -> str:
    """Normalise text for hashing and comparison.

    Strips control and zero-width characters, collapses all whitespace runs to
    a single space, and trims. Applied to both titles and bodies so that
    insignificant formatting differences do not defeat dedupe.
    """
    if not text:
        return ""
    cleaned = _CONTROL_CHARS.sub("", text)
    cleaned = _ZERO_WIDTH.sub("", cleaned)
    return _WHITESPACE.sub(" ", cleaned).strip()


def source_canonical_key(*, source_type: str, url: str | None) -> str:
    """The dedupe key for a source of the given type.

    rss and web sources key on their canonicalised URL. Agent sources have no
    URL to key on — they are pushed to, not polled — so they get a synthetic
    `agent://<name>` key instead.

    The key must never be NULL: it is what the uniqueness constraint compares,
    and SQL treats NULLs as distinct, so a NULL here would put the row outside
    the constraint entirely rather than merely leaving a field empty.
    """
    if source_type == "agent":
        if not url or not url.strip():
            raise ValueError(
                "agent sources need a name to key on, given as url "
                "(for example 'agent://newsletter-intake')"
            )
        supplied = url.strip()
        return supplied if supplied.startswith("agent://") else f"agent://{supplied}"

    # Checked against the known set rather than treating anything-not-agent as
    # rss: a misspelled type would otherwise silently acquire URL semantics.
    if source_type not in {"rss", "web"}:
        raise ValueError(
            f"unknown source type {source_type!r}; expected rss, web or agent"
        )

    if not url:
        raise ValueError(f"{source_type} sources require a url")
    return canonicalize_url(url)


def plain_text(html: str) -> str:
    """Reduce markup to the text a reader would see.

    Used only to derive a content hash, so it is intentionally crude: it does
    not parse or sanitise HTML, and its output is never displayed. Two feeds
    emitting the same article with different markup should still hash the same.
    """
    if not html:
        return ""
    without_tags = _HTML_TAG.sub(" ", html)
    return normalize_text(without_tags)


def content_hash(*, title: str, content: str) -> str:
    """Stable hash identifying an article across sources.

    Derived only from the title and body, never from the URL, the source or the
    time — AGENTS.md forbids treating the URL as the dedupe key, and including
    the source would make the same article look distinct when two feeds carry
    it, which is precisely the case B2's cross-source dedupe exists to catch.

    Two drafts of the same article therefore agree even if they arrive from
    different sources with different markup and different tracking parameters.
    """
    normalised_title = normalize_text(title).casefold()
    normalised_body = plain_text(content).casefold()
    return _digest(CONTENT_HASH_SCHEME, normalised_title, normalised_body)


def derive_item_id(*, tenant_id: str, source_id: str, external_id: str) -> str:
    """Stable internal identity for a draft.

    Deterministic rather than random, so re-fetching the same entry yields the
    same id and the write is naturally idempotent: an insert can be attempted
    again after a failure without first querying, and a database rebuilt from
    scratch reproduces the same ids.
    """
    return _digest(IDENTITY_SCHEME, "item", tenant_id, source_id, external_id)


def derive_guid(*, tenant_id: str, source_id: str, external_id: str) -> str:
    """Stable public GUID for a draft.

    The GUID is frozen once an item is published: NNW caches what it fetched,
    so reissuing a new GUID for the same article shows the user a duplicate
    rather than a correction. Deriving it deterministically means the value
    survives a database rebuild, which a randomly generated one would not.

    Scoped to the source, so the same article carried by two feeds keeps two
    distinct GUIDs; reconciling those is `content_hash`'s job, not the GUID's.
    """
    return _digest(IDENTITY_SCHEME, "guid", tenant_id, source_id, external_id)


def _digest(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:_DIGEST_CHARS]


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizationResult:
    """The outcome of normalising a draft.

    `body` is returned alongside the item because the item itself only carries
    a `content_ref`: core decides what the cleaned body *is*, and the adapter
    decides where it is stored. Returning it here rather than writing it keeps
    this module pure.
    """

    item: Item
    body: str
    # True when the title was synthesised from the body rather than supplied.
    title_derived: bool
    # True when no body text could be recovered; distinct from an empty body
    # that was legitimately empty at the source.
    content_missing: bool


def normalize(
    draft: ItemDraft,
    *,
    tenant_id: str,
    content_ref: str | None = None,
    now: datetime | None = None,
) -> NormalizationResult:
    """Turn an `ItemDraft` into an `Item`.

    The only supported path from any ingest source to a stored item.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    url = canonicalize_url(draft.canonical_url)
    # Two different forms of the content, on purpose:
    #
    #   body  — the source's own markup, which is what a reader should see,
    #           with links and formatting intact. The caller sanitises it before
    #           storing (see app.pipeline), because core must not depend on an
    #           HTML sanitiser and the cleaned form is not what we want to show.
    #   text  — markup stripped, used only where structure would get in the
    #           way: deriving a title from it, and detecting that a body had no
    #           recoverable text.
    #
    # content_hash normalises the markup itself, so dedupe is unaffected by
    # storing the richer form.
    body = draft.content
    text = plain_text(body)
    title, title_derived = _resolve_title(draft.title, text)
    timestamp = now or datetime.now(UTC)

    item = Item(
        id=derive_item_id(
            tenant_id=tenant_id,
            source_id=draft.source_id,
            external_id=draft.external_id,
        ),
        source_id=draft.source_id,
        guid=derive_guid(
            tenant_id=tenant_id,
            source_id=draft.source_id,
            external_id=draft.external_id,
        ),
        content_hash=content_hash(title=title, content=draft.content),
        title=title,
        url=url,
        published_at=_resolve_published_at(draft.published_at),
        content_ref=content_ref,
        # Every new item starts behind the gate. Whether it advances depends on
        # the feed's processing chain, decided downstream by the state machine.
        publish_status=PublishStatus.PENDING,
        published_to_feed_at=None,
        tenant_id=tenant_id,
        created_at=timestamp,
        updated_at=timestamp,
    )

    return NormalizationResult(
        item=item,
        body=body,
        title_derived=title_derived,
        # Tests the *cleaned* text, not the raw body: markup with no words in
        # it, such as an image-only entry, has a non-empty body but nothing a
        # reader could read, and that is a different problem from a source that
        # legitimately publishes no body.
        content_missing=not text and bool(draft.content),
    )


def _resolve_title(title: str, body: str) -> tuple[str, bool]:
    """Return (title, whether it was derived from the body).

    docs/guides/collector-guide.md permits a short summary when the source
    gives no title, but requires that it be clearly marked — so it is flagged
    in the result rather than being silently substituted.
    """
    cleaned = normalize_text(title)
    if cleaned:
        return cleaned, False
    if not body:
        return "Untitled", True
    clipped = body[:DERIVED_TITLE_MAX_CHARS].rstrip()
    if len(body) > DERIVED_TITLE_MAX_CHARS:
        clipped = f"{clipped}…"
    return clipped, True


def _resolve_published_at(published_at: datetime | None) -> datetime | None:
    """Normalise a publication timestamp to UTC.

    A naive timestamp is *attached* to UTC rather than converted: calling
    astimezone() on a naive datetime makes Python interpret it as local time
    and shift it, so 10:00 on a UTC+8 host would become 02:00. That is exactly
    the local-time guess docs/guides/collector-guide.md forbids, and it would
    misdate articles by hours.

    The zone is genuinely unknown here, so we record it as UTC rather than
    invent an offset. The original value remains on the draft for diagnosis.
    """
    if published_at is None:
        return None
    if published_at.tzinfo is None:
        return published_at.replace(tzinfo=UTC)
    return published_at.astimezone(UTC)


def with_content_ref(item: Item, content_ref: str) -> Item:
    """Return a copy of `item` carrying `content_ref`.

    Items are frozen and core does no I/O, so the adapter stores the body and
    attaches the reference afterwards rather than mutating the item.
    """
    return replace(item, content_ref=content_ref)


# --------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------


class DedupeAction(StrEnum):
    """What to do when a new item's content_hash matches an existing one.

    docs/domain/data-model.md requires an explicit rule rather than silent
    duplicate publication; this names the choices so the decision is visible
    in code and auditable in logs.
    """

    # A genuinely new article: store it.
    INSERT = "insert"
    # The same article from the same source, re-fetched: keep the existing row.
    SKIP_SAME_SOURCE = "skip_same_source"
    # The same article from a different source: store it but mark it related,
    # so the relationship is explicit rather than lost to a unique constraint.
    INSERT_RELATED = "insert_related"
    # The existing item is already published; publishing this copy is what
    # would show the reader a duplicate.
    SUPPRESS = "suppress"
    # The same entry, re-fetched with different content, still behind the gate:
    # take the new content, since nothing has seen the old.
    UPDATE_PENDING = "update_pending"
    # The same entry, re-fetched with different content, already published.
    # Neither rewriting it (clients cached the old) nor inserting it (the guid
    # is frozen and would collide) is available, so it is left alone and
    # reported.
    SKIP_PUBLISHED_CONTENT_CHANGED = "skip_published_content_changed"


@dataclass(frozen=True, slots=True, kw_only=True)
class DedupeDecision:
    """The outcome of comparing a candidate against existing items."""

    action: DedupeAction
    # Identity of the item that caused a non-insert decision, when there is one.
    matched_id: str | None = None
    reason: str = ""


def decide_dedupe(
    item: Item,
    existing: list[Item],
) -> DedupeDecision:
    """Decide what to do with `item` given items already stored.

    Pure and total: it never raises on an empty list, and it always returns an
    explicit action, so a caller cannot accidentally fall through to inserting
    a duplicate.

    Identity is checked before content, and that order matters. An entry's id
    and guid derive from (tenant, source, external_id), so the same entry
    re-published with an edited body keeps its guid while its content_hash
    changes. Matching on content_hash alone misses it, falls through to an
    INSERT, and collides on the unique guid — which is what reached production
    as soon as a feed reworded an article.

    A changed body is not a new article and must not become one. Published
    content is frozen by docs/domain/data-model.md, so the only honest
    outcomes are "take the new content while still pending" or "leave the
    published row alone and report the drift".
    """
    same_identity = [
        candidate
        for candidate in existing
        if candidate.id == item.id or candidate.guid == item.guid
    ]
    if same_identity:
        known = same_identity[0]
        if known.content_hash == item.content_hash:
            return DedupeDecision(
                action=DedupeAction.SKIP_SAME_SOURCE,
                matched_id=known.id,
                reason="same entry and same content already stored",
            )
        if known.publish_status is PublishStatus.PUBLISHED:
            return DedupeDecision(
                action=DedupeAction.SKIP_PUBLISHED_CONTENT_CHANGED,
                matched_id=known.id,
                reason=(
                    "entry already published; its source content changed and "
                    "published content is frozen"
                ),
            )
        return DedupeDecision(
            action=DedupeAction.UPDATE_PENDING,
            matched_id=known.id,
            reason="same entry, content changed while still pending",
        )

    matches = [
        candidate
        for candidate in existing
        if candidate.content_hash == item.content_hash
    ]
    if not matches:
        return DedupeDecision(
            action=DedupeAction.INSERT, reason="no content_hash match"
        )

    same_source = [match for match in matches if match.source_id == item.source_id]
    if same_source:
        return DedupeDecision(
            action=DedupeAction.SKIP_SAME_SOURCE,
            matched_id=same_source[0].id,
            reason="same content_hash already stored from this source",
        )

    cross_source = matches[0]
    if cross_source.publish_status is PublishStatus.PUBLISHED:
        # The reader has already been shown this article. Publishing our copy
        # under a different GUID is exactly the silent duplicate the model
        # forbids, so it is suppressed and the relationship recorded instead.
        return DedupeDecision(
            action=DedupeAction.SUPPRESS,
            matched_id=cross_source.id,
            reason="identical content already published from another source",
        )

    return DedupeDecision(
        action=DedupeAction.INSERT_RELATED,
        matched_id=cross_source.id,
        reason="same content from another source; stored and related",
    )


def is_duplicate_of(a: Item, b: Item) -> bool:
    """Whether two items represent the same article.

    Compares `content_hash` only. Two items about the same URL with different
    bodies are not duplicates — the URL is not the dedupe key.
    """
    return a.content_hash == b.content_hash
