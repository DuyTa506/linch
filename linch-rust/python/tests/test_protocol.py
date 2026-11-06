"""Wire decoding and dispatch, on in-memory streams instead of a server."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from linch_rust import (
    AgentNotFound,
    OpenError,
    PromptBlocked,
    ProtocolError,
    RpcError,
    RunError,
    RunTerminal,
    SessionBusy,
    ToolCallEndEvent,
    UnknownEvent,
)
from linch_rust._rpc import Connection, RunEnd
from linch_rust.errors import error_from
from linch_rust.events import decode_envelope


class Answering:
    """A stream writer that answers each request with canned lines, in one chunk."""

    def __init__(self, reader: asyncio.StreamReader, lines: list[dict[str, Any]]) -> None:
        self.reader = reader
        self.lines = lines

    def write(self, data: bytes) -> None:
        request = json.loads(data)
        answer = [
            {**line, "id": request["id"]} if "result" in line else line for line in self.lines
        ]
        self.reader.feed_data(b"".join(json.dumps(line).encode() + b"\n" for line in answer))

    async def drain(self) -> None:
        return None


async def test_events_read_with_the_run_answer_reach_the_run() -> None:
    envelope = {"v": 1, "seq": 1, "run_id": "r1", "event": {"type": "custom", "x": 1}}
    lines = [
        {"jsonrpc": "2.0", "result": {"run_id": "r1"}},
        {"jsonrpc": "2.0", "method": "run.event", "params": envelope},
        {"jsonrpc": "2.0", "method": "run.end", "params": {"run_id": "r1"}},
    ]
    reader = asyncio.StreamReader()
    conn = Connection(reader, cast(asyncio.StreamWriter, Answering(reader, lines)))

    run_id, queue = await conn.request_run("run.start", {})

    assert run_id == "r1"
    assert [queue.get_nowait(), queue.get_nowait()] == [envelope, RunEnd(None)]
    reader.feed_eof()
    await conn.finish()


def test_events_decode_tolerantly_and_keep_unknown_kinds() -> None:
    end = decode_envelope(
        {
            "v": 1,
            "seq": 9,
            "run_id": "r1",
            "event": {
                "type": "tool_call_end",
                "tool_name": "lookup",
                "result": {"tool_use_id": "c1", "content": [], "is_error": True},
                "duration_ms": 3,
                "added_later": "ignored",
            },
        }
    )
    other = decode_envelope(
        {"v": 1, "seq": 9, "run_id": "r1", "ignorable": True, "event": {"type": "x", "a": 1}}
    )

    assert end.seq == 9 and end.run_id == "r1"
    assert isinstance(end.event, ToolCallEndEvent)
    assert end.event.tool_use_id == "c1" and end.event.is_error
    assert other.event == UnknownEvent("x", {"a": 1})


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        (-32602, ProtocolError),
        (-32001, AgentNotFound),
        (-32013, OpenError),
        (-32026, RunTerminal),
        (-32024, RunError),
        (-32030, SessionBusy),
        (-1, RpcError),
    ],
)
def test_error_codes_map_to_the_exception_tree(code: int, cls: type[RpcError]) -> None:
    error = error_from({"code": code, "message": "boom", "data": {"status": "completed"}})

    assert type(error) is cls
    assert (error.code, error.message, error.data) == (code, "boom", {"status": "completed"})


def test_a_blocked_prompt_carries_its_reason() -> None:
    error = error_from({"code": -32029, "message": "blocked", "data": {"reason": "secret"}})

    assert isinstance(error, PromptBlocked) and isinstance(error, RunError)
    assert error.reason == "secret"


def test_a_busy_session_names_its_live_run() -> None:
    data = {"session_id": "s1", "run_id": "live-1"}
    error = error_from({"code": -32030, "message": "busy", "data": data})

    assert isinstance(error, SessionBusy) and isinstance(error, RunError)
    assert (error.session_id, error.run_id) == ("s1", "live-1")
    bare = error_from({"code": -32030, "message": "busy"})
    assert isinstance(bare, SessionBusy) and bare.run_id == ""
