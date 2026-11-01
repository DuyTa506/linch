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
