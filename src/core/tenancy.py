"""The single-tenant assumption, named once (issue #89).

Pearss is a single-user system. `tenant_id` exists on every table and in every
query, and exactly one value is ever written to it -- which is a decision, not an
oversight. It was spread across eight modules as a local `SINGLE_TENANT = "default"`,
which made it read as eight coincidences rather than one assumption repeated.

Naming it here is the part that changes something: the constant is now findable,
and the scope decision plus what would change it live beside the value rather
than in a review document nobody rereads. The check that enforces it is
`app.tenancy_check`, because it needs SQL and this module may not import any.

**Deliberately not done: end-to-end multi-tenant isolation.** Carrying a tenant
through requests, background tasks and persistence is a large change whose value
is zero until a second tenant exists -- and half of it (the column without the
carrying) is what this module replaces. ADR 0038 records that decision.

**What would change it**: a second *user* rather than a second feed, a hosted
deployment, or a shared instance for a household. Any of those turns the
assumption into a bug whose failure mode is one tenant's rows appearing in
another's reads, so the trigger is a requirement rather than a date.
"""

from __future__ import annotations

# The one tenant value this system writes. Every filter uses it and every row
# carries it; a second value in the data is a bug, not a feature.
SINGLE_TENANT = "default"

__all__ = ["SINGLE_TENANT"]
