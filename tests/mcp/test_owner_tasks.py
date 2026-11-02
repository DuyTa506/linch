"""MCP contexts stay owned by one task even when callers change tasks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp", reason="the 'mcp' extra is not installed")


async def test_transport_and_session_contexts_share_a_dedicated_owner(monkeypatch):
    from linch import McpStdioServerConfig
    from linch.mcp import client

    entries, exits = [], []

    @asynccontextmanager
    async def transport(*args, **kwargs):
        entries.append(("transport", asyncio.current_task()))
        try:
            yield object(), object()
        finally:
            exits.append(("transport", asyncio.current_task()))

    class Session:
        async def __aenter__(self):
            entries.append(("session", asyncio.current_task()))
            return self

        async def __aexit__(self, *exc):
            exits.append(("session", asyncio.current_task()))

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(client, "stdio_client", transport)
    monkeypatch.setattr(client, "ClientSession", lambda *args: Session())
    connection = await client.connect_mcp_servers({"local": McpStdioServerConfig(command="unused")})
    await asyncio.create_task(connection.close())
    assert exits == list(reversed(entries))
    assert entries[0][1] is entries[1][1]
    assert entries[0][1] is not asyncio.current_task()


async def test_closing_an_owner_before_its_task_starts_is_harmless():
    from linch import McpStdioServerConfig
    from linch.mcp.client import _SessionOwner

    owner = _SessionOwner("local", McpStdioServerConfig(command="unused"))
    await owner.aclose()
    await owner.aclose()
