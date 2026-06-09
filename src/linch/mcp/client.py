from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ..errors import ConfigError
from .config import McpServerConfig, mcp_server_type
from .naming import normalize_name_for_mcp
from .tool import make_mcp_tool

VERSION = "0.1.0"


@dataclass(slots=True)
class _OpenSession:
    transport: Any
    session: Any


class McpConnection:
    def __init__(
        self,
        tools: list[object],
        sessions: list[_OpenSession],
    ) -> None:
        self.tools = tools
        self._sessions = sessions
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for s in reversed(self._sessions):
            try:
                await s.session.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await s.transport.__aexit__(None, None, None)
            except Exception:
                pass


async def connect_mcp_servers(
    servers: dict[str, McpServerConfig],
) -> McpConnection:
    entries = list(servers.items())
    normalized: set[str] = set()
    for name, _ in entries:
        if name.strip() == "":
            raise ConfigError("MCP server name must be non-empty")
        key = normalize_name_for_mcp(name)
        if key in normalized:
            raise ConfigError(f"MCP server names collide after normalization: '{name}' -> '{key}'")
        normalized.add(key)

    opened: list[_OpenSession] = []
    tools: list[object] = []
    failures: list[str] = []

    for name, config in entries:
        try:
            transport, session = await _connect_one(name, config)
            # Track the open transport/session BEFORE list_tools() so the
            # except-cleanup loop below closes them even if list_tools() or
            # tool-building raises (otherwise the subprocess/session leak).
            opened.append(_OpenSession(transport, session))
            result = await session.list_tools()
            mcp_tools = [make_mcp_tool(name, t, _make_call_tool(session)) for t in result.tools]
            tools.extend(mcp_tools)
        except Exception as exc:
            for s in reversed(opened):
                try:
                    await s.session.__aexit__(None, None, None)
                except Exception:
                    pass
                try:
                    await s.transport.__aexit__(None, None, None)
                except Exception:
                    pass
            failures.append(f"{name}: {exc}")
            raise ConfigError(f"Failed to connect MCP server(s): {'; '.join(failures)}") from exc

    return McpConnection(tools=tools, sessions=opened)


async def _connect_one(name: str, config: McpServerConfig) -> tuple[Any, ClientSession]:
    if mcp_server_type(config) == "http":
        return await _connect_http(name, config)
    return await _connect_stdio(name, config)


async def _connect_stdio(name: str, config: object) -> tuple[Any, ClientSession]:
    transport = stdio_client(
        StdioServerParameters(
            command=getattr(config, "command", ""),
            args=getattr(config, "args", None) or [],
            env=getattr(config, "env", None),
        )
    )
    read, write = await transport.__aenter__()
    session_ctx: Any = None
    try:
        session_ctx = ClientSession(read, write)
        session = await session_ctx.__aenter__()
        await session.initialize()
        return (transport, session)
    except Exception:
        # Close the session first (if it was entered), then the transport,
        # matching the close()/cleanup ordering. Swallow secondary cleanup
        # errors so the original exception propagates.
        if session_ctx is not None:
            try:
                await session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        try:
            await transport.__aexit__(None, None, None)
        except Exception:
            pass
        raise


async def _connect_http(name: str, config: object) -> tuple[Any, ClientSession]:
    url = str(getattr(config, "url", ""))
    headers = dict(getattr(config, "headers", None) or {})
    transport = streamablehttp_client(url, headers=headers)
    read, write, _ = await transport.__aenter__()
    session_ctx: Any = None
    try:
        session_ctx = ClientSession(read, write)
        session = await session_ctx.__aenter__()
        await session.initialize()
        return (transport, session)
    except Exception:
        # Close the session first (if it was entered), then the transport,
        # matching the close()/cleanup ordering. Swallow secondary cleanup
        # errors so the original exception propagates.
        if session_ctx is not None:
            try:
                await session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        try:
            await transport.__aexit__(None, None, None)
        except Exception:
            pass
        raise


def _make_call_tool(session: ClientSession):
    async def _call(name: str, args: dict, signal: object) -> object:
        from ..abort import throw_if_aborted

        throw_if_aborted(cast(Any, signal))
        return await session.call_tool(name, args or {})

    return _call
