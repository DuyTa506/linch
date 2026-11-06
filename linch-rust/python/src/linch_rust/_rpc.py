"""JSON-RPC 2.0 with the server over its stdin and stdout, one message per line.

One reader task reads every server line: responses resolve pending calls,
`run.event` (consecutive envelopes of one run, as one list) and `run.end`
go to the run's queue, and `tool.call` and `hook.call` run as tasks that
`$/cancelRequest` cancels.

Server lines are parsed with `orjson`, about three times faster than `json`
on envelopes and stricter (no `NaN` or `Infinity`); what this client sends is
encoded with `json`, which refuses non-finite numbers instead of writing
`null`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import orjson

from .callbacks import Callback
from .errors import ConnectionClosed, LinchError, error_from, run_end_error
from .tools import HostTool, ToolOutput

JsonObject = dict[str, Any]
# A host tool's or callback's own failure; the code is application-defined.
_HANDLER_ERROR = -32000
_METHOD_NOT_FOUND = -32601


@dataclass(frozen=True)
class RunEnd:
    """The last item of a run's queue: `run.end`, or the connection closing."""

    error: LinchError | None


# A `run.event`'s envelopes, in order, or the end.
RunItem = list[JsonObject] | RunEnd


@dataclass(frozen=True)
class _Pending:
    future: asyncio.Future[JsonObject]
    run_queue: asyncio.Queue[RunItem] | None


@dataclass(frozen=True)
class _Handlers:
    tools: Mapping[str, HostTool]
    callbacks: Mapping[tuple[str, str], Callback]


class _Refused(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _encode(message: JsonObject) -> bytes:
    return (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _error_line(id: Any, code: int, message: str) -> bytes:
    return _encode({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


class Connection:
    """The client end of one server process's stdio."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._runs: dict[str, asyncio.Queue[RunItem]] = {}
        self._agents: dict[str, _Handlers] = {}
        self._served: dict[Any, asyncio.Task[None]] = {}
        self._closed: str | None = None
        self._task = asyncio.create_task(self._read())

    def bind(
        self,
        agent: str,
        tools: Mapping[str, HostTool],
        callbacks: Mapping[tuple[str, str], Callback],
    ) -> None:
        """Route the server's `tool.call` and `hook.call` for `agent` to these."""
        self._agents[agent] = _Handlers(tools, callbacks)

    async def request(self, method: str, params: JsonObject) -> JsonObject:
        """Call `method` and return its result object."""
        return await self._call(method, params, None)

    async def request_run(
        self, method: str, params: JsonObject
    ) -> tuple[str, asyncio.Queue[RunItem]]:
        """Call a run method and return the run id and the queue of its events."""
        queue: asyncio.Queue[RunItem] = asyncio.Queue()
        result = await self._call(method, params, queue)
        return str(result["run_id"]), queue

    async def finish(self) -> None:
        """Wait for the reader to see the end of output, then for served tasks."""
        await self._task
        await asyncio.gather(*self._served.values(), return_exceptions=True)

    async def _call(
        self, method: str, params: JsonObject, queue: asyncio.Queue[RunItem] | None
    ) -> JsonObject:
        if self._closed is not None:
            raise ConnectionClosed(self._closed)
        id = next(self._ids)
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending[id] = _Pending(future, queue)
        try:
            await self._write(
                _encode({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
            )
            return await future
        finally:
            self._pending.pop(id, None)

    async def _write(self, line: bytes) -> None:
        if self._closed is not None:
            raise ConnectionClosed(self._closed)
        try:
            self._writer.write(line)
            await self._writer.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise ConnectionClosed(f"writing to the server failed: {error}") from error

    async def _read(self) -> None:
        reason = "the server closed its output"
        try:
            while line := await self._reader.readline():
                if line.strip():
                    self._dispatch(orjson.loads(line))
        except Exception as error:
            reason = f"reading the server failed: {error!r}"
        finally:
            self._shutdown(reason)

    def _dispatch(self, message: JsonObject) -> None:
        method = message.get("method")
        if method is None:
            self._resolve(message)
            return
        params: JsonObject = message.get("params") or {}
        if "id" in message:
            self._serve(message["id"], str(method), params)
        elif method == "run.event":
            queue = self._runs.get(params.get("run_id", ""))
            if queue is not None:
                queue.put_nowait(params.get("envelopes") or [])
        elif method == "run.end":
            queue = self._runs.pop(params.get("run_id", ""), None)
            error = params.get("error")
            if queue is not None:
                queue.put_nowait(RunEnd(run_end_error(error) if error else None))
        elif method == "$/cancelRequest":
            task = self._served.get(params.get("id"))
            if task is not None:
                task.cancel()

    def _resolve(self, message: JsonObject) -> None:
        pending = self._pending.pop(message.get("id", -1), None)
        if pending is None or pending.future.done():
            return
        if "error" in message:
            pending.future.set_exception(error_from(message["error"]))
            return
        result: JsonObject = message.get("result") or {}
        if pending.run_queue is not None:
            # Registered before any of the run's notifications is read.
            self._runs[str(result["run_id"])] = pending.run_queue
        pending.future.set_result(result)

    def _serve(self, id: Any, method: str, params: JsonObject) -> None:
        task = asyncio.create_task(self._answer(id, method, params))
        self._served[id] = task
        task.add_done_callback(lambda _: self._served.pop(id, None))

    async def _answer(self, id: Any, method: str, params: JsonObject) -> None:
        try:
            result = await self._handle(method, params)
            line = _encode({"jsonrpc": "2.0", "id": id, "result": result})
        except _Refused as error:
            line = _error_line(id, error.code, str(error))
        except Exception as error:
            line = _error_line(id, _HANDLER_ERROR, f"{type(error).__name__}: {error}")
        try:
            await self._write(line)
        except ConnectionClosed:
            pass  # The server is gone; the reader reports why.

    async def _handle(self, method: str, params: JsonObject) -> Any:
        handlers = self._agents.get(params.get("agent", ""))
        if method == "tool.call":
            name = params.get("name", "")
            tool = handlers.tools.get(name) if handlers else None
            if tool is None:
                raise _Refused(_METHOD_NOT_FOUND, f"no host tool {name!r}")
            output = await tool.fn(**params.get("input", {}))
            if isinstance(output, ToolOutput):
                return output.wire()
            return {"value": output}
        if method == "hook.call":
            key = (params.get("point", ""), params.get("policy_id", ""))
            found = handlers.callbacks.get(key) if handlers else None
            if found is None:
                raise _Refused(_METHOD_NOT_FOUND, f"no callback {key!r}")
            return await found.fn(params.get("request", {}))
        raise _Refused(_METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _shutdown(self, reason: str) -> None:
        self._closed = reason
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(ConnectionClosed(reason))
        self._pending.clear()
        for queue in self._runs.values():
            queue.put_nowait(RunEnd(ConnectionClosed(reason)))
        self._runs.clear()
        for task in self._served.values():
            task.cancel()
