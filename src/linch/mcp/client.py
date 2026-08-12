from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

# mcp 2.x publishes no public path to this helper — its own client/sse.py and
# client/session_group.py import it from here too. Building the httpx client by
# hand instead would mean duplicating MCP's SSE-friendly timeout defaults.
from mcp.shared._httpx_utils import create_mcp_http_client

from ..errors import ConfigError
from .config import McpServerConfig, mcp_server_type
from .naming import normalize_name_for_mcp
from .tool import make_mcp_tool

VERSION = "0.1.0"


@dataclass(slots=True)
class _OpenSession:
    """The resources one connected server holds, in the order they were entered."""

    transport: Any = None
    session: Any = None
    # HTTP only: mcp closes the httpx client only when it created it itself, so
    # the one we build to carry `headers` is ours to release.
    http_client: Any = None

    async def aclose(self) -> None:
        """Release everything entered so far, newest first, swallowing failures.

        The httpx client goes last because the transport uses it to DELETE the
        session on exit. Secondary cleanup errors are dropped so that a caller
        unwinding from an earlier exception still sees the original one.
        """
        for resource in (self.session, self.transport, self.http_client):
            if resource is None:
                continue
            try:
                await resource.__aexit__(None, None, None)
            except Exception:
                pass


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
            await s.aclose()


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
            # Track the open transport/session BEFORE list_tools() so the
            # except-cleanup loop below closes them even if list_tools() or
            # tool-building raises (otherwise the subprocess/session leak).
            opened.append(await _connect_one(name, config))
            session = opened[-1].session
            result = await session.list_tools()
            mcp_tools = [make_mcp_tool(name, t, _make_call_tool(session)) for t in result.tools]
            tools.extend(mcp_tools)
        except BaseException as exc:
            for s in reversed(opened):
                await s.aclose()
            if not isinstance(exc, Exception):
                # Cancellation is not an Exception. Release first — a connect
                # cancelled by a shutdown or a timeout would otherwise strand
                # an MCP subprocess — then propagate it unchanged rather than
                # reporting it as a configuration failure.
                raise
            failures.append(f"{name}: {exc}")
            raise ConfigError(f"Failed to connect MCP server(s): {'; '.join(failures)}") from exc

    return McpConnection(tools=tools, sessions=opened)


async def _connect_one(name: str, config: McpServerConfig) -> _OpenSession:
    if mcp_server_type(config) == "http":
        return await _connect_http(name, config)
    return await _connect_stdio(name, config)


async def _connect_stdio(name: str, config: object) -> _OpenSession:
    transport = stdio_client(
        StdioServerParameters(
            command=getattr(config, "command", ""),
            args=getattr(config, "args", None) or [],
            env=getattr(config, "env", None),
        )
    )
    read, write = await transport.__aenter__()
    return await _start_session(_OpenSession(transport=transport), read, write)


async def _connect_http(name: str, config: object) -> _OpenSession:
    url = str(getattr(config, "url", ""))
    headers = dict(getattr(config, "headers", None) or {})
    # mcp 2.x has no `headers=` kwarg: per-server headers ride on an httpx client
    # the caller supplies and therefore owns.
    http_client = await create_mcp_http_client(headers or None).__aenter__()
    opened = _OpenSession(http_client=http_client)
    transport = streamable_http_client(url, http_client=http_client)
    try:
        read, write = await transport.__aenter__()
    except BaseException:
        # The transport never entered, so only the httpx client needs releasing.
        # BaseException so a cancelled connect does not leak the socket either.
        await opened.aclose()
        raise
    opened.transport = transport
    return await _start_session(opened, read, write)


async def _start_session(opened: _OpenSession, read: Any, write: Any) -> _OpenSession:
    """Open and initialize a `ClientSession` over an entered transport.

    Args:
        opened: The resources already entered for this server; unwound in
            reverse if the session fails to start.
        read: Transport read stream.
        write: Transport write stream.

    Returns:
        The same `_OpenSession`, with `session` set to the live session.
    """
    try:
        session_ctx = ClientSession(read, write)
        opened.session = await session_ctx.__aenter__()
        await opened.session.initialize()
        return opened
    except BaseException:
        await opened.aclose()
        raise


def _make_call_tool(session: ClientSession):
    async def _call(name: str, args: dict, signal: object) -> object:
        from ..abort import throw_if_aborted

        throw_if_aborted(cast(Any, signal))
        return await session.call_tool(name, args or {})

    return _call
