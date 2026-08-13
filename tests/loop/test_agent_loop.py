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


async def test_stream_turn_retains_an_omitted_claude_thinking_signature() -> None:
    """A signature-only thinking block must survive until a tool result round-trip."""
    from linch.loop.streaming import stream_turn
    from linch.types import AssistantAssembly, ProviderRequest, ThinkingBlock

    class _SignatureOnlyProvider(BaseProvider):
        id = "signature-only"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            yield {"type": "thinking_delta", "text": "", "signature": "opaque-signature"}
            yield {"type": "tool_use_start", "id": "call_1", "name": "Read"}
            yield {"type": "tool_use_input_delta", "id": "call_1", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "call_1"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}

    agent = Agent(
        model="claude-opus-4-8",
        provider=_SignatureOnlyProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    req = ProviderRequest(model=agent.model, system=[], tools=[], messages=[])

    items = [item async for item in stream_turn(session, req)]
    assembly = items[-1]
    assert isinstance(assembly, AssistantAssembly)
    thinking = next(block for block in assembly.message.content if isinstance(block, ThinkingBlock))

    assert thinking.thinking == ""
    assert thinking.signature == "opaque-signature"


async def test_stream_partials_request_override_suppresses_events_but_keeps_assembly() -> None:
    """A request can suppress raw deltas without altering provider assembly."""
    from linch.events import PartialAssistantEvent
    from linch.loop.streaming import stream_turn
    from linch.types import (
        AssistantAssembly,
        ProviderRequest,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
    )

    class _StreamingProvider(BaseProvider):
        id = "streaming"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "hello"}
            yield {"type": "thinking_delta", "text": "considering"}
            yield {"type": "tool_use_start", "id": "call_1", "name": "Read"}
            yield {"type": "tool_use_input_delta", "id": "call_1", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "call_1"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}

    agent = Agent(
        model="gpt-5",
        provider=_StreamingProvider(),
        session_store=InMemorySessionStore(),
        include_partial_messages=True,
    )
    session = await agent.session()
    req = ProviderRequest(
        model=agent.model,
        system=[],
        tools=[],
        messages=[],
        stream_partials=False,
    )

    legacy_items = [
        item
        async for item in stream_turn(
            session,
            ProviderRequest(model=agent.model, system=[], tools=[], messages=[]),
        )
    ]
    items = [item async for item in stream_turn(session, req)]

    assert len([item for item in legacy_items if isinstance(item, PartialAssistantEvent)]) == 3
    assert [item for item in items if isinstance(item, PartialAssistantEvent)] == []
    assembly = items[-1]
    assert isinstance(assembly, AssistantAssembly)
    assert [type(block) for block in assembly.message.content] == [
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
    ]


async def test_run_options_thread_stream_partial_policy_to_provider_request() -> None:
    from linch import RunOptions
    from linch.loop.request import _build_turn_request

    agent = Agent(
        model="gpt-5",
        provider=FakeProvider(),
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()

    assert _build_turn_request(session, RunOptions()).stream_partials is None
    assert _build_turn_request(session, RunOptions(stream_partials=False)).stream_partials is False
