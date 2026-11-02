from __future__ import annotations

import asyncio
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
    session_context: Any = None
    # HTTP only: mcp closes the httpx client only when it created it itself, so
    # the one we build to carry `headers` is ours to release.
    http_client: Any = None

    async def aclose(self) -> None:
        """Release everything entered so far, newest first, swallowing failures.

        The httpx client goes last because the transport uses it to DELETE the
        session on exit. Secondary cleanup errors are dropped so that a caller
        unwinding from an earlier exception still sees the original one.
        """
        for resource in (self.session_context, self.transport, self.http_client):
            if resource is None:
                continue
            try:
                await resource.__aexit__(None, None, None)
            except (Exception, asyncio.CancelledError):
                pass


async def _wait_for_cleanup(task: asyncio.Task[None]) -> None:
    """Drain owned cleanup even if a waiter is cancelled, then propagate cancellation."""
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    task.result()
    if cancelled is not None:
        raise cancelled


class _SessionOwner:
    """Enter and exit a server's AnyIO contexts in one dedicated task."""

    def __init__(self, name: str, config: McpServerConfig) -> None:
        self._ready = asyncio.Event()
        self._close_requested = asyncio.Event()
        self._started = False
        self._error: BaseException | None = None
        self.session: Any = None
        self._task = asyncio.create_task(self._run(name, config), name=f"mcp:{name}")

    async def _run(self, name: str, config: McpServerConfig) -> None:
        opened: _OpenSession | None = None
        try:
            self._started = True
            if self._close_requested.is_set():
                return
            try:
                opened = await _connect_one(name, config)
                self.session = opened.session
                self._ready.set()
                await self._close_requested.wait()
            finally:
                if opened is not None:
                    await opened.aclose()
        except BaseException as exc:
            self._error = exc
        finally:
            # On startup failure, readiness means all partial resources have
            # already been released, and the connecting task can raise safely.
            self._ready.set()

    async def wait_ready(self) -> None:
        await self._ready.wait()
        if self._error is not None:
            raise self._error

    async def aclose(self) -> None:
        if not self._close_requested.is_set():
            self._close_requested.set()
            if self._started and not self._ready.is_set():
                # An incomplete handshake cannot wait for the normal close
                # event. Cancel once; further waiter cancellations never reach
                # the owner while it unwinds transport/session contexts.
                self._task.cancel()
        await _wait_for_cleanup(self._task)


class McpConnection:
    """Own the live MCP sessions and tools returned by a server connection.

    Args:
        tools: Linch tool wrappers created from the connected servers.
        sessions: Entered server resources released when this connection closes.

    Attributes:
        tools: Tools exposed by the connected MCP servers.
    """

    def __init__(
        self,
        tools: list[object],
        sessions: list[_SessionOwner],
    ) -> None:
        """Create a connection that owns its server resources.

        Args:
            tools: Linch tool wrappers created from the connected servers.
            sessions: Entered server resources released when this connection closes.
        """
        self.tools = tools
        self._sessions = sessions
        self._close_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        """Close every connected server session once, in reverse order."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_sessions())
        await _wait_for_cleanup(self._close_task)

    async def _close_sessions(self) -> None:
        for s in reversed(self._sessions):
            await s.aclose()


async def connect_mcp_servers(
    servers: dict[str, McpServerConfig],
) -> McpConnection:
    """Connect to MCP servers and wrap their tools for a Linch registry.

    Servers are connected in mapping order. If a later connection fails, every
    session opened so far is closed before the configuration error is raised.

    Args:
        servers: Mapping from stable server names to stdio or HTTP configurations.

    Returns:
        A connection owning the wrapped tools and all entered server resources.

    Raises:
        ConfigError: If a server name is invalid, names collide after normalization,
            or a server cannot be connected or enumerated.
    """
    entries = list(servers.items())
    normalized: set[str] = set()
    for name, _ in entries:
        if name.strip() == "":
            raise ConfigError("MCP server name must be non-empty")
        key = normalize_name_for_mcp(name)
        if key in normalized:
            raise ConfigError(f"MCP server names collide after normalization: '{name}' -> '{key}'")
        normalized.add(key)

    opened: list[_SessionOwner] = []
    tools: list[object] = []
    tool_names: set[str] = set()
    failures: list[str] = []

    for name, config in entries:
        try:
            # Track the open transport/session BEFORE list_tools() so the
            # except-cleanup loop below closes them even if list_tools() or
            # tool-building raises (otherwise the subprocess/session leak).
            owner = _SessionOwner(name, config)
            opened.append(owner)
            await owner.wait_ready()
            session = opened[-1].session
            result = await session.list_tools()
            call_tool = _make_call_tool(session, getattr(config, "timeout_s", 300))
            mcp_tools = [make_mcp_tool(name, t, call_tool) for t in result.tools]
            for tool in mcp_tools:
                tool_name = str(getattr(tool, "name", ""))
                if tool_name in tool_names:
                    raise ConfigError(f"MCP tool names collide after normalization: '{tool_name}'")
                tool_names.add(tool_name)
            tools.extend(mcp_tools)
        except BaseException as exc:
            await McpConnection(tools=[], sessions=opened).close()
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
    http_context = create_mcp_http_client(headers or None)
    http_client = await http_context.__aenter__()
    opened = _OpenSession(http_client=http_context)
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
        opened.session_context = session_ctx
        await opened.session.initialize()
        return opened
    except BaseException:
        await opened.aclose()
        raise


def _make_call_tool(session: ClientSession, timeout_s: float = 300):
    async def _drain(*tasks: asyncio.Task[Any]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _call(name: str, args: dict, signal: object) -> object:
        from ..abort import throw_if_aborted

        throw_if_aborted(cast(Any, signal))
        if signal is None:
            return await session.call_tool(name, args or {}, read_timeout_seconds=timeout_s)
        call = asyncio.create_task(
            session.call_tool(name, args or {}, read_timeout_seconds=timeout_s)
        )
        aborted = asyncio.create_task(cast(Any, signal).wait())
        try:
            await asyncio.wait((call, aborted), return_when=asyncio.FIRST_COMPLETED)
            throw_if_aborted(cast(Any, signal))
            return await call
        finally:
            await _wait_for_cleanup(asyncio.create_task(_drain(call, aborted)))

    return _call
