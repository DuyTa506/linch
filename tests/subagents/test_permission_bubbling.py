"""Subagent permission bubbling (ROADMAP Phase 4.2).

A child session's events — including a ``PermissionRequestEvent`` raised when one
of its tools needs approval — must surface to the *parent caller's* event stream
rather than sitting in a buffer only host UIs can poll. The loop drains
``session.pending_child_events`` at the top-of-turn chokepoint.

Verify: a subagent permission request surfaces to the parent caller.

linch imports happen inside the test because sibling tests pop ``linch*`` modules
from ``sys.modules``.
"""

from __future__ import annotations

from typing import Any


class WriteThing:
    name = "WriteThing"
    description = "A write-scoped tool that needs approval."
    scope = "write"
    parallel = False
    input_schema = {"type": "object", "properties": {}}

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, inp: dict[str, Any]) -> str:
        return "WriteThing()"

    async def execute(self, inp: dict[str, Any], ctx: Any) -> Any:
        from linch import ToolResult

        return ToolResult(content="wrote")


async def test_subagent_permission_request_surfaces_to_parent() -> None:
    from linch import Agent, PermissionRequestEvent, SubagentEvent, ToolRegistry
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.sessions import InMemorySessionStore
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    registry = ToolRegistry()
    registry.add(WriteThing())
    # Child consumes turns 0-1 (write call → permission ask → denied → text),
    # the parent consumes turn 2.
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="WriteThing", tool_input={}),
            TextTurn(text="child done"),
            TextTurn(text="parent done"),
        ]
    )
    agent = Agent(
        model="m",
        provider=provider,
        tools=registry,
        permissions={"mode": "default"},  # write tools must ask
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    parent = await agent.session()

    # Run the child as the SubagentTool would: emit into the parent's buffer.
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

    # The next parent turn drains the buffered child events into its own stream.
    events = [event async for event in parent.run("go")]
    bubbled = [
        event.event
        for event in events
        if isinstance(event, SubagentEvent) and isinstance(event.event, PermissionRequestEvent)
    ]
    assert bubbled, "child PermissionRequestEvent did not surface to the parent caller"
    assert bubbled[0].requests[0].tool_name == "WriteThing"
    assert parent.pending_child_events == []  # buffer drained
