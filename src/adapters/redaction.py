"""One place that turns a URL into something safe to put in a log line.

Two byte-identical copies of "drop the query string" had grown across the
repository (``adapters.http._redact`` and ``app.pipeline._redact_target``),
and both missed the authority: a feed URL may carry
``https://user:password@host/rss``, and that password is as much a credential
as a ``?token=`` is. Duplicated redaction is how one copy gets a fix while the
others keep leaking, so the copies collapse into this module (issue #310).

``adapters.audit.redact_url`` and ``app.mcp.rendering._redact_url`` are
deliberately *not* folded in: both keep non-sensitive query parameters,
because their job is to identify a resource (an audit row, a suggestion shown
to a caller) rather than to name it safely. This function's contract is the
opposite -- only what names the resource survives.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def redact_url_for_log(url: str) -> str:
    """A URL with no query, no userinfo and no fragment.

    Each of the three carries credentials in practice: the query string a feed
    token, the userinfo a basic-auth password, the fragment an OAuth token
    (``#access_token=...``). Scheme, host, port and path stay, because that is
    what identifies the resource being fetched.

    A string that does not parse as a URL -- an opaque id, a relative path --
    falls back to cutting at the first ``?`` or ``#``, so a caller may pass
    either a URL or an id and get a sane answer from both.
    """
    parts = urlsplit(url)
    if not parts.scheme:
        return url.split("?", 1)[0].split("#", 1)[0]
    # rpartition, not hostname/port: rebuilding from those would drop the
    # brackets around an IPv6 literal and produce a URL that no longer parses.
    netloc = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
