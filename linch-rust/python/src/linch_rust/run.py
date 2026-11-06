"""`Run`: the event stream of one run the server drives."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Protocol

from ._rpc import Connection, JsonObject, RunEnd, RunItem
from .errors import LinchError
from .events import Envelope, ResultEvent, SuspendedEvent, decode_envelope


class Observer(Protocol):
    """What a run tells its tracer (`linch_rust.otel.RunObserver`)."""

    def observe(self, envelope: Envelope) -> None:
        """Trace one envelope, in stream order."""
        ...

    def fail(self, error_type: str) -> None:
        """The stream ended with an error of this type."""
        ...

    def close(self) -> None:
        """The stream ended; end what its events did not."""
        ...


class Run:
    """One run started, resumed or approved: iterate it for its envelopes.

    Iteration yields every `Envelope` the server sends for the run, ending
    after its `run.end`. When the run could not continue durably, the end
    raises the `RunError` the server reported; when the server goes away,
    it raises `ConnectionClosed`.

    Attributes:
        id: The run id; pass it to `Agent.resume`, `Agent.approve` and
            `Agent.events`.
    """

    def __init__(
        self,
        conn: Connection,
        run_id: str,
        queue: asyncio.Queue[RunItem],
        observer: Observer | None = None,
    ) -> None:
        self.id = run_id
        self._conn = conn
        self._queue = queue
        self._observer = observer
        # Envelopes received and not yet yielded, still raw.
        self._received: deque[JsonObject] = deque()
        self._end: RunEnd | None = None
        self._terminal: ResultEvent | SuspendedEvent | None = None

    def __aiter__(self) -> Run:
        return self

    async def __anext__(self) -> Envelope:
        envelope = await self._next()
        if envelope is None:
            raise StopAsyncIteration
        return envelope

    async def result(self) -> ResultEvent | SuspendedEvent:
        """Read the rest of the run and return how it stopped.

        Returns:
            The `result` event of a finished run, or the `suspended` event of
            a run waiting for `Agent.approve`.

        Raises:
            RunError: The server ended the run with an error.
            ConnectionClosed: The server went away first.
        """
        while await self._next() is not None:
            pass
        if self._terminal is None:
            raise LinchError(f"run {self.id} ended without a result or suspended event")
        return self._terminal

    async def cancel(self) -> bool:
        """Abort the run; it ends with a `result` of subtype `aborted`.

        Returns:
            Whether the server was driving the run; `False` when it had
            already ended.
        """
        answer = await self._conn.request("run.cancel", {"run_id": self.id})
        return bool(answer["cancelled"])

    async def _next(self) -> Envelope | None:
        while not self._received and self._end is None:
            item = await self._queue.get()
            if isinstance(item, RunEnd):
                self._end = item
            else:
                self._received.extend(item)
        if self._received:
            envelope = decode_envelope(self._received.popleft())
            if isinstance(envelope.event, ResultEvent | SuspendedEvent):
                self._terminal = envelope.event
            if self._observer is not None:
                self._observer.observe(envelope)
            return envelope
        assert self._end is not None
        error = self._end.error
        if self._observer is not None:
            if error is not None:
                self._observer.fail(type(error).__name__)
            else:
                self._observer.close()
        if error is not None:
            raise error
        return None
