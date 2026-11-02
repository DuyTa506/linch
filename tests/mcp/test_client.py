from __future__ import annotations

import asyncio
import types

import pytest

pytest.importorskip("mcp", reason="the 'mcp' extra is not installed")

from linch.mcp import client as mcp_client  # noqa: E402


class _Resource:
    def __init__(
        self,
        *,
        result=None,
        enter_error: bool = False,
        initialize_error: bool = False,
        list_tools_error: bool = False,
        hang: bool = False,
    ) -> None:
        self._result = result if result is not None else self
        self._enter_error = enter_error
        self.initialize_error = initialize_error
        self.list_tools_error = list_tools_error
        self.hang = hang
        self.aenter_calls = 0
        self.aexit_calls = 0

    async def __aenter__(self):
        self.aenter_calls += 1
        if self._enter_error:
            raise RuntimeError("transport boom")
        return self._result

    async def __aexit__(self, *exc) -> None:
        self.aexit_calls += 1

    async def initialize(self) -> None:
        if self.initialize_error:
            raise RuntimeError("initialize boom")

    async def list_tools(self):
        if self.list_tools_error:
            raise RuntimeError("list_tools boom")
        if self.hang:
            await asyncio.sleep(30)
        raise AssertionError("list_tools should not be reached in these tests")


class FakeTransport(_Resource):
    def __init__(self, *, n_yield: int = 2, enter_error: bool = False) -> None:
        super().__init__(result=tuple(object() for _ in range(n_yield)), enter_error=enter_error)


class FakeSession(_Resource):
    def __init__(
        self, *, initialize_error: bool = False, list_tools_error: bool = False, hang: bool = False
    ) -> None:
        super().__init__(
            initialize_error=initialize_error, list_tools_error=list_tools_error, hang=hang
        )


class ToolSession(FakeSession):
    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names

    async def list_tools(self):
        return types.SimpleNamespace(
            tools=[
                types.SimpleNamespace(
                    name=name,
                    description="",
                    input_schema={},
                    annotations=types.SimpleNamespace(
                        read_only_hint=False,
                        destructive_hint=name == "wipe_db",
                    ),
                )
                for name in self.names
            ]
        )


class FakeHttpClient(_Resource):
    pass


def _patch_http(monkeypatch, transport: FakeTransport, session: FakeSession) -> FakeHttpClient:
    http_client = FakeHttpClient()
    monkeypatch.setattr(mcp_client, "create_mcp_http_client", lambda *a, **k: http_client)
    monkeypatch.setattr(mcp_client, "streamable_http_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)
    return http_client


def _stdio_cfg() -> types.SimpleNamespace:
    return types.SimpleNamespace(command="echo", args=[], env=None)


def _http_cfg(headers: dict[str, str] | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="http", url="https://example.test/mcp", headers=headers)


async def _assert_failure(
    monkeypatch, config: types.SimpleNamespace, *, http: bool = False, initialize: bool = False
) -> None:
    transport = FakeTransport()
    session = FakeSession(initialize_error=initialize, list_tools_error=not initialize)
    http_client = _patch_http(monkeypatch, transport, session) if http else None
    if not http:
        monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: transport)
        monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)
    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": config})
    assert session.aexit_calls == 1
    assert transport.aexit_calls == 1
    if http_client is not None:
        assert http_client.aexit_calls == 1


async def test_list_tools_failure_cleans_up_transport_and_session(monkeypatch):
    await _assert_failure(monkeypatch, _stdio_cfg())


async def test_initialize_failure_cleans_up_transport_and_session(monkeypatch):
    await _assert_failure(monkeypatch, _stdio_cfg(), initialize=True)


async def test_http_list_tools_failure_cleans_up_every_resource(monkeypatch):
    await _assert_failure(monkeypatch, _http_cfg(), http=True)


async def test_http_initialize_failure_cleans_up_every_resource(monkeypatch):
    await _assert_failure(monkeypatch, _http_cfg(), http=True, initialize=True)


async def test_http_transport_enter_failure_releases_the_client(monkeypatch):
    transport = FakeTransport(enter_error=True)
    session = FakeSession()
    http_client = _patch_http(monkeypatch, transport, session)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": _http_cfg()})

    assert http_client.aexit_calls == 1, "httpx client __aexit__ not awaited (socket leaked)"
    assert transport.aexit_calls == 0, "a transport that never entered must not be exited"
    assert session.aenter_calls == 0


async def test_http_headers_are_carried_on_the_client(monkeypatch):
    transport = FakeTransport(enter_error=True)
    session = FakeSession()
    seen: list[object] = []

    http_client = FakeHttpClient()

    def fake_create(headers=None, *a, **k):
        seen.append(headers)
        return http_client

    monkeypatch.setattr(mcp_client, "create_mcp_http_client", fake_create)
    monkeypatch.setattr(mcp_client, "streamable_http_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers(
            {"srv": _http_cfg(headers={"Authorization": "Bearer t"})}
        )

    assert seen == [{"Authorization": "Bearer t"}]


