"""Tool progress is streamed live but never written to the RunStore."""

from __future__ import annotations

from typing import Any

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.stores import memory_run_store, memory_session_store
from tests.support.tools import tool_registry


async def test_tool_progress_is_not_persisted() -> None:
    from linch.tools import ToolContext, ToolResult

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

    tools = tool_registry(ProgressTool())
    run_store = memory_run_store()
    agent = make_agent(
        ScriptedProvider([ToolUseTurn("Progress", {}, "call-1"), TextTurn("finished")]),
        model="test-model",
        tools=tools,
        session_store=memory_session_store(),
        run_store=run_store,
        result_offload=None,
    )
    events = [event async for event in (await agent.session()).run("go")]
    run_id = next(event.run_id for event in events if event.type == "system")

    assert any(event.type == "tool_progress" for event in events)
    durable = await run_store.load_events(run_id)
    assert all(row.event.type != "tool_progress" for row in durable)
