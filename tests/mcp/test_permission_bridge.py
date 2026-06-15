"""MCP mid-run registration + annotation→permission bridge (ROADMAP Phase 4.3).

A server connected mid-run must have its tools appear on the next turn (the
per-turn request rebuilds its tool list from the live registry, so attaching
tools mid-run is picked up). A ``destructiveHint`` annotation maps to an ``ask``
permission rule so the tool prompts even under permissive modes.

Verify: a tool registered mid-run is offered on the next turn; a destructive MCP
tool triggers a permission prompt.

The optional ``mcp`` package is not installed here; the annotation→rule logic and
the registration path do not need it, and the one test that exercises
``make_mcp_tool`` injects a minimal fake ``mcp.types`` module.
"""

from __future__ import annotations

import sys
import types as _pytypes
from collections.abc import AsyncIterator
from typing import Any


class _RecordingProvider:
    id = "fake"

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 1_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.requests.append(req)
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "ok"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


class _FakeMcpTool:
    """A tool object shaped like ``make_mcp_tool``'s output, carrying ``destructive``."""

    parallel = False
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str, *, scope: str = "write", destructive: bool = False) -> None:
        self.name = name
        self.scope = scope
        self.destructive = destructive
        self.description = "fake mcp tool"

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, inp: dict[str, Any]) -> str:
        return self.name

    async def execute(self, inp: dict[str, Any], ctx: Any) -> Any:
        from linch import ToolResult

        return ToolResult(content="called")


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
    # Without the rule, a write tool under skip-dangerous would auto-allow.
    assert decision.decision == "ask"


def test_make_mcp_tool_derives_destructive_flag() -> None:
    injected = "mcp.types" not in sys.modules
    orig_mcp = sys.modules.get("mcp")
    orig_mcp_types = sys.modules.get("mcp.types")
    if injected:
        mcp = _pytypes.ModuleType("mcp")
        mcp.__path__ = []  # type: ignore[attr-defined]
        types_mod = _pytypes.ModuleType("mcp.types")

        class CallToolResult: ...

        class Tool: ...

        class TextContent: ...

        types_mod.CallToolResult = CallToolResult  # type: ignore[attr-defined]
        types_mod.Tool = Tool  # type: ignore[attr-defined]
        types_mod.TextContent = TextContent  # type: ignore[attr-defined]
        sys.modules["mcp"] = mcp
        sys.modules["mcp.types"] = types_mod

    try:
        from linch.mcp.tool import make_mcp_tool

        annotations = _pytypes.SimpleNamespace(readOnlyHint=False, destructiveHint=True)
        mcp_tool = _pytypes.SimpleNamespace(
            name="rm",
            description="remove",
            inputSchema=_pytypes.SimpleNamespace(properties={}, required=None),
            annotations=annotations,
        )

        async def _call(*_a: Any, **_k: Any) -> Any:  # pragma: no cover - not invoked
            return None

        tool = make_mcp_tool("srv", mcp_tool, _call)
        assert tool.destructive is True
        assert tool.scope == "write"
    finally:
        if injected:
            # Restore sys.modules so the injected fakes don't leak into other tests.
            for key, orig in (("mcp", orig_mcp), ("mcp.types", orig_mcp_types)):
                if orig is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = orig


async def test_tool_attached_mid_run_appears_next_turn() -> None:
    from linch import Agent
    from linch.mcp import McpConnection
    from linch.sessions import InMemorySessionStore

    provider = _RecordingProvider()
    agent = Agent(
        model="m",
        provider=provider,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    session = await agent.session()

    # Turn 1: the mid-run tool is not yet present.
    _ = [event async for event in session.run("first")]
    turn1_tools = {t["name"] for t in provider.requests[0].tools}
    assert "srv__danger" not in turn1_tools

    # Attach a connection mid-run (as add_mcp_servers does after connecting).
    tool = _FakeMcpTool("srv__danger", destructive=True)
    agent._attach_mcp_tools(McpConnection(tools=[tool], sessions=[]))

    # Turn 2: the tool is offered, and its destructive annotation forced an ask rule.
    _ = [event async for event in session.run("second")]
    turn2_tools = {t["name"] for t in provider.requests[1].tools}
    assert "srv__danger" in turn2_tools
    assert any(
        getattr(r, "tool", None) == "srv__danger" and r.decision == "ask"
        for r in agent.permission_engine.rules
    )
