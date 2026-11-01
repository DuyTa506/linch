from __future__ import annotations

from linch import Agent
from linch.providers import BaseProvider
from linch.sessions import InMemorySessionStore
from linch.types import Usage
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.stores import memory_run_store
from tests.support.tools import StubTool, tool_registry


async def test_agent_loop_runs_tool_and_finishes() -> None:
    agent = make_agent(
        ScriptedProvider([ToolUseTurn("Read", {"file_path": "README.md"}), TextTurn("done")]),
        tools=tool_registry(StubTool("Read")),
        run_store=None,
    )
    session = await agent.session()

    events = [event async for event in session.run("read readme")]

    assert [event.type for event in events] == [
        "system",
        "user",
        "assistant",
        "usage",
        "tool_call_start",
        "tool_call_end",
        "user",
        "assistant",
        "usage",
        "result",
    ]
    assert events[0].session_id == session.id
    assert events[0].run_id
    assert events[2].stop_reason == "tool_use"
    assert any(event.type == "tool_call_start" and event.tool_name == "Read" for event in events)
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"


async def test_text_only_event_order_and_terminal_fields() -> None:
    usage = Usage(input_tokens=2, output_tokens=3)
    agent = make_agent(
        ScriptedProvider([TextTurn("done", usage=usage)]),
        run_store=None,
    )
    session = await agent.session(id="text-only")

    events = [event async for event in session.run("hello")]

    assert [event.type for event in events] == ["system", "user", "assistant", "usage", "result"]
    system, _prompt, assistant, usage_event, result = events
    assert system.session_id == "text-only"
    assert system.model == "test-model"
    assert assistant.stop_reason == "end_turn"
    assert usage_event.cumulative == usage
    assert result.final_text == "done"


async def test_abort_emits_tool_result_before_terminal() -> None:
    session_ref: list[object] = []

    def abort_after_tool(_input: dict[str, object], _ctx: object) -> None:
        session_ref[0].abort()  # type: ignore[attr-defined]

    provider = ScriptedProvider(
        [ToolUseTurn("Abort", {"value": "now"}), TextTurn("should not be requested")]
    )
    agent = make_agent(
        provider,
        tools=tool_registry(StubTool("Abort", on_execute=abort_after_tool)),
        run_store=memory_run_store(),
    )
    session = await agent.session(id="abort")
    session_ref.append(session)

    events = [event async for event in session.run("abort after tool")]

    assert [event.type for event in events] == [
        "system",
        "user",
        "assistant",
        "usage",
        "tool_call_start",
        "tool_call_end",
        "user",
        "result",
    ]
    assert events[5].is_error is False
    assert events[6].subtype == "tool_result"
    assert events[-1].subtype == "aborted"
    assert events[-1].stop_reason == "error"
    assert len(provider.requests) == 1


async def test_public_api_has_provider_module() -> None:
    module = __import__("linch.providers")
    assert module is not None


async def test_stream_turn_retains_an_omitted_claude_thinking_signature() -> None:
    """A signature-only thinking block must survive until a tool result round-trip."""
    from linch.loop.streaming import _stream_turn
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

    items = [item async for item in _stream_turn(session, req)]
    assembly = items[-1]
    assert isinstance(assembly, AssistantAssembly)
    thinking = next(block for block in assembly.message.content if isinstance(block, ThinkingBlock))

    assert thinking.thinking == ""
    assert thinking.signature == "opaque-signature"


async def test_stream_partials_request_override_suppresses_events_but_keeps_assembly() -> None:
    """A request can suppress raw deltas without altering provider assembly."""
    from linch.events import PartialAssistantEvent
    from linch.loop.streaming import _stream_turn
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
        async for item in _stream_turn(
            session,
            ProviderRequest(model=agent.model, system=[], tools=[], messages=[]),
        )
    ]
    items = [item async for item in _stream_turn(session, req)]

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
        provider=ScriptedProvider([TextTurn("done")]),
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()

    assert _build_turn_request(session, RunOptions()).stream_partials is None
    assert _build_turn_request(session, RunOptions(stream_partials=False)).stream_partials is False


def test_missing_core_jsonschema_cannot_accept_structured_output() -> None:
    """A missing validation dependency must fail, never accept unchecked JSON."""
    import os
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import builtins
            import sys
            from types import SimpleNamespace

            # Warm unrelated core imports, then fault only this validation module.
            import linch.loop.terminals
            del sys.modules["linch.loop.terminals"]
            original_import = builtins.__import__
            def import_without_jsonschema(name, *args, **kwargs):
                if name == "jsonschema":
                    raise ModuleNotFoundError("missing core jsonschema")
                return original_import(name, *args, **kwargs)
            builtins.__import__ = import_without_jsonschema

            from linch.loop.terminals import _validate_structured_output
            _validate_structured_output(
                {"count": "invalid"},
                SimpleNamespace(schema={"type": "object", "properties": {
                    "count": {"type": "integer"}
                }}),
            )
        """),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "missing core validation dependency silently accepted output"
    assert "ModuleNotFoundError: missing core jsonschema" in result.stderr
