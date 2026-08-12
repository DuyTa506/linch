"""Regression tests for MCP connection cleanup on failed connects.

A failed MCP connect must not leak the stdio subprocess transport, the
`ClientSession`, or — over HTTP — the httpx client linch builds to carry the
server's headers. Each must have `__aexit__` awaited when:

  (a) `session.list_tools()` raises after a successful connect,
  (b) `session.initialize()` raises while opening the session, and
  (c) the transport itself fails to enter (HTTP only, where a resource is
      already held by then).

These tests monkeypatch the symbols `client.py` binds at import time. They
deliberately do **not** fake the `mcp` package: an earlier version injected fake
`mcp.*` modules into `sys.modules`, which hid mcp 2.0's renamed transport and
snake_case models until CI went red. `test_mcp_api_contract.py` pins the real
API those fakes stand in for.
"""

from __future__ import annotations

import asyncio
import types

import pytest

pytest.importorskip("mcp", reason="the 'mcp' extra is not installed")

from linch.mcp import client as mcp_client  # noqa: E402


class FakeTransport:
    """Async-context-manager stand-in for an stdio/http transport."""

    def __init__(self, *, n_yield: int = 2, enter_error: bool = False) -> None:
        self._yield = tuple(object() for _ in range(n_yield))
        self._enter_error = enter_error
        self.aenter_calls = 0
        self.aexit_calls = 0

    async def __aenter__(self):
        self.aenter_calls += 1
        if self._enter_error:
            raise RuntimeError("transport boom")
        return self._yield

    async def __aexit__(self, *exc) -> None:
        self.aexit_calls += 1


class FakeSession:
    """Stand-in for an mcp ClientSession used as an async context manager."""

    def __init__(
        self,
        *,
        initialize_error: bool = False,
        list_tools_error: bool = False,
        hang: bool = False,
    ) -> None:
        self.initialize_error = initialize_error
        self.list_tools_error = list_tools_error
        self.hang = hang
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
        if self.hang:
            await asyncio.sleep(30)
        raise AssertionError("list_tools should not be reached in these tests")


class FakeHttpClient:
    """Stand-in for the httpx client linch builds to carry per-server headers."""

    def __init__(self) -> None:
        self.aexit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        self.aexit_calls += 1


def _patch_http(monkeypatch, transport: FakeTransport, session: FakeSession) -> FakeHttpClient:
    """Wire the HTTP connect path to fakes and return the httpx stand-in."""
    http_client = FakeHttpClient()
    monkeypatch.setattr(mcp_client, "create_mcp_http_client", lambda *a, **k: http_client)
    monkeypatch.setattr(mcp_client, "streamable_http_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)
    return http_client


def _stdio_cfg() -> types.SimpleNamespace:
    return types.SimpleNamespace(command="echo", args=[], env=None)


def _http_cfg(headers: dict[str, str] | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="http", url="https://example.test/mcp", headers=headers)


async def test_list_tools_failure_cleans_up_transport_and_session(monkeypatch):
    """If list_tools() raises after a successful connect, BOTH the transport
    and the session must have __aexit__ awaited (no leak)."""
    transport = FakeTransport()
    session = FakeSession(list_tools_error=True)

    monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": _stdio_cfg()})

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (subprocess leaked)"


async def test_initialize_failure_cleans_up_transport_and_session(monkeypatch):
    """If initialize() raises after the session context is entered, BOTH the
    session and transport must have __aexit__ awaited (no leak)."""
    transport = FakeTransport()
    session = FakeSession(initialize_error=True)

    monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": _stdio_cfg()})

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (subprocess leaked)"


# The HTTP path holds a third resource — the httpx client that carries the
# server's headers, which mcp does not close because it did not create it.


async def test_http_list_tools_failure_cleans_up_every_resource(monkeypatch):
    """list_tools() failure over HTTP must close session, transport, and client."""
    transport = FakeTransport()
    session = FakeSession(list_tools_error=True)
    http_client = _patch_http(monkeypatch, transport, session)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": _http_cfg()})

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (HTTP transport leaked)"
    assert http_client.aexit_calls == 1, "httpx client __aexit__ not awaited (socket leaked)"


async def test_http_initialize_failure_cleans_up_every_resource(monkeypatch):
    """initialize() failure over HTTP must close session, transport, and client."""
    transport = FakeTransport()
    session = FakeSession(initialize_error=True)
    http_client = _patch_http(monkeypatch, transport, session)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": _http_cfg()})

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (HTTP transport leaked)"
    assert http_client.aexit_calls == 1, "httpx client __aexit__ not awaited (socket leaked)"


async def test_http_transport_enter_failure_releases_the_client(monkeypatch):
    """A transport that never enters still leaves the httpx client to close."""
    transport = FakeTransport(enter_error=True)
    session = FakeSession()
    http_client = _patch_http(monkeypatch, transport, session)

    with pytest.raises(mcp_client.ConfigError):
        await mcp_client.connect_mcp_servers({"srv": _http_cfg()})

    assert http_client.aexit_calls == 1, "httpx client __aexit__ not awaited (socket leaked)"
    assert transport.aexit_calls == 0, "a transport that never entered must not be exited"
    assert session.aenter_calls == 0


async def test_http_headers_are_carried_on_the_client(monkeypatch):
    """mcp 2.x has no headers= kwarg, so they must reach the httpx client."""
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


# Cancellation is not an Exception, so an `except Exception` unwind never runs.
# A connect cancelled by a shutdown or a timeout would otherwise strand the
# stdio subprocess, the session, and the httpx client.


async def _cancel_during_list_tools(coro) -> None:
    task = asyncio.create_task(coro)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelling_a_stdio_connect_releases_its_resources(monkeypatch):
    transport = FakeTransport()
    session = FakeSession(hang=True)

    monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: transport)
    monkeypatch.setattr(mcp_client, "ClientSession", lambda *a, **k: session)

    await _cancel_during_list_tools(mcp_client.connect_mcp_servers({"srv": _stdio_cfg()}))

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (subprocess leaked)"


async def test_cancelling_an_http_connect_releases_its_resources(monkeypatch):
    transport = FakeTransport()
    session = FakeSession(hang=True)
    http_client = _patch_http(monkeypatch, transport, session)

    await _cancel_during_list_tools(mcp_client.connect_mcp_servers({"srv": _http_cfg()}))

    assert session.aexit_calls == 1, "session __aexit__ not awaited (ClientSession leaked)"
    assert transport.aexit_calls == 1, "transport __aexit__ not awaited (HTTP transport leaked)"
    assert http_client.aexit_calls == 1, "httpx client __aexit__ not awaited (socket leaked)"
