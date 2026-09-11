"""The OpenAPI document, pinned against a committed snapshot.

The style guide promises that breaking API changes fail CI. oasdiff is not
available in nixpkgs, so the same guarantee is delivered the way the MCP
surface snapshot already works: the generated document is compared against a
committed file, and every change -- a removed field, a new required parameter,
a changed error shape -- becomes a visible diff a reviewer must accept rather
than a silent drift.

Set OPENAPI_SNAPSHOT_UPDATE=1 to rewrite the snapshot after reviewing the
change, never to make the test pass.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "tests" / "contract" / "openapi.json"


def _current(app: FastAPI) -> dict[str, Any]:
    """The OpenAPI document as FastAPI generates it, canonically ordered.

    Dict ordering is irrelevant to the contract, so the comparison runs on
    sorted JSON: a pure reordering must not churn the snapshot.
    """
    document: dict[str, Any] = json.loads(
        json.dumps(app.openapi(), sort_keys=True, ensure_ascii=False)
    )
    return document


def test_openapi_document_matches_the_snapshot(app: FastAPI) -> None:
    current = _current(app)

    if os.environ.get("OPENAPI_SNAPSHOT_UPDATE") == "1":
        SNAPSHOT_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        pytest.skip("snapshot rewritten; review the diff and commit it")

    assert SNAPSHOT_PATH.exists(), (
        "tests/contract/openapi.json is missing; run "
        "OPENAPI_SNAPSHOT_UPDATE=1 just contract to create it"
    )
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert current == expected, (
        "the OpenAPI document changed; review the difference, then run "
        "OPENAPI_SNAPSHOT_UPDATE=1 just contract and commit the new snapshot"
    )


def test_every_documented_path_still_exists(app: FastAPI) -> None:
    """A path disappearing from the spec is the loudest breaking change.

    The full-document snapshot already catches this, but with a message that
    names the path instead of a JSON blob -- the difference between a reviewer
    instantly knowing what broke and diffing two thousand lines.
    """
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    current = _current(app)

    removed = sorted(set(expected["paths"]) - set(current["paths"]))
    added = sorted(set(current["paths"]) - set(expected["paths"]))
    assert not removed, f"routes disappeared from the API: {removed}"
    assert not added, (
        f"routes appeared without a snapshot update: {added} "
        "(run OPENAPI_SNAPSHOT_UPDATE=1 just contract)"
    )
