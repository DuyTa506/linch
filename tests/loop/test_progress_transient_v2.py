"""Tool progress is streamed live but never written to the RunStore."""

from __future__ import annotations

from typing import Any


async def test_tool_progress_is_not_persisted() -> None:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolContext, ToolRegistry, ToolResult

    class ProgressTool:
        name = "Progress"
        description = "reports progress"
        input_schema: dict[str, Any] = {"type": "object"}
        scope = "read"
        parallel = False

        def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
            return raw

        def summarize(self, input: dict[str, Any]) -> str:
            return "progress"

        async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
            ctx.report_progress("secret transient detail", {"token": "do-not-persist"})
            return ToolResult(content="done")

    tools = ToolRegistry()
    tools.add(ProgressTool())
    run_store = InMemoryRunStore()
    agent = Agent(
        model="test-model",
        provider=ScriptedProvider([ToolUseTurn("Progress", {}, "call-1"), TextTurn("finished")]),
        tools=tools,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        run_store=run_store,
        features=FeatureFlags(),
        result_offload=None,
    )
    events = [event async for event in (await agent.session()).run("go")]
    run_id = next(event.run_id for event in events if event.type == "system")

    assert any(event.type == "tool_progress" for event in events)
    durable = await run_store.load_events(run_id)
    assert all(row.event.type != "tool_progress" for row in durable)
