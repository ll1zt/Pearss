"""The processor runtime's child: the context in, the per-model report out.

The child normally runs as `python -m app.processor_runtime` in a subprocess,
whose coverage the parent's run cannot see -- the reason the testing guide
requires an in-process test for an out-of-band entry point. Driving it here is
also the cheapest place to pin the two data channels it owns: the row's
deadline going into the context the processor reads (ADR 0047), and the
per-model report coming back out in the response (ADR 0048).
"""

from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app import processor_runtime
from core.enums import ErrorClass
from core.plugins.hooks import ProcessorError

pytestmark = pytest.mark.unit

# What the processor below was handed, so the test can assert the deadline
# crossed the boundary rather than only that the call succeeded.
_RECEIVED: dict[str, Any] = {}


def process(item_ctx: dict[str, Any], *, target_lang: str, store: Any) -> str:
    """A processor that answers, reporting one failure and one answer.

    Not `test_`-prefixed, so pytest does not collect it: the child resolves a
    processor by module and attribute name, and this module is the fixture.
    """
    _RECEIVED["deadline"] = item_ctx.get("deadline")
    item_ctx["model_events"].append(["primary", 2, 1, "network"])
    item_ctx["model_events"].append(["fallback", 3, 0, None])
    return str(store("<p>translated</p>"))


class Failing:
    """A second processor: it reports, then fails, like the real chain does."""

    @staticmethod
    def process(item_ctx: dict[str, Any], *, target_lang: str, store: Any) -> str:
        item_ctx["model_events"].append(["primary", 1, 1, "blocked"])
        raise ProcessorError(ErrorClass.BLOCKED, "provider returned HTTP 429")


class _NoWatchdog:
    """The child's deadline watchdog, disarmed.

    `_child_main` starts a `threading.Timer` that calls `os._exit(124)` at the
    deadline. In a real child that is exactly the point; driven in-process it
    kills the *pytest* process once the deadline passes -- how this test first
    failed the gate (exit 124, 30s after it ran, mid-suite). The watchdog is
    not what is under test here: the two data channels are.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.daemon = True

    def start(self) -> None:
        pass


def _child_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    name: str,
) -> dict[str, Any]:
    """Run the child in this process and return the response it wrote."""
    request = {
        "module": __name__,
        "name": name,
        "item_ctx": {"title": "T", "content": "<p>English</p>"},
        "target_lang": "zh",
        "deadline": time.monotonic() + 30.0,
    }
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(
        processor_runtime, "threading", SimpleNamespace(Timer=_NoWatchdog)
    )
    before = threading.active_count()
    processor_runtime._child_main()
    assert threading.active_count() <= before, (
        "the child's deadline watchdog outlived the call; in-process it would "
        "os._exit(124) this pytest process once the deadline passed"
    )
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    return payload


def test_the_child_hands_the_row_deadline_and_returns_the_model_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-223, AC-226: the report crosses the child boundary on the success path.

    Without this the worker's registry never learns which model answered, so
    "a success resets the escalation" could not be implemented at all.
    """
    _RECEIVED.clear()

    response = _child_response(monkeypatch, capsys, name="")

    assert response == {
        "body": "<p>translated</p>",
        "model_events": [["primary", 2, 1, "network"], ["fallback", 3, 0, None]],
    }
    assert isinstance(_RECEIVED["deadline"], float), "the row's deadline arrived"


def test_the_child_returns_the_report_on_the_failure_path_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-225, AC-227: a failed row is exactly the row the registry needs.

    A report that survived only success would leave every genuinely broken
    model looking healthy to the worker.
    """
    response = _child_response(monkeypatch, capsys, name="Failing")

    assert response == {
        "error_class": "blocked",
        "detail": "provider returned HTTP 429",
        "model_events": [["primary", 1, 1, "blocked"]],
    }
