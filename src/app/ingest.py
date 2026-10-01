"""One item an agent pushed: the shape, and the draft it becomes.

Two entry points accept pushed content -- `POST /v1/sources/{id}:ingest` and an
`ingest` work (issue #220) -- and they must agree on both halves. The identity
rule in particular ("when the submitted URL is unusable, mint one under
`agent.invalid`") is exactly the kind that two copies would drift on, and the
drift would be silent: the same content would land twice under two identities,
which is the duplicate the dedupe is supposed to prevent.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from pydantic import BaseModel, Field

from core.models import ItemDraft
from core.normalize import canonicalize_url


class IngestItem(BaseModel):
    """One item an agent is pushing into an agent source (ADR 0022).

    Deliberately *not* ItemDraft, which carries `source_id`. Binding that
    directly is how the endpoint this replaces let a write-scoped caller inject
    items into any source, including ones it does not own. The field is absent
    here so the privilege line holds at the type level: there is no key to send,
    so no runtime check can be forgotten.
    """

    external_id: str = Field(min_length=1, max_length=512)
    title: str = Field(default="", max_length=1024)
    canonical_url: str = Field(default="", max_length=4096)
    published_at: datetime | None = None
    content: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    derivation_note: str | None = Field(default=None, max_length=1024)


def ingest_url(item: IngestItem, *, source_url: str) -> str:
    """The canonical_url to normalise for one submitted item.

    The submitted URL wins when it is usable, because it is the item's real
    location and downstream dedupe keys on it. When there is none, or it is not
    an http(s) URL -- an agent scraping a page whose canonical form is a
    `mailto:` or an app link -- the item still deserves a stable, honest
    identity, so one is minted under a namespace that says where it came from.
    `agent.invalid` is reserved for this and can never resolve; the external_id
    makes the minted URL unique per entry.
    """
    supplied = item.canonical_url.strip()
    if supplied:
        try:
            canonicalize_url(supplied)
            return supplied
        except ValueError:
            pass  # non-http(s) schemes fall through to the minted form
    pod = source_url.removeprefix("agent://") or "agent"
    return f"https://agent.invalid/{pod}/{quote(item.external_id, safe='')}"


def ingest_draft(item: IngestItem, *, source_id: str, source_url: str) -> ItemDraft:
    """The draft one submitted item becomes, bound to its destination source."""
    metadata = dict(item.metadata)
    supplied = item.canonical_url.strip()
    resolved = ingest_url(item, source_url=source_url)
    if supplied and supplied != resolved:
        # Preserved so a URL we could not use as the item's link is still
        # readable rather than silently swallowed.
        metadata.setdefault("submitted_url", supplied)
    return ItemDraft(
        source_id=source_id,
        external_id=item.external_id,
        title=item.title,
        canonical_url=resolved,
        published_at=item.published_at,
        content=item.content,
        metadata=metadata,
        derivation_note=item.derivation_note,
    )
