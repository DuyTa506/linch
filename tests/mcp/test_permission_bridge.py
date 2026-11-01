from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.tools import StubTool


class _FakeMcpTool(StubTool):
    def __init__(self, name: str, *, scope: str = "write", destructive: bool = False) -> None:
        super().__init__(name, scope=scope, parallel=False, output="called")
        self.destructive = destructive


class _FakeConnection:
    def __init__(self, tools: list[Any], close_error: BaseException | None = None) -> None:
        self.tools = tools
        self.closed = 0
        self.close_error = close_error

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class _BlockingCloseConnection(_FakeConnection):
    def __init__(self, tools: list[Any]) -> None:
        super().__init__(tools)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.closed += 1
        self.close_started.set()
        await self.release_close.wait()


class _DisposableRegistry:
    def __init__(self, *tools: _FakeMcpTool) -> None:
        self.tools = {tool.name: tool for tool in tools}
        self.block_name: str | None = None
        self.dispose_started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    def get(self, name: str) -> _FakeMcpTool | None:
        return self.tools.get(name)

    def register(self, tool: _FakeMcpTool) -> object:
        from linch.errors import ConfigError
        from linch.kernel import Disposable

        if tool.name in self.tools:
            raise ConfigError(f"tool {tool.name!r} already registered")
        self.tools[tool.name] = tool

        async def undo() -> None:
            if tool.name == self.block_name:
                assert self.dispose_started is not None
                assert self.release is not None
                self.dispose_started.set()
                await self.release.wait()
            self.tools.pop(tool.name, None)

        return Disposable(undo)


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
    await agent._attach_mcp_tools(McpConnection(tools=[tool], sessions=[]))

    _ = [event async for event in session.run("second")]
    turn2_tools = {t["name"] for t in provider.requests[1].tools}
    assert "srv__danger" in turn2_tools
    assert any(
        getattr(r, "tool", None) == "srv__danger" and r.decision == "ask"
        for r in agent.permission_engine.rules
    )


async def test_mcp_attach_rolls_back_tools_and_rules_on_collision() -> None:
    from linch import Agent
    from linch.errors import ConfigError
    from linch.mcp import McpConnection
    from linch.sessions import InMemorySessionStore
    from tests.support.tools import tool_registry

    existing = _FakeMcpTool("srv__collision")
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
        tools=tool_registry(existing),
    )
    destructive = _FakeMcpTool("srv__danger", destructive=True)

    with pytest.raises(ConfigError, match="already registered"):
        await agent._attach_mcp_tools(
            McpConnection(tools=[destructive, _FakeMcpTool("srv__collision")], sessions=[])
        )

    assert agent.tools.get("srv__danger") is None
    assert agent.tools.get("srv__collision") is existing
    assert not any(getattr(r, "tool", None) == "srv__danger" for r in agent.permission_engine.rules)


async def test_mcp_attach_rolls_back_rules_when_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from linch import Agent
    from linch.mcp import McpConnection
    from linch.sessions import InMemorySessionStore

    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    tool = _FakeMcpTool("srv__danger", destructive=True)
    replacement = _FakeMcpTool("srv__danger")

    def fail_refresh() -> None:
        agent.tools.replace(replacement)  # type: ignore[arg-type]
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(agent, "_refresh_system_blocks", fail_refresh)

    with pytest.raises(RuntimeError, match="refresh failed"):
        await agent._attach_mcp_tools(McpConnection(tools=[tool], sessions=[]))

    assert agent.tools.get("srv__danger") is replacement
    assert not any(getattr(r, "tool", None) == "srv__danger" for r in agent.permission_engine.rules)


async def test_mcp_attach_cancellation_drains_all_rollback_disposers() -> None:
    from linch import Agent
    from linch.errors import ConfigError
    from linch.mcp import McpConnection
    from linch.sessions import InMemorySessionStore

    collision = _FakeMcpTool("srv__collision")
    registry = _DisposableRegistry(collision)
    registry.block_name = "srv__second"
    registry.dispose_started = asyncio.Event()
    registry.release = asyncio.Event()
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    agent.tools = registry  # type: ignore[assignment]
    connection = McpConnection(
        tools=[
            _FakeMcpTool("srv__first", destructive=True),
            _FakeMcpTool("srv__second"),
            collision,
        ],
        sessions=[],
    )

    attach = asyncio.create_task(agent._attach_mcp_tools(connection))
    await registry.dispose_started.wait()
    attach.cancel()
    registry.release.set()

    with pytest.raises((ConfigError, asyncio.CancelledError)):
        await attach
    assert registry.get("srv__first") is None
    assert registry.get("srv__second") is None
    assert not any(getattr(r, "tool", None) == "srv__first" for r in agent.permission_engine.rules)


async def test_mcp_attach_rollback_preserves_concurrent_rules() -> None:
    from linch import Agent
    from linch.errors import ConfigError
    from linch.mcp import McpConnection
    from linch.sessions import InMemorySessionStore

    collision = _FakeMcpTool("srv__collision")
    registry = _DisposableRegistry(collision)
    registry.block_name = "srv__bad"
    registry.dispose_started = asyncio.Event()
    registry.release = asyncio.Event()
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    agent.tools = registry  # type: ignore[assignment]

    failing = asyncio.create_task(
        agent._attach_mcp_tools(
            McpConnection(
                tools=[_FakeMcpTool("srv__bad", destructive=True), collision],
                sessions=[],
            )
        )
    )
    await registry.dispose_started.wait()

    good = _FakeMcpTool("srv__good", destructive=True)
    await agent._attach_mcp_tools(McpConnection(tools=[good], sessions=[]))
    assert registry.get("srv__good") is good
    assert any(getattr(r, "tool", None) == "srv__good" for r in agent.permission_engine.rules)

    registry.release.set()
    with pytest.raises(ConfigError, match="already registered"):
        await failing
    assert registry.get("srv__bad") is None
    assert registry.get("srv__good") is good
    assert any(getattr(r, "tool", None) == "srv__good" for r in agent.permission_engine.rules)


