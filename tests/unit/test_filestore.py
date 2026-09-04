"""Tests for content-addressed body storage.

The store keeps large payloads out of the database (docs/domain/data-model.md),
so these pin the properties the rest of the system relies on: a reference
always resolves to the bytes that produced it, a bad reference degrades to an
empty body rather than taking down a feed, and nothing can escape the root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters import filestore


@pytest.mark.unit
def test_round_trip(tmp_path: Path) -> None:
    ref = filestore.store_body("Hello <b>world</b>", root=tmp_path)
    assert filestore.read_body(ref, root=tmp_path) == "Hello <b>world</b>"


@pytest.mark.unit
def test_reference_is_content_derived_and_stable(tmp_path: Path) -> None:
    first = filestore.store_body("same bytes", root=tmp_path)
    second = filestore.store_body("same bytes", root=tmp_path)
    assert first == second


@pytest.mark.unit
def test_identical_bodies_share_one_file(tmp_path: Path) -> None:
    filestore.store_body("dedupe me", root=tmp_path)
    filestore.store_body("dedupe me", root=tmp_path)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(files) == 1


@pytest.mark.unit
def test_different_bodies_get_different_references(tmp_path: Path) -> None:
    assert filestore.store_body("a", root=tmp_path) != filestore.store_body(
        "b", root=tmp_path
    )


@pytest.mark.unit
def test_reference_carries_the_scheme(tmp_path: Path) -> None:
    ref = filestore.store_body("x", root=tmp_path)
    # The scheme records which backend produced it, so a P3 move to object
    # storage can coexist with rows written by this one.
    assert ref.startswith("cas://")


@pytest.mark.unit
@pytest.mark.parametrize(
    "ref",
    [
        None,
        "",
        "not-a-reference",
        "cas://../etc/passwd",
        "cas://" + "z" * 64,
        "cas://" + "a" * 63,
        "file:///etc/passwd",
        "cas://" + "a" * 64 + "/../../escape",
    ],
)
def test_unusable_references_yield_empty_not_an_error(
    tmp_path: Path, ref: str | None
) -> None:
    # A feed must survive one bad row: the caller still has title and link.
    assert filestore.read_body(ref, root=tmp_path) == ""


@pytest.mark.unit
def test_missing_file_yields_empty(tmp_path: Path) -> None:
    ref = "cas://" + ("a" * 64)
    assert filestore.read_body(ref, root=tmp_path) == ""


@pytest.mark.unit
def test_reference_cannot_escape_the_root(tmp_path: Path) -> None:
    # The digest is validated as exactly 64 hex characters, so no path
    # separators or traversal sequences can reach the filesystem at all.
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("do not read me", encoding="utf-8")
    sneaky = f"cas://{outside.name}"
    assert filestore.read_body(sneaky, root=tmp_path) == ""
    assert outside.read_text(encoding="utf-8") == "do not read me"


@pytest.mark.unit
def test_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    filestore.store_body("clean", root=tmp_path)
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.unit
def test_unicode_survives(tmp_path: Path) -> None:
    body = "中文 — em dash — \U0001f600"
    ref = filestore.store_body(body, root=tmp_path)
    assert filestore.read_body(ref, root=tmp_path) == body


@pytest.mark.unit
def test_empty_body_round_trips(tmp_path: Path) -> None:
    ref = filestore.store_body("", root=tmp_path)
    assert filestore.read_body(ref, root=tmp_path) == ""


@pytest.mark.unit
def test_bodies_for_skips_items_without_a_reference(tmp_path: Path) -> None:
    stored = filestore.store_body("real body", root=tmp_path)
    resolved = filestore.bodies_for(
        {"with": stored, "without": None, "blank": ""}, root=tmp_path
    )
    # Omitted rather than mapped to "": a caller must be able to tell "no body
    # was stored" from "the body is empty".
    assert resolved == {"with": "real body"}


@pytest.mark.unit
def test_bodies_for_resolves_many(tmp_path: Path) -> None:
    refs = {
        name: filestore.store_body(f"body of {name}", root=tmp_path) for name in "abc"
    }
    assert filestore.bodies_for(refs, root=tmp_path) == {
        "a": "body of a",
        "b": "body of b",
        "c": "body of c",
    }
