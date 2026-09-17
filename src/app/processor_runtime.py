"""One-shot processor execution: JSON only, killable, no database in the child."""

from __future__ import annotations

import contextlib
import importlib
import json
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
from core.plugins.hooks import ProcessorError

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
REAP_SECONDS = 1.0
# The lease reserves time for termination and the parent's result transaction.
LEASE_GRACE_SECONDS = 5.0


def run_processor(
    processor: Any,
    *,
    plugin_id: str,
    item_ctx: dict[str, Any],
    target_lang: str,
    deadline: float,
    cancelled: threading.Event,
) -> str:
    """Return artifact text, after the child has exited and been reaped.

    Modules and module-level classes with static process hooks are importable;
    closures/instances are refused rather than silently run without a deadline.
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
        stderr=subprocess.DEVNULL,
        env=child_environment(plugin_id),
        start_new_session=True,
    )
    try:
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("processor pipes unavailable")
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        output = bytearray()
        pending = memoryview(request)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdin, selectors.EVENT_WRITE)
            selector.register(proc.stdout, selectors.EVENT_READ)
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
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()


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
                request["item_ctx"], target_lang=request["target_lang"], store=store
            )
        if result != "processor-artifact" or body is None:
            raise ProcessorError(
                ErrorClass.PARSE, "processor did not return its stored artifact"
            )
        response = {"body": body}
    except ProcessorError as exc:
        response = {"error_class": exc.error_class.value, "detail": exc.detail[:500]}
    except Exception:
        response = {
            "error_class": ErrorClass.NETWORK.value,
            "detail": "processor execution failed",
        }
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()


if __name__ == "__main__":
    _child_main()
