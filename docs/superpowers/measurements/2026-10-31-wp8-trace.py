from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))

from linch import Agent, InMemoryRunStore
from linch.events import Event
from linch.sessions import InMemorySessionStore
from linch.tools import ToolContext, ToolRegistry, ToolResult
from linch.types import Usage


class Provider:
    id = "trace-provider"

    def __init__(self, *, tool_turn: bool) -> None:
        self.calls = 0
        self.tool_turn = tool_turn

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, request):
        self.calls += 1
        yield {"type": "message_start", "model": request.model}
        if self.tool_turn and self.calls == 1:
            for name, tool_id in (("trace_read_a", "fixed-call-a"), ("trace_read_b", "fixed-call-b")):
                yield {"type": "tool_use_start", "id": tool_id, "name": name}
                yield {"type": "tool_use_input_delta", "id": tool_id, "json_delta": '{"value":"x"}'}
                yield {"type": "tool_use_end", "id": tool_id}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
        elif self.tool_turn and self.calls == 2:
            yield {"type": "tool_use_start", "id": "fixed-call-write", "name": "trace_write"}
            yield {
                "type": "tool_use_input_delta",
                "id": "fixed-call-write",
                "json_delta": '{"value":"x"}',
            }
            yield {"type": "tool_use_end", "id": "fixed-call-write"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
        else:
            yield {"type": "text_delta", "text": "ok"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


class Tool:
    def __init__(self, name: str, *, scope: str = "read") -> None:
        self.name = name
        self.scope = scope
        self.parallel = scope == "read"
    description = "trace tool"
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    def validate(self, raw):
        return raw

    def summarize(self, input):
        return self.name

    async def execute(self, input, ctx: ToolContext):
        return ToolResult(content="tool:ok")


def normalize(value):
    if dataclasses.is_dataclass(value):
        return normalize(dataclasses.asdict(value))
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in {"session_id", "run_id", "created_at", "updated_at", "timestamp", "emitted_at"}:
                out[key] = "<normalized>"
            elif key == "id" and not (
                isinstance(item, str) and item.startswith(("fixed-call-", "call-"))
            ):
                # Keep deterministic provider/tool references in normalized
                # traces; generated UUID/event identities still disappear.
                out[key] = "<normalized>"
            elif key in {"duration_ms", "elapsed_ms", "latency_ms"}:
                out[key] = "<timing>"
            else:
                out[str(key)] = normalize(item)
        return dict(sorted(out.items()))
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, Event):
        return normalize(dataclasses.asdict(value))
    if hasattr(value, "value") and type(value).__name__.endswith("Enum"):
        return normalize(value.value)
    return value


async def collect(tool_turn: bool):
    tools = ToolRegistry()
    if tool_turn:
        tools.register(Tool("trace_read_a"))
        tools.register(Tool("trace_read_b"))
        tools.register(Tool("trace_write", scope="write"))
    agent = Agent(
        model="gpt-5",
        provider=Provider(tool_turn=tool_turn),
        tools=tools,
        session_store=InMemorySessionStore(),
        run_store=InMemoryRunStore(),
        cwd="/home/duy/.codex/worktrees/scale-hot-path/agent_kit",
        permissions={"mode": "skip-dangerous"},
        result_offload=None,
        include_partial_messages=True,
    )
    try:
        session = await agent.session()
        events = [event async for event in session.run("trace")]
        return [normalize(event) for event in events]
    finally:
        await agent.close()


async def main():
    payload = {"text_only": await collect(False), "multi_tool": await collect(True)}
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))


asyncio.run(main())
