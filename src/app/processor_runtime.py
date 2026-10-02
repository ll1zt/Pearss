"""One-shot processor execution: JSON only, killable, no database in the child.

The child's two pipes are both protocol: **stdout** carries the response (one
JSON object, written once) and **stderr** carries the processor's own logs, one
`<LEVEL> <logger> <message>` line per record. The host re-logs them with the
row's context, so "which model failed, and where did it fall back to?" is
answerable from the journal alone (issue #229: stderr used to go to /dev/null
and the fallback line was written on every failure, read by nobody).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from types import ModuleType
from typing import Any

from app.plugin_runtime import child_environment
from core.enums import ErrorClass
from core.plugins.hooks import (
    DEADLINE_KEY,
    MODEL_EVENTS_KEY,
    ProcessorError,
)

LOGGER = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
REAP_SECONDS = 1.0
# The lease reserves time for termination and the parent's result transaction.
LEASE_GRACE_SECONDS = 5.0
# Bounds on the child's log channel: the journal is not a bucket, and a
# processor logging in a loop must not be able to fill it (issue #229).
MAX_CHILD_LOG_LINES = 200
MAX_CHILD_LOG_CHARS = 1000

# The child formats records with `logging`, so the host reads its own vocabulary
# back rather than a format we invented in two places. A line that does not open
# with one of these is forwarded as a warning -- a traceback or a stray print is
# an observation, not something to drop.
_CHILD_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class _ChildLog:
    """Forward a child's stderr lines into the host's logger, bounded.

    Each forwarded line carries the row's context as a prefix. That prefix is
    the whole point: without it a processor's warning is indistinguishable from
    the host's own, and `journalctl -u pearss | grep model` answers nothing.
    """

    def __init__(self, *, plugin_id: str, item_id: str) -> None:
        self._prefix = f"[{plugin_id} {item_id}]"
        self._buffer = ""
        self._lines = 0
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        """Take whatever arrived; emits only complete lines."""
        self._buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            self._emit(line.rstrip("\r"))

    def _emit(self, line: str) -> None:
        if not line.strip():
            return
        if self._lines >= MAX_CHILD_LOG_LINES:
            if not self._truncated:
                self._truncated = True
                LOGGER.warning(
                    "%s child log truncated after %d lines; the rest is dropped",
                    self._prefix,
                    self._lines,
                )
            return
        self._lines += 1
        token, _, message = line.partition(" ")
        level = _CHILD_LOG_LEVELS.get(token)
        text = message if level is not None else line
        if len(text) > MAX_CHILD_LOG_CHARS:
            text = f"{text[:MAX_CHILD_LOG_CHARS]}…"
        LOGGER.log(
            logging.WARNING if level is None else level,
            "%s %s",
            self._prefix,
            text,
        )


def run_processor(
    processor: Any,
    *,
    plugin_id: str,
    item_ctx: dict[str, Any],
    target_lang: str,
    deadline: float,
    cancelled: threading.Event,
    report: list[tuple[str, str | None]] | None = None,
) -> str:
    """Return artifact text, after the child has exited and been reaped.

    Modules and module-level classes with static process hooks are importable;
    closures/instances are refused rather than silently run without a deadline.

    `report`, when given, collects the processor's per-model outcome for this
    row -- `(model, error_class)` for a model that failed, `(model, None)` for
    one that answered. It is filled on the failure path too, which is the path
    the caller's health registry most needs (ADR 0048).
    """
    if isinstance(processor, ModuleType):
        module, name = processor.__name__, ""
    elif isinstance(processor, type) and "<locals>" not in processor.__qualname__:
        module, name = processor.__module__, processor.__qualname__
    else:
        raise ProcessorError(ErrorClass.PARSE, "processor must be importable")
    request = json.dumps(
        {
            "module": module,
            "name": name,
            "item_ctx": item_ctx,
            "target_lang": target_lang,
            "deadline": deadline,
        }
    ).encode()
    if len(request) > MAX_MESSAGE_BYTES:
        raise ProcessorError(ErrorClass.PARSE, "processor input exceeds size cap")
    if cancelled.is_set() or time.monotonic() >= deadline:
        raise ProcessorError(
            ErrorClass.NETWORK, "processor cancelled or deadline expired"
        )
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.processor_runtime"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_environment(plugin_id),
        start_new_session=True,
    )
    try:
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            raise RuntimeError("processor pipes unavailable")
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stderr.fileno(), False)
        child_log = _ChildLog(
            plugin_id=plugin_id, item_id=str(item_ctx.get("item_id", "?"))
        )
        output = bytearray()
        pending = memoryview(request)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdin, selectors.EVENT_WRITE)
            selector.register(proc.stdout, selectors.EVENT_READ)
            # Drained while the child runs, not afterwards: stderr is a pipe,
            # and a processor that logs more than its buffer holds would block
            # on the write instead of finishing.
            selector.register(proc.stderr, selectors.EVENT_READ)
            while selector.get_map():
                if cancelled.is_set():
                    raise ProcessorError(ErrorClass.NETWORK, "processor cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProcessorError(
                        ErrorClass.NETWORK, "processor deadline expired"
                    )
                for key, _ in selector.select(min(remaining, 0.05)):
                    if key.fileobj is proc.stdin:
                        count = os.write(key.fd, pending[:65536])
                        pending = pending[count:]
                        if not pending:
                            selector.unregister(proc.stdin)
                            proc.stdin.close()
                    elif key.fileobj is proc.stderr:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(proc.stderr)
                        else:
                            child_log.feed(chunk)
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(proc.stdout)
                        output.extend(chunk)
                        if len(output) > MAX_MESSAGE_BYTES:
                            raise ProcessorError(
                                ErrorClass.PARSE, "processor output exceeds size cap"
                            )
        while proc.poll() is None:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise ProcessorError(
                    ErrorClass.NETWORK, "processor cancelled or deadline expired"
                )
            cancelled.wait(min(0.05, max(0, deadline - time.monotonic())))
        if proc.returncode != 0:
            raise ProcessorError(
                ErrorClass.NETWORK, "processor exited without a result"
            )
        try:
            response = json.loads(output)
            _collect_events(response, report)
            if "error_class" in response:
                raise ProcessorError(
                    ErrorClass(response["error_class"]), response["detail"]
                )
            body = response["body"]
            if not isinstance(body, str):
                raise ValueError("artifact must be text")
        except (ValueError, TypeError, KeyError) as exc:
            raise ProcessorError(
                ErrorClass.PARSE, "invalid processor response"
            ) from exc
        return body
    except OSError as exc:
        raise ProcessorError(ErrorClass.NETWORK, "processor pipe failed") from exc
    finally:
        # Kill the whole group even after a successful parent exit: descendants
        # must not outlive an attempt or keep a provider socket open.
        with contextlib.suppress(ProcessLookupError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except PermissionError:
                # Some macOS process sandboxes prohibit group signalling.
                proc.kill()
        try:
            proc.wait(timeout=REAP_SECONDS)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()


def _collect_events(response: Any, report: list[tuple[str, str | None]] | None) -> None:
    """Append the child's per-model report to the caller's list.

    Defensive on shape: the child is ours, but a malformed entry must not turn
    a diagnosable row failure into a crash on the parent's result path.
    """
    if report is None or not isinstance(response, dict):
        return
    events = response.get(MODEL_EVENTS_KEY)
    if not isinstance(events, list):
        return
    for event in events:
        if (
            isinstance(event, list)
            and len(event) == 2
            and isinstance(event[0], str)
            and (event[1] is None or isinstance(event[1], str))
        ):
            report.append((event[0], event[1]))


def _child_main() -> None:
    request = json.loads(sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1))
    # Parent death must not leave a normally scheduled Python child running
    # beyond its lease. This watchdog uses the same host's monotonic clock.
    watchdog = threading.Timer(
        max(0, request["deadline"] - time.monotonic()), os._exit, args=(124,)
    )
    watchdog.daemon = True
    watchdog.start()
    body: str | None = None
    # A processor's logs go to stderr with their level; the host re-logs them
    # with the row's context (issue #229). stderr is otherwise unused here:
    # stdout carries the response and is redirected away from plugin prints,
    # so this is a channel, not a side effect.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        force=True,
    )
    # The row's deadline goes into the context the processor reads, not only
    # into the watchdog that kills it: a processor that cannot see its budget
    # can only discover it by being killed, and a kill discards whatever the
    # processor had learned (issue #227). The report channel is created here so
    # that a processor which knows nothing about it is unaffected (ADR 0048).
    item_ctx: dict[str, Any] = {
        **request["item_ctx"],
        DEADLINE_KEY: request["deadline"],
        MODEL_EVENTS_KEY: [],
    }

    def store(content: str) -> str:
        nonlocal body
        if not isinstance(content, str):
            raise ProcessorError(ErrorClass.PARSE, "artifact must be text")
        body = content
        return "processor-artifact"

    try:
        # stdout belongs exclusively to the protocol; discard plugin prints.
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            processor = importlib.import_module(request["module"])
            for part in filter(None, request["name"].split(".")):
                processor = getattr(processor, part)
            result = processor.process(
                item_ctx,
                target_lang=request["target_lang"],
                store=store,
            )
        if result != "processor-artifact" or body is None:
            raise ProcessorError(
                ErrorClass.PARSE, "processor did not return its stored artifact"
            )
        response = {"body": body, MODEL_EVENTS_KEY: item_ctx[MODEL_EVENTS_KEY]}
    except ProcessorError as exc:
        response = {
            "error_class": exc.error_class.value,
            "detail": exc.detail[:500],
            MODEL_EVENTS_KEY: item_ctx[MODEL_EVENTS_KEY],
        }
    except Exception:
        response = {
            "error_class": ErrorClass.NETWORK.value,
            "detail": "processor execution failed",
            MODEL_EVENTS_KEY: item_ctx[MODEL_EVENTS_KEY],
        }
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()


if __name__ == "__main__":
    _child_main()