async def test_mcp_attach_rollback_cleans_subagent_registry() -> None:
    from linch import Agent
    from linch.errors import ConfigError
    from linch.mcp import McpConnection
    from linch.sessions import InMemorySessionStore

    collision = _FakeMcpTool("srv__collision")
    main_registry = _DisposableRegistry(collision)
    subagent_registry = _DisposableRegistry(collision)
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    agent.tools = main_registry  # type: ignore[assignment]
    agent._subagent_tool_registry = subagent_registry  # type: ignore[assignment]

    with pytest.raises(ConfigError, match="already registered"):
        await agent._attach_mcp_tools(
            McpConnection(
                tools=[_FakeMcpTool("srv__new", destructive=True), collision],
                sessions=[],
            )
        )

    assert main_registry.get("srv__new") is None
    assert subagent_registry.get("srv__new") is None
    assert main_registry.get("srv__collision") is collision
    assert subagent_registry.get("srv__collision") is collision


async def test_configured_mcp_connection_closes_when_attachment_fails(monkeypatch) -> None:
    import linch.mcp as mcp_module
    from linch import Agent
    from linch.errors import ConfigError
    from linch.sessions import InMemorySessionStore
    from tests.support.tools import tool_registry

    existing = _FakeMcpTool("srv__collision")
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
        tools=tool_registry(existing),
    )
    agent._mcp_servers = {"srv": object()}
    connection = _FakeConnection([_FakeMcpTool("srv__collision")])

    async def connect(_servers: Any) -> _FakeConnection:
        return connection

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", connect)

    try:
        with pytest.raises(ConfigError, match="already registered"):
            await agent.connect_mcp()
        assert connection.closed == 1
        assert agent._mcp_connection is None
        assert agent._mcp_connect is None
    finally:
        await agent.close()


async def test_added_mcp_connection_closes_when_attachment_fails(monkeypatch) -> None:
    import linch.mcp as mcp_module
    from linch import Agent
    from linch.errors import ConfigError
    from linch.sessions import InMemorySessionStore
    from tests.support.tools import tool_registry

    existing = _FakeMcpTool("srv__collision")
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
        tools=tool_registry(existing),
    )
    connection = _FakeConnection([_FakeMcpTool("srv__collision")])

    async def connect(_servers: Any) -> _FakeConnection:
        return connection

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", connect)

    try:
        with pytest.raises(ConfigError, match="already registered"):
            await agent.add_mcp_servers({"srv": object()})
        assert connection.closed == 1
        assert agent._extra_mcp_connections == []
    finally:
        await agent.close()


async def test_mcp_cleanup_error_preserves_attachment_error(monkeypatch) -> None:
    import linch.mcp as mcp_module
    from linch import Agent
    from linch.errors import ConfigError
    from linch.sessions import InMemorySessionStore
    from tests.support.tools import tool_registry

    existing = _FakeMcpTool("srv__collision")
    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
        tools=tool_registry(existing),
    )
    agent._mcp_servers = {"srv": object()}
    connection = _FakeConnection(
        [_FakeMcpTool("srv__collision")], close_error=RuntimeError("close failed")
    )

    async def connect(_servers: Any) -> _FakeConnection:
        return connection

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", connect)

    try:
        with pytest.raises(ConfigError, match="already registered") as caught:
            await agent.connect_mcp()
        assert connection.closed == 1
        assert any("MCP connection cleanup failed" in note for note in caught.value.__notes__)
    finally:
        await agent.close()


@pytest.mark.parametrize("entrypoint", ["configured", "added"])
async def test_mcp_connection_close_drains_repeated_cancellation(
    monkeypatch, entrypoint: str
) -> None:
    import linch.mcp as mcp_module
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    agent = Agent(
        model="m",
        provider=ScriptedProvider([TextTurn("ok")]),  # type: ignore[arg-type]
        permissions={"mode": "acceptEdits"},
        session_store=InMemorySessionStore(),
        cwd=".",
    )
    if entrypoint == "configured":
        agent._mcp_servers = {"srv": object()}
    connection = _BlockingCloseConnection([])
    attached = asyncio.Event()

    async def connect(_servers: Any) -> _FakeConnection:
        return connection

    async def block_attach(_connection: Any) -> None:
        attached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp_module, "connect_mcp_servers", connect)
    monkeypatch.setattr(agent, "_attach_mcp_tools", block_attach)

    async def invoke() -> None:
        if entrypoint == "configured":
            await agent.connect_mcp()
        else:
            await agent.add_mcp_servers({"srv": object()})

    task = asyncio.create_task(invoke())
    await attached.wait()
    task.cancel()
    try:
        await connection.close_started.wait()
        task.cancel()
        connection.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.closed == 1
        if entrypoint == "configured":
            assert agent._mcp_connect is None
        else:
            assert agent._extra_mcp_connections == []
    finally:
        connection.release_close.set()
        await agent.close()
