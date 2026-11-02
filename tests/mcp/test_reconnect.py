"""MCP reconnect supervision tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp", reason="the 'mcp' extra is not installed")

from linch.mcp import client as mcp_client  # noqa: E402
from linch.mcp.config import McpStdioServerConfig  # noqa: E402
from linch.tools.base import ToolContext  # noqa: E402

_STDIO_SERVER = r"""
import json
import os
import pathlib
import sys

marker = pathlib.Path(sys.argv[1])
pid_marker = pathlib.Path(sys.argv[2])
pid_marker.write_text(str(os.getpid()))

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": request.get("params", {}).get("protocolVersion", "2025-11-25"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "reconnect-fixture", "version": "1"},
            },
        })
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "tools": [{
                    "name": "echo",
                    "description": "echo",
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                }],
            },
        })
    elif method == "tools/call":
        current = int(marker.read_text()) if marker.exists() else 0
        marker.write_text(str(current + 1))
        if current == 0:
            os._exit(0)
        send({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "content": [{
                    "type": "text",
                    "text": "ok:" + request.get("params", {}).get("arguments", {}).get("text", ""),
                }],
            },
        })
"""

_IDLE_STDIO_SERVER = r"""
import json
import os
import pathlib
import sys

starts = pathlib.Path(sys.argv[1])
count = int(starts.read_text()) if starts.exists() else 0
starts.write_text(str(count + 1))

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "idle", "version": "1"},
            },
        })
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "tools": [{"name": "echo", "inputSchema": {"type": "object"}}],
            },
        })
    elif method == "tools/call":
        send({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"content": [{"type": "text", "text": "done"}]},
        })
        os._exit(0)
