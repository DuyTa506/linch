"""Wire decoding and dispatch, on in-memory streams instead of a server."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from linch_rust import (
    AgentNotFound,
    NotSuspended,
    OpenError,
    PromptBlocked,
    ProtocolError,
    ResultEvent,
    RpcError,
    RunError,
    RunTerminal,
    SessionBusy,
    ToolCallEndEvent,
    UnknownEvent,
    UsageEvent,
)
from linch_rust._rpc import Connection, RunEnd
from linch_rust.errors import error_from
from linch_rust.events import decode_envelope, decode_event


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
    batch = {"run_id": "r1", "envelopes": [envelope, {**envelope, "seq": 2}]}
    lines = [
        {"jsonrpc": "2.0", "result": {"run_id": "r1"}},
        {"jsonrpc": "2.0", "method": "run.event", "params": batch},
        {"jsonrpc": "2.0", "method": "run.end", "params": {"run_id": "r1"}},
    ]
    reader = asyncio.StreamReader()
    conn = Connection(reader, cast(asyncio.StreamWriter, Answering(reader, lines)))

    run_id, queue = await conn.request_run("run.start", {})

    assert run_id == "r1"
    assert [queue.get_nowait(), queue.get_nowait()] == [batch["envelopes"], RunEnd(None)]
    reader.feed_eof()
    await conn.finish()


async def test_a_run_that_failed_inside_the_server_ends_with_a_run_error() -> None:
    panic = {"code": -32603, "message": "the run failed inside the server (a panic)"}
    lines = [
        {"jsonrpc": "2.0", "result": {"run_id": "r1"}},
        {"jsonrpc": "2.0", "method": "run.end", "params": {"run_id": "r1", "error": panic}},
    ]
    reader = asyncio.StreamReader()
    conn = Connection(reader, cast(asyncio.StreamWriter, Answering(reader, lines)))

    _, queue = await conn.request_run("run.start", {})
    end = queue.get_nowait()

    assert isinstance(end, RunEnd)
    assert type(end.error) is RunError
    assert (end.error.code, end.error.message) == (-32603, panic["message"])
    # Answering a request, the same code is still a protocol error.
    assert type(error_from(panic)) is ProtocolError
    reader.feed_eof()
    await conn.finish()


def test_events_decode_tolerantly_and_keep_unknown_kinds() -> None:
    end = decode_envelope(
        {
            "v": 1,
            "seq": 9,
            "run_id": "r1",
            "ts_ms": 1_762_400_000_000,
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
        {
            "v": 1,
            "seq": 9,
            "run_id": "r1",
            "ts_ms": 0,
            "ignorable": True,
            "event": {"type": "x", "a": 1},
        }
    )

    assert end.seq == 9 and end.run_id == "r1"
    assert isinstance(end.event, ToolCallEndEvent)
    assert end.event.tool_use_id == "c1" and end.event.is_error
    assert other.event == UnknownEvent("x", {"a": 1})


def test_envelopes_and_results_carry_their_time_and_cost() -> None:
    usage = {"input_tokens": 3, "output_tokens": 1}
    result = decode_envelope(
        {
            "v": 1,
            "seq": 4,
            "run_id": "r1",
            "ts_ms": 1_762_400_000_123,
            "event": {
                "type": "result",
                "subtype": "success",
                "stop_reason": "end_turn",
                "total_usage": usage,
                "usage_complete": True,
                "duration_ms": 42,
                "total_cost_usd": 0.25,
            },
        }
    )
    unpriced = decode_event(
        {
            "type": "result",
            "subtype": "success",
            "stop_reason": "end_turn",
            "total_usage": usage,
            "usage_complete": True,
            "duration_ms": 7,
        }
    )
    spent = decode_event(
        {
            "type": "usage",
            "usage": usage,
            "cumulative": usage,
            "cost_usd": 0.25,
            "cumulative_cost_usd": 0.5,
        }
    )

    assert result.ts_ms == 1_762_400_000_123
    assert isinstance(result.event, ResultEvent)
    assert (result.event.duration_ms, result.event.total_cost_usd) == (42, 0.25)
    assert isinstance(unpriced, ResultEvent) and unpriced.total_cost_usd is None
    assert isinstance(spent, UsageEvent)
    assert (spent.cost_usd, spent.cumulative_cost_usd) == (0.25, 0.5)


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        (-32602, ProtocolError),
        (-32001, AgentNotFound),
        (-32013, OpenError),
        (-32026, RunTerminal),
        (-32027, NotSuspended),
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
