"""Falsify the single-tenant assumption against the data (issue #89).

`core.tenancy.SINGLE_TENANT` names the assumption; this is the one place it can
be shown wrong. The column exists on every fact table, so a second value is
representable -- and if one ever appears, every read that filters on the
constant stops returning those rows, silently. Startup is the first moment that
is visible, and saying so is the whole point of the check.

Not raised, logged. The rows exist; refusing to start turns a data surprise into
an outage, and an operator reading the journal is better served by "these rows
are invisible to every read" than by a service that will not come up. What is not
acceptable is silence, which is what the scattered constants guaranteed.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from core.tenancy import SINGLE_TENANT

LOGGER = logging.getLogger(__name__)

# Every table whose rows carry a tenant, measured from the migrated schema
# rather than guessed: `audit_log`, `job`, `job_run` and `raw_fetch` do not have
# the column, and the first version of this list said `audit_log` did.
#
# A guard in tests/unit/test_structure_guards.py reads the schema and fails if
# this list and the schema disagree, so a new table with the column cannot
# quietly go unreported -- which is the silence this check exists to remove.
TENANT_TABLES = (
    "feed_out",
    "item",
    "item_enrichment",
    "plugin",
    "read_state",
    "source",
    "work",
)


def other_tenants(session_factory: Any) -> list[str]:
    """Tenant ids present in the data that are not the single one.

    Reads only the distinct values, with a bound: this runs at startup, and its
    job is to answer "is there anything else here", not to count it.
    """
    found: set[str] = set()
    with session_factory() as session:
        for table in TENANT_TABLES:
            rows = session.execute(
                # Fixed table names from the tuple above, never user input.
                text(f"select distinct tenant_id from {table} limit 10")  # noqa: S608
            ).fetchall()
            found.update(str(row[0]) for row in rows if row[0] is not None)
    return sorted(found - {SINGLE_TENANT})


def report_other_tenants(session_factory: Any) -> list[str]:
    """Log the tenants this system does not write, and return them."""
    others = other_tenants(session_factory)
    if others:
        LOGGER.warning(
            "this database contains rows for tenant(s) %s, but Pearss writes "
            "only %r. Reads filter on that constant, so those rows do not "
            "appear anywhere -- the single-tenant assumption is wrong here, and "
            "the decision behind it is in core/tenancy.py and ADR 0038",
            others,
            SINGLE_TENANT,
        )
    return others
