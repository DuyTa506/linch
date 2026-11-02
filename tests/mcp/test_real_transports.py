"""End-to-end MCP transport lifecycle tests with real local servers."""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import pytest

pytest.importorskip("mcp", reason="the 'mcp' extra is not installed")

from linch import Agent, McpHttpServerConfig, McpStdioServerConfig  # noqa: E402
from linch.mcp.client import connect_mcp_servers  # noqa: E402
from linch.tools.base import ToolContext  # noqa: E402

_SERVER = r"""
import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def list_tools(_ctx, _params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="echo",
                description="Echo text",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    )


async def call_tool(_ctx, params):
    return types.CallToolResult(
        content=[types.TextContent(text="echo:" + str(params.arguments.get("text", "")))]
    )


async def main():
    server = Server("local-test", on_list_tools=list_tools, on_call_tool=call_tool)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


anyio.run(main, backend="asyncio")
"""

_HTTP_SERVER = r"""
import sys
from mcp import types
from mcp.server.lowlevel import Server
import uvicorn


async def list_tools(_ctx, _params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="echo",
                description="Echo text",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
    )


async def call_tool(_ctx, params):
    return types.CallToolResult(
        content=[types.TextContent(text="echo:" + str(params.arguments.get("text", "")))]
    )


server = Server("http-test", on_list_tools=list_tools, on_call_tool=call_tool)
app = server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1")
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="error")
"""


def _stdio_config() -> McpStdioServerConfig:
    return McpStdioServerConfig(
        command=sys.executable,
        args=["-c", _SERVER],
        env={"PATH": os.environ.get("PATH", "")},
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_local_port(port: int) -> None:
    async def probe() -> None:
        while True:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.02)
            else:
                writer.close()
                await writer.wait_closed()
                return

    await asyncio.wait_for(probe(), timeout=5)


async def _connect() -> object:
    return await connect_mcp_servers({"local": _stdio_config()})


@pytest.mark.timeout(10)
async def test_real_stdio_connect_call_and_cross_task_close() -> None:
    connection = await _connect()
    try:
        assert [tool.name for tool in connection.tools] == ["mcp__local__echo"]

        result = await connection.tools[0].execute(
            {"text": "hello"},
            ToolContext(cwd=".", session_id="s", run_id="r", session_store=None),
        )
        assert result.content == "echo:hello"

        close_task = asyncio.create_task(connection.close())
        await asyncio.wait_for(close_task, timeout=5)
        await connection.close()
    except BaseException:
        # The regression intentionally raises from close on the unfixed code.
        # Give the owner a chance to finish cleanup before the assertion escapes.
        try:
            await asyncio.wait_for(connection.close(), timeout=5)
        except BaseException:
            pass
        raise


@pytest.mark.timeout(10)
async def test_agent_mcp_servers_close_from_agent_task() -> None:
    agent = Agent(model="test-model", mcp_servers={"local": _stdio_config()})
    try:
        await agent.connect_mcp()
        tool = agent.tools.get("mcp__local__echo")
        assert tool is not None
        result = await tool.execute(
            {"text": "agent"},
            ToolContext(cwd=".", session_id="s", run_id="r", session_store=None),
        )
        assert result.content == "echo:agent"
        await asyncio.wait_for(agent.close(), timeout=5)
        await agent.close()
    except BaseException:
        try:
            await asyncio.wait_for(agent.close(), timeout=5)
        except BaseException:
            pass
        raise


@pytest.mark.timeout(15)
async def test_real_streamable_http_connect_call_and_cross_task_close() -> None:
    port = _free_local_port()
    server_process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _HTTP_SERVER,
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    connection = None
    try:
        await _wait_for_local_port(port)
        connection = await connect_mcp_servers(
            {
                "http": McpHttpServerConfig(
                    url=f"http://127.0.0.1:{port}/mcp",
                )
            }
        )
        result = await connection.tools[0].execute(
            {"text": "http"},
            ToolContext(cwd=".", session_id="s", run_id="r", session_store=None),
        )
        assert result.content == "echo:http"
        await asyncio.wait_for(asyncio.create_task(connection.close()), timeout=5)
        await connection.close()
    finally:
        if connection is not None:
            try:
                await asyncio.wait_for(connection.close(), timeout=5)
            except BaseException:
                pass
        server_process.terminate()
        try:
            await asyncio.wait_for(server_process.wait(), timeout=5)
        except TimeoutError:
            server_process.kill()
            await server_process.wait()