async def _cancel_during_list_tools(coro) -> None:
    task = asyncio.create_task(coro)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("http", [False, True])
async def test_cancelling_connect_releases_resources(monkeypatch, http: bool):
    transport = FakeTransport()
    session = FakeSession(hang=True)
    http_client = _patch_http(monkeypatch, transport, session) if http else None
    if not http:
        monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: transport)
        monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)
    config = _http_cfg() if http else _stdio_cfg()
    await _cancel_during_list_tools(mcp_client.connect_mcp_servers({"srv": config}))
    assert session.aexit_calls == 1
    assert transport.aexit_calls == 1
    if http_client is not None:
        assert http_client.aexit_calls == 1


async def test_tool_name_collisions_fail_closed_and_clean_up(monkeypatch):
    """Reject normalized MCP tool collisions before a connection is attached."""
    transport = FakeTransport()
    session = ToolSession(["wipe_db", "get.status", "get_status"])
    monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    with pytest.raises(mcp_client.ConfigError, match="tool names collide"):
        await mcp_client.connect_mcp_servers({"ops": _stdio_cfg()})

    assert session.aexit_calls == 1
    assert transport.aexit_calls == 1


@pytest.mark.parametrize("http", [False, True])
async def test_cancelling_initialization_releases_resources(monkeypatch, http):
    started = asyncio.Event()

    class InitializingSession(FakeSession):
        async def initialize(self):
            started.set()
            await asyncio.Event().wait()

    transport = FakeTransport()
    session = InitializingSession()
    http_client = _patch_http(monkeypatch, transport, session) if http else None
    if not http:
        monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: transport)
        monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)
    config = _http_cfg() if http else _stdio_cfg()
    connecting = asyncio.create_task(mcp_client.connect_mcp_servers({"srv": config}))
    await started.wait()
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert session.aexit_calls == transport.aexit_calls == 1
    if http_client is not None:
        assert http_client.aexit_calls == 1


async def test_http_closes_contexts_rather_than_their_entered_values(monkeypatch):
    client_context = _Resource(result=object())
    session = ToolSession([])
    session_context = _Resource(result=session)
    transport = FakeTransport()
    monkeypatch.setattr(mcp_client, "create_mcp_http_client", lambda *a: client_context)
    monkeypatch.setattr(mcp_client, "streamable_http_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a: session_context)

    connection = await mcp_client.connect_mcp_servers({"srv": _http_cfg()})
    await connection.close()

    assert session_context.aexit_calls == transport.aexit_calls == client_context.aexit_calls == 1
    assert session.aexit_calls == 0


async def test_concurrent_close_and_cancelled_waiters_drain_every_server(monkeypatch):
    closing = asyncio.Event()
    release = asyncio.Event()

    class SlowClosingSession(ToolSession):
        async def __aexit__(self, *exc):
            closing.set()
            await release.wait()
            await super().__aexit__(*exc)

    sessions = [ToolSession([]), SlowClosingSession([])]
    transports = [FakeTransport(), FakeTransport()]
    session_iter, transport_iter = iter(sessions), iter(transports)
    monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: next(transport_iter))
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a: next(session_iter))
    connection = await mcp_client.connect_mcp_servers({"one": _stdio_cfg(), "two": _stdio_cfg()})
    first = asyncio.create_task(connection.close())
    await closing.wait()
    second = asyncio.create_task(connection.close())
    try:
        await asyncio.sleep(0)
        assert not second.done(), "every closer must wait until cleanup finishes"
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
    finally:
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None
    assert all(resource.aexit_calls == 1 for resource in [*sessions, *transports])
    await connection.close()


@pytest.mark.parametrize("http", [False, True])
@pytest.mark.parametrize("timeout_s", [None, 0.125])
async def test_configured_request_timeout_reaches_each_transport(monkeypatch, http, timeout_s):
    from mcp.types import CallToolResult, TextContent

    from linch.mcp.config import McpHttpServerConfig, McpStdioServerConfig
    from linch.tools.base import ToolContext

    class TimeoutSession(ToolSession):
        async def call_tool(self, name, args, *, read_timeout_seconds=None):
            return CallToolResult(content=[TextContent(text=f"timeout:{read_timeout_seconds}")])

    kwargs = {} if timeout_s is None else {"timeout_s": timeout_s}
    config = (
        McpHttpServerConfig(url="https://example.test/mcp", **kwargs)
        if http
        else McpStdioServerConfig(command="echo", **kwargs)
    )
    session = TimeoutSession(["test"])
    transport = FakeTransport()
    if http:
        _patch_http(monkeypatch, transport, session)
    else:
        monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: transport)
        monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)
    connection = await mcp_client.connect_mcp_servers({"srv": config})
    try:
        result = await connection.tools[0].execute(
            {}, ToolContext(cwd=".", session_id="s", run_id="r", session_store=None)
        )
        assert result.content == ("timeout:300" if timeout_s is None else "timeout:0.125")
    finally:
        await connection.close()
