from __future__ import annotations

from collections.abc import AsyncIterator

from linch import Agent
from linch.filesystem import OffloadConfig, StateFileBackend
from linch.filesystem.offload import maybe_offload
from linch.sessions import InMemorySessionStore
from linch.tools.base import ToolContext, ToolResult
from linch.tools.registry import tools_from_defaults
from linch.types import Usage

BIG = "\n".join(f"result line {i} with some filler text" for i in range(200))


class BigTool:
    name = "big_search"
    description = "Returns a large result."
    input_schema = {"type": "object", "properties": {}}
    scope = "read"
    parallel_safe = True
    parallel = True

    def validate(self, raw: dict) -> dict:
        return {}

    async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=BIG, summary="big")

    def summarize(self, input: dict) -> str:
        return "big_search()"


class FakeProvider:
    id = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req) -> AsyncIterator[dict]:
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self.calls == 1:
            yield {"type": "tool_use_start", "id": "call_1", "name": "big_search"}
            yield {"type": "tool_use_input_delta", "id": "call_1", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "call_1"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
        else:
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


# ── Unit: maybe_offload ──────────────────────────────────────────────────────


async def test_maybe_offload_replaces_large_content() -> None:
    backend = StateFileBackend()
    result = ToolResult(content=BIG)
    out = await maybe_offload(
        result,
        tool_name="big_search",
        call_id="abc",
        backend=backend,
        config=OffloadConfig(threshold_tokens=10, preview_lines=3),
    )
    assert out.truncated
    assert "offloaded to" in out.content
    assert len(out.content) < len(BIG)
    path = out.metadata["offloaded_to"]
    assert await backend.read(path) == BIG  # full content preserved in backend


async def test_maybe_offload_skips_small_results() -> None:
    backend = StateFileBackend()
    result = ToolResult(content="small")
    out = await maybe_offload(
        result,
        tool_name="t",
        call_id="x",
        backend=backend,
        config=OffloadConfig(threshold_tokens=10_000),
    )
    assert out.content == "small"
    assert not out.truncated
    assert await backend.ls() == []


async def test_maybe_offload_skips_errors_and_fs_tools() -> None:
    backend = StateFileBackend()
    err = await maybe_offload(
        ToolResult(content=BIG, is_error=True),
        tool_name="big_search",
        call_id="x",
        backend=backend,
        config=OffloadConfig(threshold_tokens=1),
    )
    assert err.content == BIG  # errors are never offloaded

    fs = await maybe_offload(
        ToolResult(content=BIG),
        tool_name="read_file",  # in default skip_tools
        call_id="x",
        backend=backend,
        config=OffloadConfig(threshold_tokens=1),
    )
    assert fs.content == BIG


# ── End-to-end through the loop ──────────────────────────────────────────────


async def test_offload_end_to_end() -> None:
    agent = Agent(
        model="gpt-5",
        provider=FakeProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=tools_from_defaults(extra=[BigTool()]),
        result_offload=OffloadConfig(threshold_tokens=10, preview_lines=5),
    )
    session = await agent.session()
    events = [e async for e in session.run("search")]

    end = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "big_search")
    # The string the LLM sees is shrunk and references a file...
    assert "offloaded to" in end.result
    assert len(end.result) < len(BIG)
    # ...but the full structured result still rides along for observers.
    assert end.tool_result is not None
    assert end.tool_result.content == BIG
    assert not end.tool_result.truncated

    # Full content lives in the session filesystem.
    paths = await session.filesystem.ls("/offload")
    assert len(paths) == 1
    path = paths[0]
    assert await session.filesystem.read(path) == BIG

    # And the persisted provider_view never contains the full payload.
    persisted = "".join(
        block.content
        for msg in session.provider_view
        for block in msg.content
        if getattr(block, "type", None) == "tool_result"
    )
    assert BIG not in persisted


async def test_no_offload_when_disabled() -> None:
    agent = Agent(
        model="gpt-5",
        provider=FakeProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=tools_from_defaults(extra=[BigTool()]),
        result_offload=None,  # explicitly disable
    )
    session = await agent.session()
    assert session.filesystem is None
    events = [e async for e in session.run("search")]
    end = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "big_search")
    assert end.result == BIG  # untouched
