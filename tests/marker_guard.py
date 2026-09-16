"""Collect pytest items and reject tests absent from every execution suite."""

from __future__ import annotations

import sys

import pytest

LEVELS = frozenset({"unit", "fixture", "integration", "contract", "e2e"})


class ExecutionLevels:
    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        # Before -m/-k deselection: otherwise missing levels hide themselves.
        missing = [
            item.nodeid
            for item in items
            if not LEVELS.intersection(mark.name for mark in item.iter_markers())
        ]
        if missing:
            raise pytest.UsageError(
                "missing execution-level marker:\n" + "\n".join(missing)
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main(sys.argv[1:], plugins=[ExecutionLevels()]))
