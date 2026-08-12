from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from linch.abort import AbortContext
from linch.events import (
    BackgroundWorkerEvent,
    HookEventRecord,
    PermissionRequestEvent,
    ToolCallEndEvent,
    ToolProgressEvent,
    event_from_dict,
    event_to_dict,
)
from linch.hooks import HookResult
from linch.permissions import BashRule, PathRule, PendingToolCall, PermissionEngine
from linch.permissions.keys import permission_decision_key
from linch.scheduler import execute_tool_calls
from linch.tools import ToolContext, ToolRegistry, ToolResult, ToolScope
from linch.tools.builtin import BashTool
from linch.types import ToolUseBlock


class _RecordingTool:
    description = "records canonical input"
    input_schema = {"type": "object", "properties": {"file_path": {"type": "string"}}}
    scope: ToolScope = "write"
    parallel = False

    def __init__(self, name: str = "Write") -> None:
        self.name = name
        self.inputs: list[dict[str, Any]] = []

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw.get("file_path"), str):
            raise ValueError("file_path is required")
        return dict(raw)

    def summarize(self, input: dict[str, Any]) -> str:
        return f"{self.name}({input['file_path']})"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.inputs.append(dict(input))
        return ToolResult(content="ran")


def _agent(
    tool: Any,
    permission_engine: PermissionEngine,
    *,
    hooks: list[Any] | None = None,
    run_store: Any = None,
    enable_background_tools: bool = False,
) -> SimpleNamespace:
    registry = ToolRegistry()
    registry.add(tool)
    return SimpleNamespace(
        cwd="/project",
        tools=registry,
        permission_engine=permission_engine,
        max_tool_concurrency=4,
        tool_concurrency=4,
        hooks=hooks or [],
        run_store=run_store,
        enable_background_tools=enable_background_tools,
    )


