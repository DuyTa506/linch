from __future__ import annotations

from collections.abc import AsyncIterator

from linch import Agent
from linch.providers import BaseProvider
from linch.sessions import InMemorySessionStore
from linch.types import Usage


class FakeProvider(BaseProvider):
    id = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self.calls == 1:
            yield {"type": "tool_use_start", "id": "call_1", "name": "Read"}
            yield {
                "type": "tool_use_input_delta",
                "id": "call_1",
                "json_delta": '{"file_path":"README.md"}',
            }
            yield {"type": "tool_use_end", "id": "call_1"}
            yield {
                "type": "message_end",
                "stop_reason": "tool_use",
                "usage": Usage(),
            }
        else:
            yield {"type": "text_delta", "text": "done"}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(),
            }


async def test_agent_loop_runs_tool_and_finishes() -> None:
    agent = Agent(
        model="gpt-5",
        provider=FakeProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    session = await agent.session()

    events = [event async for event in session.run("read readme")]

    assert [event.type for event in events].count("assistant") == 2
    assert any(event.type == "tool_call_start" and event.tool_name == "Read" for event in events)
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"


async def test_public_api_has_provider_module() -> None:
    module = __import__("linch.providers")
    assert module is not None
