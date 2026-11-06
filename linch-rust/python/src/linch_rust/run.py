"""`Run`: the event stream of one run the server drives."""

from __future__ import annotations

import asyncio

from ._rpc import Connection, RunEnd, RunItem
from .errors import LinchError
from .events import Envelope, ResultEvent, SuspendedEvent, decode_envelope


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

    def __init__(self, conn: Connection, run_id: str, queue: asyncio.Queue[RunItem]) -> None:
        self.id = run_id
        self._conn = conn
        self._queue = queue
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
        if self._end is None:
            item = await self._queue.get()
            if not isinstance(item, RunEnd):
                envelope = decode_envelope(item)
                if isinstance(envelope.event, ResultEvent | SuspendedEvent):
                    self._terminal = envelope.event
                return envelope
            self._end = item
        if self._end.error is not None:
            raise self._end.error
        return None