def _session(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": "s1",
        "store": None,
        "active_run_id": "run-origin",
        "tools_override": None,
        "current_turn_allowed_tools": None,
        "current_turn_permission_decisions": {},
        "background_tasks": [],
        "pending_notifications": [],
        "pending_child_events": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _events(agent: Any, session: Any, block: ToolUseBlock) -> list[Any]:
    return [event async for event in execute_tool_calls([block], agent, session, AbortContext())]


async def test_pre_tool_mutation_is_revalidated_then_denied_on_final_path() -> None:
    tool = _RecordingTool()

    class Rewrite:
        def on_pre_tool_use(self, ctx: Any) -> HookResult:
            return HookResult.mutate(input={"file_path": "secrets/key.txt"})

    agent = _agent(
        tool,
        PermissionEngine(
            mode="skip-dangerous",
            rules=[PathRule(paths=["secrets/**"], decision="deny")],
            project_root="/project",
        ),
        hooks=[Rewrite()],
    )
    events = await _events(
        agent,
        _session(),
        ToolUseBlock(id="call", name="Write", input={"file_path": "safe.txt"}),
    )

    assert tool.inputs == []
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert end.is_error is True
    assert "denied" in end.result
    # One mutating hook produces one telemetry record, not one per lifecycle pass.
    assert len([event for event in events if isinstance(event, HookEventRecord)]) == 1


async def test_unoffered_global_tool_is_unavailable_even_in_skip_dangerous_mode() -> None:
    tool = _RecordingTool("Bash")
    hook_calls = 0

    class Rewrite:
        def on_pre_tool_use(self, ctx: Any) -> HookResult:
            nonlocal hook_calls
            hook_calls += 1
            return HookResult.mutate(input={"file_path": "rewritten"})

    events = await _events(
        _agent(
            tool,
            PermissionEngine(mode="skip-dangerous"),
            hooks=[Rewrite()],
        ),
        _session(current_turn_allowed_tools=["Read"]),
        ToolUseBlock(id="call", name="Bash", input={"file_path": "raw"}),
    )

    assert hook_calls == 0  # availability is enforced before hook/policy side effects
    assert tool.inputs == []
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert end.is_error is True
    assert "not offered for the current turn" in end.result
    assert not any(isinstance(event, PermissionRequestEvent) for event in events)


async def test_checkpoint_restored_tool_allowlist_beats_stored_permission_replay() -> None:
    tool = _RecordingTool("Bash")
    input = {"file_path": "raw"}
    session = _session(
        # This is the scheduler state restored from
        # RunCheckpoint.current_turn_allowed_tools on a mid-turn resume.
        current_turn_allowed_tools=["Read"],
        current_turn_permission_decisions={
            permission_decision_key("Bash", input): {
                "decision": "allow",
                "reason": None,
                "updated_input": None,
            }
        },
    )

    events = await _events(
        _agent(tool, PermissionEngine(mode="default")),
        session,
        ToolUseBlock(id="call", name="Bash", input=input),
    )

    assert tool.inputs == []
    end = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert end.is_error is True
    assert "not offered" in end.result


async def test_permission_callback_updated_input_is_rejected() -> None:
    tool = _RecordingTool()
    engine = PermissionEngine(
        mode="default",
        can_use_tool=lambda _req: {
            "behavior": "allow",
            "updatedInput": {"file_path": "secrets/key.txt"},
        },
    )
    decision = await engine.resolve(
        PendingToolCall(
            tool_use_id="call",
            tool=tool,
            input={"file_path": "safe.txt"},
        ),
        AbortContext(),
    )

    assert decision.decision == "deny"
    assert "updatedInput is unsupported" in (decision.reason or "")


async def test_legacy_updated_input_decision_is_not_replayed() -> None:
    tool = _RecordingTool()
    callback_calls = 0

    def allow(_req: Any) -> dict[str, str]:
        nonlocal callback_calls
        callback_calls += 1
        return {"behavior": "allow"}

    final_input = {"file_path": "final.txt"}
    session = _session(
        current_turn_permission_decisions={
            permission_decision_key("Write", final_input): {
                "decision": "allow",
                "reason": None,
                "updated_input": {"file_path": "other.txt"},
            }
        }
    )
    events = await _events(
        _agent(tool, PermissionEngine(mode="default", can_use_tool=allow)),
        session,
        ToolUseBlock(id="call", name="Write", input=final_input),
    )

    assert callback_calls == 1
    assert any(isinstance(event, PermissionRequestEvent) for event in events)
    assert tool.inputs == [final_input]


def test_matching_bash_ask_survives_all_permission_modes() -> None:
    for mode in ("default", "acceptEdits", "skip-dangerous"):
        engine = PermissionEngine(
            mode=mode,
            rules=[BashRule(pattern="deploy *", decision="ask")],
        )
        decision = engine.evaluate(
            PendingToolCall(
                tool_use_id="call",
                tool=BashTool(),
                input={"command": "deploy production"},
            )
        )
        assert decision.decision == "ask"


def test_bash_passthrough_continues_and_quoted_separator_stays_one_segment() -> None:
    engine = PermissionEngine(
        mode="skip-dangerous",
        rules=[
            BashRule(pattern="echo *", decision="passthrough"),
            BashRule(pattern="echo *", decision="deny"),
        ],
    )
    decision = engine.evaluate(
        PendingToolCall(
            tool_use_id="call",
            tool=BashTool(),
            input={"command": "echo 'safe; still one command'"},
        )
    )
    assert decision.decision == "deny"


async def test_progress_is_live_bracketed_and_round_trips() -> None:
    release = asyncio.Event()
    retained: list[ToolContext] = []

    class ProgressTool(_RecordingTool):
        name = "Progress"
        scope: ToolScope = "read"
        parallel = False

        def __init__(self) -> None:
            super().__init__("Progress")

        async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
            retained.append(ctx)
            ctx.report_progress("halfway", {"percent": 50})
            await release.wait()
            return ToolResult(content="done")

    iterator = execute_tool_calls(
        [ToolUseBlock(id="call", name="Progress", input={"file_path": "x"})],
        _agent(ProgressTool(), PermissionEngine(mode="skip-dangerous")),
        _session(),
        AbortContext(),
    ).__aiter__()
    start = await iterator.__anext__()
    progress = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
    assert start.type == "tool_call_start"
    assert isinstance(progress, ToolProgressEvent)
    assert progress.message == "halfway"
    assert progress.data == {"percent": 50}
    assert event_from_dict(event_to_dict(progress)) == progress

    release.set()
    rest = [event async for event in iterator]
    assert isinstance(rest[-1], ToolCallEndEvent)
    retained[0].report_progress("late")  # ignored after execute() settled


async def test_parallel_later_tool_progress_is_not_hidden_by_slow_first() -> None:
    release = asyncio.Event()

    class ParallelTool(_RecordingTool):
        scope: ToolScope = "read"
        parallel = True

        def __init__(self, name: str, report: bool) -> None:
            super().__init__(name)
            self.report = report

        async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
            if self.report:
                ctx.report_progress("later-live")
            await release.wait()
            return ToolResult(content=self.name)

    first = ParallelTool("First", False)
    second = ParallelTool("Second", True)
    registry = ToolRegistry()
    registry.add(first)
    registry.add(second)
    agent = _agent(first, PermissionEngine(mode="skip-dangerous"))
    agent.tools = registry
    iterator = execute_tool_calls(
        [
            ToolUseBlock(id="first", name="First", input={"file_path": "a"}),
            ToolUseBlock(id="second", name="Second", input={"file_path": "b"}),
        ],
        agent,
        _session(),
        AbortContext(),
    ).__aiter__()

    assert (await iterator.__anext__()).type == "tool_call_start"
    assert (await iterator.__anext__()).type == "tool_call_start"
    progress = await asyncio.wait_for(iterator.__anext__(), timeout=1.0)
    assert isinstance(progress, ToolProgressEvent)
    assert progress.tool_use_id == "second"
    release.set()
    rest = [event async for event in iterator]
    assert [event.tool_use_id for event in rest if isinstance(event, ToolCallEndEvent)] == [
        "first",
        "second",
    ]


async def test_background_completion_audit_is_attributed_to_origin_run() -> None:
    release = asyncio.Event()

    class Store:
        def __init__(self) -> None:
            self.events: list[tuple[str, Any]] = []

        async def append_event(self, run_id: str, event: Any) -> int:
            self.events.append((run_id, event))
            return len(self.events)

    class BackgroundTool(_RecordingTool):
        name = "Background"
        scope: ToolScope = "read"

        def __init__(self) -> None:
            super().__init__("Background")

        async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
            await release.wait()
            return ToolResult(content="finished")

    store = Store()
    session = _session()
    await _events(
        _agent(
            BackgroundTool(),
            PermissionEngine(mode="skip-dangerous"),
            run_store=store,
            enable_background_tools=True,
        ),
        session,
        ToolUseBlock(
            id="call",
            name="Background",
            input={"file_path": "x", "run_in_background": True},
        ),
    )
    session.active_run_id = None  # foreground run has settled
    release.set()
    await asyncio.gather(*session.background_tasks)

    assert len(store.events) == 1
    run_id, event = store.events[0]
    assert run_id == "run-origin"
    assert isinstance(event, BackgroundWorkerEvent)
    assert event.status == "completed"
