from __future__ import annotations

from typing import Any

import pytest


class RecordingTool:
    name = "Record"
    description = "Records input."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    scope = "read"
    parallel = False

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        return f"Record({input.get('value', '')})"

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        from linch import ToolResult

        self.inputs.append(dict(input))
        return ToolResult(content=f"tool:{input.get('value', '')}")


def _agent(provider: Any, *, hooks: Any = None, tools: Any = None):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    return Agent(
        model="test-model",
        provider=provider,
        tools=tools or empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        hooks=hooks,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
    )


async def _collect(session: Any, prompt: str = "go") -> list[Any]:
    return [event async for event in session.run(prompt)]


@pytest.mark.asyncio
async def test_hook_dispatcher_orders_sync_async_and_isolates_errors() -> None:
    from linch import HookDispatcher, HookEvent, HookResult, UserPromptSubmitContext

    calls: list[str] = []

    class First:
        def on_user_prompt_submit(self, ctx):
            calls.append("first")
            return HookResult.mutate(prompt=ctx.prompt + "-first")

    class Broken:
        def on_user_prompt_submit(self, ctx):
            calls.append("broken")
            raise RuntimeError("boom")

    class Last:
        async def on_user_prompt_submit(self, ctx):
            calls.append("last")
            assert ctx.prompt == "hi-first"
            return HookResult.mutate(prompt=ctx.prompt + "!")

    ctx = UserPromptSubmitContext(session=object(), run_id="r1", turn_index=0, prompt="hi")
    result = await HookDispatcher([First(), Broken(), Last()]).dispatch(
        HookEvent.USER_PROMPT_SUBMIT,
        ctx,
    )

    assert calls == ["first", "broken", "last"]
    assert result.result.action == "mutate"
    assert result.result.prompt == "hi-first!"
    assert [event.action for event in result.events] == ["mutate", "error", "mutate"]


@pytest.mark.asyncio
async def test_prompt_and_request_hooks_mutate_provider_request() -> None:
    from linch import HookResult, TextBlock
    from linch.evals import ScriptedProvider, TextTurn
    from linch.types import SystemBlock

    class CaptureProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__([TextTurn(text="done")])
            self.requests: list[Any] = []

        async def stream(self, req):
            self.requests.append(req)
            async for event in super().stream(req):
                yield event

    class Hooks:
        def on_user_prompt_submit(self, ctx):
            return HookResult.mutate(prompt=ctx.prompt + " rewritten")

        def on_before_provider_call(self, ctx):
            assert ctx.request is not None
            ctx.request.system.append(SystemBlock(text="HOOK_SYSTEM"))
            return HookResult.mutate(request=ctx.request)

    provider = CaptureProvider()
    agent = _agent(provider, hooks=[Hooks()])
    session = await agent.session()
    events = await _collect(session, "hello")

    assert events[-1].type == "result"
    request = provider.requests[0]
    user_text = [
        block.text
        for message in request.messages
        for block in message.content
        if isinstance(block, TextBlock)
    ]
    assert "hello rewritten" in user_text
    assert any(block.text == "HOOK_SYSTEM" for block in request.system)


@pytest.mark.asyncio
async def test_tool_hooks_rewrite_input_block_and_result() -> None:
    from linch import HookResult, ToolCallEndEvent, ToolCallStartEvent, ToolRegistry
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.add(tool)

    class Hooks:
        def on_pre_tool_use(self, ctx):
            return HookResult.mutate(input={"value": "rewritten"})

        def on_post_tool_use(self, ctx):
            from linch import ToolResult

            return HookResult.mutate(tool_result=ToolResult(content="redacted"))

    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="Record", tool_input={"value": "raw"}), TextTurn(text="done")]
    )
    agent = _agent(provider, hooks=[Hooks()], tools=registry)
    session = await agent.session()
    events = await _collect(session)

    start = next(event for event in events if isinstance(event, ToolCallStartEvent))
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert start.input == {"value": "rewritten"}
    assert tool.inputs == [{"value": "rewritten"}]
    assert end.result == "redacted"


@pytest.mark.asyncio
async def test_tool_hook_can_block_execution() -> None:
    from linch import HookResult, ToolCallEndEvent, ToolRegistry
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.add(tool)

    class Hooks:
        def on_pre_tool_use(self, ctx):
            return HookResult.block("blocked by hook")

    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="Record", tool_input={"value": "raw"}), TextTurn(text="done")]
    )
    agent = _agent(provider, hooks=[Hooks()], tools=registry)
    session = await agent.session()
    events = await _collect(session)

    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert end.is_error is True
    assert end.result == "blocked by hook"
    assert tool.inputs == []


