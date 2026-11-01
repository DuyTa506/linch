from __future__ import annotations

from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool


class _FakeMcpTool(StubTool):
    def __init__(self, name: str, *, scope: str = "write", destructive: bool = False) -> None:
        super().__init__(name, scope=scope, parallel=False, output="called")
        self.destructive = destructive


def test_mcp_permission_rules_maps_only_destructive() -> None:
    from linch.mcp import mcp_permission_rules

    tools = [
        _FakeMcpTool("srv__danger", destructive=True),
        _FakeMcpTool("srv__safe", destructive=False),
        _FakeMcpTool("srv__read", scope="read", destructive=False),
    ]
    rules = mcp_permission_rules(tools)
    assert [(r.tool, r.decision) for r in rules] == [("srv__danger", "ask")]


def test_destructive_rule_forces_ask_under_permissive_mode() -> None:
    from linch.mcp import mcp_permission_rules
    from linch.permissions.engine import PendingToolCall, PermissionEngine

    tool = _FakeMcpTool("srv__danger", destructive=True)
    engine = PermissionEngine(mode="skip-dangerous", rules=mcp_permission_rules([tool]))
    decision = engine.evaluate(
        PendingToolCall(tool_use_id="t", tool=tool, input={}),
    )
    assert decision.decision == "ask"


async def test_tool_attached_mid_run_appears_next_turn() -> None:
    from linch import Agent
    from linch.mcp import McpConnection
    from linch.sessions import InMemorySessionStore

    provider = ScriptedProvider([TextTurn("ok"), TextTurn("ok")])
    agent = Agent(
        model="m",
        provider=provider,  # type: ignore[arg-type]
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    session = await agent.session()

    _ = [event async for event in session.run("first")]
    turn1_tools = {t["name"] for t in provider.requests[0].tools}
    assert "srv__danger" not in turn1_tools

    tool = _FakeMcpTool("srv__danger", destructive=True)
    agent._attach_mcp_tools(McpConnection(tools=[tool], sessions=[]))

    _ = [event async for event in session.run("second")]
    turn2_tools = {t["name"] for t in provider.requests[1].tools}
    assert "srv__danger" in turn2_tools
    assert any(
        getattr(r, "tool", None) == "srv__danger" and r.decision == "ask"
        for r in agent.permission_engine.rules
    )
