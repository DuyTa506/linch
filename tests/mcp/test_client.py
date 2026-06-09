"""Regression tests for MCP connection cleanup on failed connects.

These tests verify that a failed MCP connect does NOT leak the stdio
subprocess transport or the ClientSession. Both must have ``__aexit__``
awaited when:

  (a) ``session.list_tools()`` raises after a successful connect, and
  (b) ``session.initialize()`` raises while opening the session.

The real ``mcp`` package is not importable in this environment, so we inject
fake ``mcp.*`` modules into ``sys.modules`` before importing the client module,
then monkeypatch the module-level transport/session symbols the code uses.
"""

from __future__ import annotations

import sys
import types

import pytest


def _install_fake_mcp() -> None:
    """Install minimal fake ``mcp.*`` modules so client.py can import."""
    if "mcp.client.session" in sys.modules:
        return

    mcp = types.ModuleType("mcp")
    mcp.__path__ = []  # mark as a package
    mcp_client = types.ModuleType("mcp.client")
    mcp_client.__path__ = []
    session_mod = types.ModuleType("mcp.client.session")
    stdio_mod = types.ModuleType("mcp.client.stdio")
    http_mod = types.ModuleType("mcp.client.streamable_http")
    types_mod = types.ModuleType("mcp.types")

    class ClientSession:  # placeholder; tests monkeypatch the symbol
        def __init__(self, *a, **k) -> None: ...

    class StdioServerParameters:
        def __init__(self, *a, **k) -> None:
            self.args = a
            self.kwargs = k

    def stdio_client(*a, **k):  # placeholder; tests monkeypatch the symbol
        raise NotImplementedError

    def streamablehttp_client(*a, **k):  # placeholder; tests monkeypatch
        raise NotImplementedError

    class CallToolResult:  # placeholder consumed by mcp.tool import
        ...

    class Tool:  # placeholder consumed by mcp.tool import
        inputSchema = dict

    class TextContent:  # placeholder consumed by mcp.result import
        ...

    session_mod.ClientSession = ClientSession
    stdio_mod.StdioServerParameters = StdioServerParameters
    stdio_mod.stdio_client = stdio_client
    http_mod.streamablehttp_client = streamablehttp_client
    types_mod.CallToolResult = CallToolResult
    types_mod.Tool = Tool
    types_mod.TextContent = TextContent

    mcp.client = mcp_client
    mcp_client.session = session_mod
    mcp_client.stdio = stdio_mod
    mcp_client.streamable_http = http_mod

    sys.modules["mcp"] = mcp
    sys.modules["mcp.client"] = mcp_client
    sys.modules["mcp.client.session"] = session_mod
    sys.modules["mcp.client.stdio"] = stdio_mod
    sys.modules["mcp.client.streamable_http"] = http_mod
    sys.modules["mcp.types"] = types_mod


_install_fake_mcp()

try:
    from linch.mcp import client as mcp_client
except ModuleNotFoundError:  # pragma: no cover - optional dep absent
    pytest.skip("linch.mcp.client not importable", allow_module_level=True)


class FakeTransport:
    """Async-context-manager stand-in for an stdio/http transport."""

    def __init__(self, *, n_yield: int) -> None:
        self._yield = tuple(object() for _ in range(n_yield))
        self.aenter_calls = 0
        self.aexit_calls = 0

    async def __aenter__(self):
        self.aenter_calls += 1
        return self._yield

    async def __aexit__(self, *exc) -> None:
        self.aexit_calls += 1


class FakeSession:
    """Stand-in for an mcp ClientSession used as an async context manager."""

    def __init__(self, *, initialize_error: bool = False, list_tools_error: bool = False) -> None:
        self.initialize_error = initialize_error
        self.list_tools_error = list_tools_error
        self.aenter_calls = 0
        self.aexit_calls = 0

    async def __aenter__(self):
        self.aenter_calls += 1
        return self

    async def __aexit__(self, *exc) -> None:
        self.aexit_calls += 1

    async def initialize(self) -> None:
        if self.initialize_error:
            raise RuntimeError("initialize boom")

    async def list_tools(self):
        if self.list_tools_error:
            raise RuntimeError("list_tools boom")
        raise AssertionError("list_tools should not be reached in these tests")


@pytest.mark.asyncio
async def test_list_tools_failure_cleans_up_transport_and_session(monkeypatch):
    """If list_tools() raises after a successful connect, BOTH the transport
    and the session must have __aexit__ awaited (no leak)."""
    transport = FakeTransport(n_yield=2)
    session = FakeSession(list_tools_error=True)

    def fake_stdio_client(*a, **k):
        return transport

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    cfg = types.SimpleNamespace(command="echo", args=[], env=None)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": cfg})

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (subprocess leaked)"


@pytest.mark.asyncio
async def test_initialize_failure_cleans_up_transport_and_session(monkeypatch):
    """If initialize() raises after the session context is entered, BOTH the
    session and transport must have __aexit__ awaited (no leak)."""
    transport = FakeTransport(n_yield=2)
    session = FakeSession(initialize_error=True)

    def fake_stdio_client(*a, **k):
        return transport

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    cfg = types.SimpleNamespace(command="echo", args=[], env=None)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": cfg})

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (subprocess leaked)"


# The HTTP transport yields a 3-tuple (read, write, _) instead of stdio's 2-tuple,
# and is selected by ``config.type == "http"``.  These mirror the stdio tests for
# the ``_connect_http`` cleanup path.


@pytest.mark.asyncio
async def test_http_list_tools_failure_cleans_up_transport_and_session(monkeypatch):
    """list_tools() failure over HTTP must still close transport and session."""
    transport = FakeTransport(n_yield=3)
    session = FakeSession(list_tools_error=True)

    monkeypatch.setattr(mcp_client, "streamablehttp_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    cfg = types.SimpleNamespace(type="http", url="https://example.test/mcp", headers=None)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": cfg})

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (HTTP transport leaked)"


@pytest.mark.asyncio
async def test_http_initialize_failure_cleans_up_transport_and_session(monkeypatch):
    """initialize() failure over HTTP must still close transport and session."""
    transport = FakeTransport(n_yield=3)
    session = FakeSession(initialize_error=True)

    monkeypatch.setattr(mcp_client, "streamablehttp_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    cfg = types.SimpleNamespace(type="http", url="https://example.test/mcp", headers=None)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": cfg})

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (HTTP transport leaked)"
