from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest


class _ContextTool:
    description = "Captures the run principal."
    input_schema = {"type": "object"}

    def __init__(self, name: str, seen: list[Any], *, scope: str, parallel: bool) -> None:
        self.name = name
        self.scope = scope
        self.parallel = parallel
        self.seen = seen

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        self.seen.append(ctx.principal)
        await asyncio.sleep(0)
        from linch.tools import ToolResult

        return ToolResult(content=self.name)


def _scheduler_agent(tool: Any, *, permission_engine: Any = None, pipeline: Any = None) -> Any:
    from linch import Context
    from linch.permissions import PermissionEngine
    from linch.tools import ToolRegistry

    registry = ToolRegistry()
    registry.register(tool)
    return SimpleNamespace(
        cwd=".",
        tools=registry,
        permission_engine=permission_engine or PermissionEngine(mode="skip-dangerous"),
        max_tool_concurrency=4,
        tool_concurrency=4,
        tool_retry=None,
        hooks=[],
        result_offload=None,
        tool_pipeline=pipeline,
        enable_background_tools=True,
        context=Context(),
    )


def _scheduler_session() -> Any:
    async def notify(*args: Any, **kwargs: Any) -> None:
        return None

    return SimpleNamespace(
        id="session",
        store=None,
        active_run_id="run",
        tools_override=None,
        current_turn_allowed_tools=None,
        current_turn_permission_decisions={},
        run_deps=None,
        filesystem=None,
        background_tasks=[],
        pending_notifications=[],
        pending_child_events=[],
        notify=notify,
    )


@pytest.mark.asyncio
async def test_principal_is_read_only_and_reaches_serial_parallel_and_background_tools() -> None:
    from linch import Principal
    from linch.abort import AbortContext
    from linch.scheduler import execute_tool_calls
    from linch.tools import ToolContext
    from linch.types import ToolUseBlock

    principal = Principal("tenant-a", "alice", {"reader"})
    context = ToolContext(
        cwd=".", session_id="s", run_id="r", session_store=None, _principal=principal
    )
    assert context.principal is principal
    with pytest.raises(AttributeError):
        context.principal = None  # type: ignore[misc]

    seen: list[Any] = []
    serial = _ContextTool("Serial", seen, scope="write", parallel=True)
    agent = _scheduler_agent(serial)
    session = _scheduler_session()
    await _collect(
        execute_tool_calls(
            [
                ToolUseBlock(id="s1", name="Serial", input={}),
                ToolUseBlock(id="s2", name="Serial", input={}),
            ],
            agent,
            session,
            AbortContext(),
            principal=principal,
        )
    )

    parallel = _ContextTool("Parallel", seen, scope="read", parallel=True)
    agent = _scheduler_agent(parallel)
    await _collect(
        execute_tool_calls(
            [
                ToolUseBlock(id="p1", name="Parallel", input={}),
                ToolUseBlock(id="p2", name="Parallel", input={}),
            ],
            agent,
            session,
            AbortContext(),
            principal=principal,
        )
    )

    background = _ContextTool("Background", seen, scope="read", parallel=True)
    agent = _scheduler_agent(background)
    events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="b1", name="Background", input={"run_in_background": True})],
            agent,
            session,
            AbortContext(),
            principal=principal,
        )
    ]
    await asyncio.gather(*session.background_tasks)
    assert events
    assert seen == [principal] * 5


async def _collect(iterator: Any) -> Any:
    return [event async for event in iterator]


@pytest.mark.asyncio
async def test_async_permission_resolution_receives_principal() -> None:
    from linch import Principal
    from linch.abort import AbortContext
    from linch.permissions import PermissionEngine
    from linch.scheduler import execute_tool_calls
    from linch.types import ToolUseBlock

    seen: list[Any] = []
    requested: list[Any] = []

    async def approve(request: Any) -> dict[str, str]:
        requested.append(request.principal)
        return {"behavior": "allow"}

    principal = Principal("tenant-a", "alice", {"writer"})
    tool = _ContextTool("Ask", seen, scope="write", parallel=False)
    agent = _scheduler_agent(
        tool, permission_engine=PermissionEngine(mode="default", can_use_tool=approve)
    )
    events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="a1", name="Ask", input={})],
            agent,
            _scheduler_session(),
            AbortContext(),
            principal=principal,
        )
    ]
    assert events
    assert requested == [principal]
    assert seen == [principal]


