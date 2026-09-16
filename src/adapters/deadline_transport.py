"""Cancellable network I/O behind HTTPX's synchronous transport interface."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine, Iterator
from threading import Event, Thread
from time import monotonic
from typing import Any

import httpx

DEADLINE_EXTENSION = "pearss_deadline"


class DeadlineTransport(httpx.BaseTransport):
    """Own an asynchronous connection pool on a dedicated event-loop thread."""

    def __init__(self, *, total_timeout: float = 60.0, trust_env: bool = True) -> None:
        self._total_timeout = total_timeout
        self._loop = asyncio.new_event_loop()
        ready = Event()

        def serve() -> None:
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        self._thread = Thread(target=serve, name="pearss-http", daemon=True)
        self._thread.start()
        ready.wait()
        self._transport = httpx.AsyncClient(trust_env=trust_env)

    def _run[T](self, operation: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(operation, self._loop).result()

    async def _bounded[T](self, operation: Awaitable[T], deadline: float) -> T:
        try:
            async with asyncio.timeout(max(0.0, deadline - monotonic())):
                return await operation
        except TimeoutError as exc:
            raise httpx.ReadTimeout("total HTTP deadline exceeded") from exc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        deadline = min(
            float(request.extensions.get(DEADLINE_EXTENSION, float("inf"))),
            monotonic() + self._total_timeout,
        )
        # HTTPX's byte-backed request supplies both sync and async iteration;
        # custom synchronous upload streams must be materialized at this boundary.
        outgoing = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.read(),
            extensions=request.extensions,
        )
        response = self._run(
            self._bounded(
                self._transport.send(outgoing, stream=True, follow_redirects=False),
                deadline,
            )
        )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_ResponseStream(self, response, deadline),
            extensions=response.extensions,
        )

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        try:
            self._run(self._transport.aclose())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()


class _ResponseStream(httpx.SyncByteStream):
    def __init__(
        self, owner: DeadlineTransport, response: httpx.Response, deadline: float
    ) -> None:
        self._owner = owner
        self._response = response
        self._deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        iterator = self._response.aiter_raw()
        try:
            while True:
                try:
                    yield self._owner._run(
                        self._owner._bounded(anext(iterator), self._deadline)
                    )
                except StopAsyncIteration:
                    return
        finally:
            self.close()

    def close(self) -> None:
        self._owner._run(self._response.aclose())