"""


@pytest.mark.timeout(15)
async def test_stdio_lost_call_is_not_replayed_and_reconnects(tmp_path: Path) -> None:
    marker = tmp_path / "calls"
    pid_marker = tmp_path / "pid"
    config = McpStdioServerConfig(
        command=sys.executable,
        args=["-c", _STDIO_SERVER, str(marker), str(pid_marker)],
        env={"PATH": os.environ.get("PATH", "")},
    )
    connection = await mcp_client.connect_mcp_servers({"local": config})
    try:
        tool = connection.tools[0]
        failed = await tool.execute(
            {"text": "first"},
            ToolContext(cwd=str(tmp_path), session_id="s", run_id="r", session_store=None),
        )
        assert failed.is_error is True
        assert marker.read_text() == "1"
        await asyncio.sleep(0.8)
        assert marker.read_text() == "1", "the failed call was replayed during reconnect"
        succeeded = await tool.execute(
            {"text": "second"},
            ToolContext(cwd=str(tmp_path), session_id="s", run_id="r", session_store=None),
        )
        assert succeeded.content == "ok:second"
        assert marker.read_text() == "2"
        assert tool.name == "mcp__local__echo"
    finally:
        await connection.close()
    pid = int(pid_marker.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        pytest.fail("stdio MCP process was not reaped")


@pytest.mark.timeout(15)
async def test_idle_stdio_eof_starts_reconnect_without_a_call(tmp_path: Path) -> None:
    starts = tmp_path / "starts"
    config = McpStdioServerConfig(
        command=sys.executable,
        args=["-c", _IDLE_STDIO_SERVER, str(starts)],
        env={"PATH": os.environ.get("PATH", "")},
    )
    connection = await mcp_client.connect_mcp_servers({"local": config})
    try:
        result = await connection.tools[0].execute(
            {}, ToolContext(cwd=str(tmp_path), session_id="s", run_id="r", session_store=None)
        )
        assert result.content == "done"
        await _wait_until(lambda: starts.exists() and int(starts.read_text()) >= 2, timeout=3)
        assert connection.tools[0].name == "mcp__local__echo"
    finally:
        await connection.close()


async def test_loss_aware_read_preserves_async_iterator_eof() -> None:
    lost = asyncio.Event()

    class EofStream:
        async def __anext__(self):
            raise StopAsyncIteration

    wrapper = mcp_client._LossAwareRead(EofStream(), lost)
    assert [item async for item in wrapper] == []
    assert lost.is_set()


class _Resource:
    def __init__(self, session: object) -> None:
        self.session = session
        self.exits = 0

    async def __aexit__(self, *exc: object) -> None:
        self.exits += 1


class _CallSession:
    def __init__(self, outcome: object = "ok") -> None:
        self.outcome = outcome
        self.calls = 0

    async def call_tool(self, name: str, args: dict, **kwargs: object) -> object:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _opened(session: _CallSession) -> mcp_client._OpenSession:
    resource = _Resource(session)
    return mcp_client._OpenSession(session=session, session_context=resource, transport=resource)


async def _wait_until(predicate, timeout: float = 2) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout)


async def test_reconnect_attempts_backoff_then_stable_unavailable(monkeypatch) -> None:
    sessions = [_CallSession(ConnectionError("closed")), _CallSession("reconnected")]
    attempts = 0

    async def connect(name: str, config: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _opened(sessions[0])
        raise ConnectionError("down")

    monkeypatch.setattr(mcp_client, "_connect_one", connect)
    monkeypatch.setattr(mcp_client, "_RECONNECT_INITIAL_DELAY", 0.001)
    monkeypatch.setattr(mcp_client, "_RECONNECT_MAX_DELAY", 0.001)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    try:
        with pytest.raises(ConnectionError):
            await owner.call_tool("echo", {}, None)
        await _wait_until(lambda: owner.unavailable)
        assert attempts == 6
        with pytest.raises(RuntimeError, match="unavailable"):
            await owner.call_tool("echo", {}, None)
        await asyncio.sleep(0.01)
        assert attempts == 6
    finally:
        await owner.aclose()


async def test_reconnect_backoff_uses_exponential_delays(monkeypatch) -> None:
    delays: list[float] = []
    original_wait_for = mcp_client.asyncio.wait_for
    initial = _CallSession(ConnectionError("closed"))
    attempts = 0

    async def fake_wait_for(awaitable, timeout):
        delays.append(timeout)
        awaitable.close()
        raise TimeoutError

    async def connect(name: str, config: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _opened(initial)
        raise ConnectionError("down")

    monkeypatch.setattr(mcp_client, "_connect_one", connect)
    monkeypatch.setattr(mcp_client.asyncio, "wait_for", fake_wait_for)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    try:
        with pytest.raises(ConnectionError):
            await owner.call_tool("echo", {}, None)
        await asyncio.sleep(0.01)
        assert delays == [0.5, 1.0, 2.0, 4.0, 8.0]
    finally:
        monkeypatch.setattr(mcp_client.asyncio, "wait_for", original_wait_for)
        await owner.aclose()


async def test_close_interrupts_reconnect_backoff_and_reaps_generation(monkeypatch) -> None:
    initial = _CallSession(ConnectionError("closed"))
    attempts = 0
    started = asyncio.Event()

    async def connect(name: str, config: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _opened(initial)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp_client, "_connect_one", connect)
    monkeypatch.setattr(mcp_client, "_RECONNECT_INITIAL_DELAY", 10)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    with pytest.raises(ConnectionError):
        await owner.call_tool("echo", {}, None)
    await owner.aclose()
    assert attempts == 1
    assert initial.calls == 1


async def test_close_interrupts_reconnect_handshake(monkeypatch) -> None:
    initial = _CallSession(ConnectionError("closed"))
    started = asyncio.Event()
    attempts = 0

    async def connect(name: str, config: object):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _opened(initial)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp_client, "_connect_one", connect)
    monkeypatch.setattr(mcp_client, "_RECONNECT_INITIAL_DELAY", 0.001)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    with pytest.raises(ConnectionError):
        await owner.call_tool("echo", {}, None)
    await asyncio.wait_for(started.wait(), 1)
    await owner.aclose()
    assert attempts == 2


async def test_repeated_outages_start_a_fresh_attempt_budget(monkeypatch) -> None:
    first = _CallSession(ConnectionError("first down"))
    second = _CallSession(ConnectionError("second down"))
    third = _CallSession("healthy")
    sessions = iter([first, second, third])
    attempts = 0

    async def connect(name: str, config: object):
        nonlocal attempts
        attempts += 1
        return _opened(next(sessions))

    monkeypatch.setattr(mcp_client, "_connect_one", connect)
    monkeypatch.setattr(mcp_client, "_RECONNECT_INITIAL_DELAY", 0.001)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    try:
        with pytest.raises(ConnectionError):
            await owner.call_tool("echo", {}, None)
        await _wait_until(lambda: owner.session is not None and owner.session is not first)
        with pytest.raises(ConnectionError):
            await owner.call_tool("echo", {}, None)
        await _wait_until(lambda: owner.session is not None and owner.session is not second)
        assert await owner.call_tool("echo", {}, None) == "healthy"
        assert not owner.unavailable
        assert attempts == 3
    finally:
        await owner.aclose()


async def test_normal_tool_error_and_abort_do_not_trigger_reconnect(monkeypatch) -> None:
    from mcp.shared.exceptions import MCPError

    from linch.abort import AbortContext
    from linch.errors import AbortError

    session = _CallSession(MCPError(code=-32602, message="unknown tool"))
    attempts = 0

    async def connect(name: str, config: object):
        nonlocal attempts
        attempts += 1
        return _opened(session)

    monkeypatch.setattr(mcp_client, "_connect_one", connect)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    try:
        with pytest.raises(MCPError):
            await owner.call_tool("gone", {}, None)
        signal = AbortContext()
        signal.abort()
        with pytest.raises(AbortError):
            await owner.call_tool("gone", {}, signal)
        session.outcome = TimeoutError("timed out")
        with pytest.raises(TimeoutError):
            await owner.call_tool("gone", {}, None)
        await asyncio.sleep(0.01)
        assert attempts == 1
        assert owner.session is session
    finally:
        await owner.aclose()


async def test_reconnected_vanished_tool_keeps_wrapper_name_and_returns_error(monkeypatch) -> None:
    from mcp.shared.exceptions import MCPError
    from mcp.types import Tool

    from linch.mcp.tool import make_mcp_tool

    first = _CallSession(ConnectionError("closed"))
    second = _CallSession(MCPError(code=-32602, message="unknown tool"))
    attempts = 0

    async def connect(name: str, config: object):
        nonlocal attempts
        attempts += 1
        return _opened(first if attempts == 1 else second)

    monkeypatch.setattr(mcp_client, "_connect_one", connect)
    monkeypatch.setattr(mcp_client, "_RECONNECT_INITIAL_DELAY", 0.001)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    tool = make_mcp_tool("srv", Tool(name="gone", inputSchema={}), owner.call_tool)
    try:
        with pytest.raises(ConnectionError):
            await owner.call_tool("gone", {}, None)
        await _wait_until(lambda: owner.session is second)
        result = await tool.execute(
            {}, ToolContext(cwd=".", session_id="s", run_id="r", session_store=None)
        )
        assert result.is_error is True
        assert tool.name == "mcp__srv__gone"
        assert owner.session is second
    finally:
        await owner.aclose()


async def test_stale_generation_failures_share_one_reconnect(monkeypatch) -> None:
    class StaleSession(_CallSession):
        def __init__(self) -> None:
            super().__init__(ConnectionError("closed"))
            self.entered = 0
            self.release = asyncio.Event()

        async def call_tool(self, name: str, args: dict, **kwargs: object) -> object:
            self.entered += 1
            if self.entered == 2:
                self.release.set()
            await self.release.wait()
            return await super().call_tool(name, args, **kwargs)

    first = StaleSession()
    replacement = _CallSession("ok")
    attempts = 0

    async def connect(name: str, config: object):
        nonlocal attempts
        attempts += 1
        return _opened(first if attempts == 1 else replacement)

    monkeypatch.setattr(mcp_client, "_connect_one", connect)
    monkeypatch.setattr(mcp_client, "_RECONNECT_INITIAL_DELAY", 0.001)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    try:
        results = await asyncio.gather(
            owner.call_tool("echo", {}, None),
            owner.call_tool("echo", {}, None),
            return_exceptions=True,
        )
        assert all(isinstance(result, ConnectionError) for result in results)
        await _wait_until(lambda: owner.session is replacement)
        assert attempts == 2
    finally:
        await owner.aclose()


async def test_fresh_owner_recovers_after_exhaustion(monkeypatch) -> None:
    initial = _CallSession(ConnectionError("closed"))
    calls = 0

    async def fail_connect(name: str, config: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _opened(initial)
        raise ConnectionError("offline")

    monkeypatch.setattr(mcp_client, "_connect_one", fail_connect)
    monkeypatch.setattr(mcp_client, "_RECONNECT_INITIAL_DELAY", 0.001)
    monkeypatch.setattr(mcp_client, "_RECONNECT_MAX_DELAY", 0.001)
    owner = mcp_client._SessionOwner("srv", SimpleNamespace())
    await owner.wait_ready()
    with pytest.raises(ConnectionError):
        await owner.call_tool("echo", {}, None)
    await _wait_until(lambda: owner.unavailable)
    await owner.aclose()

    fresh = _CallSession("fresh")

    async def fresh_connect(name: str, config: object):
        return _opened(fresh)

    monkeypatch.setattr(mcp_client, "_connect_one", fresh_connect)
    replacement = mcp_client._SessionOwner("srv", SimpleNamespace())
    await replacement.wait_ready()
    try:
        assert await replacement.call_tool("echo", {}, None) == "fresh"
    finally:
        await replacement.aclose()