@pytest.mark.asyncio
async def test_final_answer_hook_retries_then_mutates_final_text() -> None:
    from linch import HookResult
    from linch.evals import ScriptedProvider, TextTurn

    class Hooks:
        def __init__(self) -> None:
            self.calls = 0

        def on_before_final_answer(self, ctx):
            self.calls += 1
            if self.calls == 1:
                return HookResult.retry("try again")
            return HookResult.mutate(final_text=f"{ctx.final_text}!")

    hooks = Hooks()
    provider = ScriptedProvider([TextTurn(text="draft"), TextTurn(text="final")])
    agent = _agent(provider, hooks=[hooks])
    session = await agent.session()
    events = await _collect(session)

    assert events[-1].type == "result"
    assert events[-1].final_text == "final!"
    assert hooks.calls == 2


@pytest.mark.asyncio
async def test_stop_hook_force_continue_counts_as_another_turn() -> None:
    from linch import HookResult
    from linch.evals import ScriptedProvider, TextTurn

    class Hooks:
        def __init__(self) -> None:
            self.calls = 0

        def on_stop(self, ctx):
            self.calls += 1
            if self.calls == 1:
                return HookResult.force_continue("continue once")
            return None

    hooks = Hooks()
    provider = ScriptedProvider([TextTurn(text="first"), TextTurn(text="second")])
    agent = _agent(provider, hooks=[hooks])
    session = await agent.session()
    events = await _collect(session)

    assert events[-1].type == "result"
    assert events[-1].final_text == "second"
    assert hooks.calls == 2


@pytest.mark.asyncio
async def test_agent_lifecycle_and_event_hooks_fire() -> None:
    from linch.evals import ScriptedProvider, TextTurn

    calls: list[str] = []

    class Hooks:
        def on_agent_start(self, ctx):
            calls.append(f"agent_start:{ctx.prompt}")

        def on_turn_start(self, ctx):
            calls.append(f"turn_start:{ctx.turn_index}")

        def on_provider_call_start(self, ctx):
            calls.append(f"provider_start:{ctx.model}")

        def on_provider_call_stop(self, ctx):
            calls.append(f"provider_stop:{ctx.stop_reason}")

        def on_turn_stop(self, ctx):
            calls.append(f"turn_stop:{ctx.turn_index}")

        def on_event_emit(self, ctx):
            if getattr(ctx.event, "type", None) == "result":
                calls.append("event:result")

        def on_agent_stop(self, ctx):
            calls.append(f"agent_stop:{ctx.result.subtype}")

    provider = ScriptedProvider([TextTurn(text="done")])
    agent = _agent(provider, hooks=[Hooks()])
    session = await agent.session()
    events = await _collect(session, "hello")

    assert events[-1].type == "result"
    assert calls == [
        "agent_start:hello",
        "turn_start:0",
        "provider_start:test-model",
        "provider_stop:end_turn",
        "turn_stop:0",
        "event:result",
        "agent_stop:success",
    ]


@pytest.mark.asyncio
async def test_tool_middleware_raising_before_call_fails_closed() -> None:
    """Regression: a before_tool_call middleware that raises must block the
    tool (fail closed), not let it run with the original input."""
    from linch import ToolCallEndEvent, ToolRegistry
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.hooks import ToolMiddlewareHook

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.add(tool)

    class VetoMiddleware:
        def before_tool_call(self, call, ctx):
            raise RuntimeError("veto")

    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="Record", tool_input={"value": "raw"}), TextTurn(text="done")]
    )
    agent = _agent(provider, hooks=[ToolMiddlewareHook(VetoMiddleware())], tools=registry)
    session = await agent.session()
    events = await _collect(session)

    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert end.is_error is True
    assert tool.inputs == []  # tool never executed


@pytest.mark.asyncio
async def test_tool_middleware_raising_after_result_fails_closed() -> None:
    """Regression: an after_tool_result middleware that raises must yield an
    error result, not let the original (unredacted) result pass through."""
    from linch import ToolCallEndEvent, ToolRegistry
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.hooks import ToolMiddlewareHook

    tool = RecordingTool()
    registry = ToolRegistry()
    registry.add(tool)

    class BoomMiddleware:
        def after_tool_result(self, call, result, ctx):
            raise RuntimeError("redact failed")

    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="Record", tool_input={"value": "secret"}), TextTurn(text="done")]
    )
    agent = _agent(provider, hooks=[ToolMiddlewareHook(BoomMiddleware())], tools=registry)
    session = await agent.session()
    events = await _collect(session)

    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert end.is_error is True
    assert "secret" not in str(end.result)


