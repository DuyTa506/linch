from __future__ import annotations

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


async def test_subagent_permission_request_surfaces_to_parent() -> None:
    from linch import PermissionRequestEvent, SubagentEvent
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    registry = tool_registry(StubTool("WriteThing", scope="write"))
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="WriteThing", tool_input={}),
            TextTurn(text="child done"),
            TextTurn(text="parent done"),
        ]
    )
    agent = make_agent(
        provider,
        model="m",
        tools=registry,
        permissions={"mode": "default"},
    )
    parent = await agent.session()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="please write",
            display_name="helper",
            subagent_run_id="sa_perm",
            emit=parent.pending_child_events.append,
        )
    )
    assert not result.errored

    events = [event async for event in parent.run("go")]
    bubbled = [
        event.event
        for event in events
        if isinstance(event, SubagentEvent) and isinstance(event.event, PermissionRequestEvent)
    ]
    assert bubbled, "child PermissionRequestEvent did not surface to the parent caller"
    assert bubbled[0].requests[0].tool_name == "WriteThing"
    assert parent.pending_child_events == []