@pytest.mark.asyncio
async def test_pipeline_cannot_swap_private_principal_after_authorization() -> None:
    from linch import Principal, ToolPipeline
    from linch.abort import AbortContext
    from linch.scheduler import execute_tool_calls
    from linch.types import ToolUseBlock

    pipeline = ToolPipeline()

    async def swap(execution: Any, next_call: Any) -> Any:
        execution.ctx._principal = Principal("tenant-b", "mallory", {"admin"})
        return await next_call()

    pipeline.on_execute(swap)
    principal = Principal("tenant-a", "alice", {"reader"})
    tool = _ContextTool("Pipeline", [], scope="read", parallel=False)
    agent = _scheduler_agent(tool, pipeline=pipeline)
    events = [
        event
        async for event in execute_tool_calls(
            [ToolUseBlock(id="x1", name="Pipeline", input={})],
            agent,
            _scheduler_session(),
            AbortContext(),
            principal=principal,
        )
    ]
    end = next(event for event in events if event.type == "tool_call_end")
    assert end.is_error
    assert "authorization-sensitive" in end.result


@pytest.mark.asyncio
async def test_same_agent_sessions_keep_principals_and_role_decisions_isolated() -> None:
    import asyncio

    from linch import Agent, Principal, RunOptions
    from linch.config import FeatureFlags
    from linch.permissions import ToolRule
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry
    from linch.types import ToolResultBlock, Usage

    class _ConcurrentProvider:
        id = "concurrent-principal"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, request: Any) -> Any:
            has_tool_result = any(
                isinstance(block, ToolResultBlock)
                for message in request.messages
                for block in message.content
            )
            if has_tool_result:
                yield {"type": "message_start", "model": request.model}
                yield {"type": "text_delta", "text": "done"}
                yield {
                    "type": "message_end",
                    "stop_reason": "end_turn",
                    "usage": Usage(),
                }
                return
            yield {"type": "message_start", "model": request.model}
            yield {"type": "tool_use_start", "id": "call", "name": "PrincipalTool"}
            yield {"type": "tool_use_input_delta", "id": "call", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "call"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}

    seen: list[Any] = []
    tool = _ContextTool("PrincipalTool", seen, scope="write", parallel=False)
    registry = ToolRegistry()
    registry.register(tool)
    agent = Agent(
        model="test-model",
        provider=_ConcurrentProvider(),
        tools=registry,
        permissions={
            "mode": "skip-dangerous",
            "rules": [ToolRule("PrincipalTool", "deny", role="admin")],
        },
        session_store=InMemorySessionStore(),
        run_store=InMemoryRunStore(),
        features=FeatureFlags(),
        result_offload=None,
    )
    reader = Principal("tenant-a", "alice", {"reader"})
    admin = Principal("tenant-a", "bob", {"admin"})
    reader_session = await agent.session(id="reader")
    admin_session = await agent.session(id="admin")

    async def run(session: Any, principal: Any) -> list[Any]:
        return [event async for event in session.run("go", RunOptions(principal=principal))]

    reader_events, admin_events = await asyncio.gather(
        run(reader_session, reader),
        run(admin_session, admin),
    )
    assert reader_events[-1].subtype == "success"
    assert admin_events[-1].subtype == "success"
    assert seen == [reader]
    admin_tool_end = next(event for event in admin_events if event.type == "tool_call_end")
    assert admin_tool_end.is_error


@pytest.mark.asyncio
async def test_subagent_spawn_and_continue_forward_principal() -> None:
    from types import SimpleNamespace

    import linch.subagents.runner as runner_module
    from linch import Principal
    from linch.subagents.registry import AgentRegistry
    from linch.subagents.runner import RunSubagentResult
    from linch.tools import ToolContext
    from linch.tools.subagent import SubagentTool
    from linch.tools.subagent_continue import SubagentContinueTool

    principal = Principal("tenant-a", "alice", {"operator"})
    parent_agent = SimpleNamespace()
    session = SimpleNamespace(
        id="parent",
        agent=parent_agent,
        pending_child_events=[],
        workers={},
    )
    ctx = ToolContext(
        cwd=".",
        session_id=session.id,
        run_id="run",
        session_store=None,
        _principal=principal,
    )
    spawned: list[Any] = []
    continued: list[Any] = []

    async def fake_run(args: Any) -> Any:
        spawned.append(args)
        return RunSubagentResult("child", "spawned", False, False)

    async def fake_continue(args: Any) -> Any:
        continued.append(args)
        return RunSubagentResult("child", "continued", False, False)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runner_module, "run_subagent", fake_run)
    monkeypatch.setattr(runner_module, "continue_subagent", fake_continue)
    try:
        spawn = SubagentTool(
            AgentRegistry([]),
            lambda _session_id: session,
            lambda _session_id: "Agent #1",
            retain_subagents=True,
        )
        result = await spawn.execute({"description": "research", "prompt": "hello"}, ctx)
        assert not result.is_error
        assert spawned[0].run_options.principal is principal
        worker_id = next(iter(session.workers))

        continue_tool = SubagentContinueTool(lambda _session_id: session)
        result = await continue_tool.execute({"to": worker_id, "message": "follow up"}, ctx)
        assert not result.is_error
        assert continued[0].run_options.principal is principal
    finally:
        monkeypatch.undo()