@pytest.mark.asyncio
async def test_blocked_prompt_still_dispatches_agent_stop() -> None:
    """Regression: a UserPromptSubmit block returns before the main
    try/finally, but AgentStop/on_run_end must still fire."""
    from linch import HookResult
    from linch.evals import ScriptedProvider, TextTurn

    stops: list[str] = []

    class Hooks:
        def on_user_prompt_submit(self, ctx):
            return HookResult.block("nope")

        def on_agent_stop(self, ctx):
            stops.append(ctx.result.subtype)

    provider = ScriptedProvider([TextTurn(text="unreached")])
    agent = _agent(provider, hooks=[Hooks()])
    session = await agent.session()
    events = await _collect(session)

    assert events[-1].type == "result"
    assert events[-1].subtype == "error"
    assert stops == ["error"]
    assert provider._index == 0  # provider never called


@pytest.mark.asyncio
async def test_before_final_answer_force_continue_retries() -> None:
    """Regression: force_continue at BeforeFinalAnswer must bounce the loop,
    not be silently ignored."""
    from linch import HookResult
    from linch.evals import ScriptedProvider, TextTurn

    class Hooks:
        def __init__(self) -> None:
            self.calls = 0

        def on_before_final_answer(self, ctx):
            self.calls += 1
            if self.calls == 1:
                return HookResult.force_continue("again")
            return None

    hooks = Hooks()
    provider = ScriptedProvider([TextTurn(text="first"), TextTurn(text="second")])
    agent = _agent(provider, hooks=[hooks])
    session = await agent.session()
    events = await _collect(session)

    assert events[-1].final_text == "second"
    assert hooks.calls == 2


@pytest.mark.asyncio
async def test_after_provider_call_force_continue_retries() -> None:
    """Regression: retry/force_continue at AfterProviderCall must bounce the
    loop instead of being dropped."""
    from linch import HookResult
    from linch.evals import ScriptedProvider, TextTurn

    class Hooks:
        def __init__(self) -> None:
            self.calls = 0

        def on_after_provider_call(self, ctx):
            self.calls += 1
            if self.calls == 1:
                return HookResult.force_continue("again")
            return None

    hooks = Hooks()
    provider = ScriptedProvider([TextTurn(text="first"), TextTurn(text="second")])
    agent = _agent(provider, hooks=[hooks])
    session = await agent.session()
    events = await _collect(session)

    assert events[-1].type == "result"
    assert events[-1].final_text == "second"
    assert hooks.calls == 2


@pytest.mark.asyncio
async def test_after_provider_call_mutation_keeps_provider_usage() -> None:
    """Regression: an AfterProviderCall mutation that rebuilds the assembly with
    fresh Usage() must not undercharge — accounting uses the provider's usage."""
    from linch import HookResult
    from linch.evals import ScriptedProvider, TextTurn
    from linch.events import UsageEvent
    from linch.types import AssistantAssembly, Message, TextBlock, Usage

    class Hooks:
        def on_after_provider_call(self, ctx):
            original = ctx.assembly
            return HookResult.mutate(
                assembly=AssistantAssembly(
                    message=Message(role="assistant", content=[TextBlock(text="redacted")]),
                    stop_reason=original.stop_reason,
                    usage=Usage(),  # hook drops usage to zero
                )
            )

    provider = ScriptedProvider(
        [TextTurn(text="secret", usage=Usage(input_tokens=10, output_tokens=5))]
    )
    agent = _agent(provider, hooks=[Hooks()])
    session = await agent.session()
    events = await _collect(session)

    usage_events = [event for event in events if isinstance(event, UsageEvent)]
    assert usage_events
    assert usage_events[0].usage.input_tokens == 10
    assert usage_events[0].usage.output_tokens == 5


@pytest.mark.asyncio
async def test_stop_when_runs_through_before_provider_hook() -> None:
    from linch import HookEventRecord
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.hooks import StopPredicateHook

    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="Record", tool_input={"value": "raw"}), TextTurn(text="never")]
    )
    tool = RecordingTool()
    from linch import ToolRegistry

    registry = ToolRegistry()
    registry.add(tool)
    agent = _agent(provider, tools=registry)
    session = await agent.session()

    def saw_tool_result(sess) -> bool:
        return any(
            getattr(block, "type", None) == "tool_result"
            for message in sess.provider_view
            for block in message.content
        )

    agent.hooks = [StopPredicateHook(saw_tool_result)]
    events = [event async for event in session.run("go")]

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert provider._index == 1
    assert any(
        isinstance(event, HookEventRecord)
        and event.event == "BeforeProviderCall"
        and event.hook == "stop_when"
        and event.action == "stop"
        for event in events
    )
