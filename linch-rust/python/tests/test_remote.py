"""A running `linch-server --config`, reached with `Server.connect` over its
Unix socket and its WebSocket as a tenant: host tools, callbacks, approvals
and the event stream work as over stdio, and the tenant's checks refuse.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import signal
import socket
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from scripted import MODEL, text_turn, tool_turn
from test_resume import pg_dsn

from linch_rust import (
    AgentConfig,
    Allow,
    HookEvent,
    InitEvent,
    LimitReached,
    LinchError,
    NotAllowed,
    PartialAssistantEvent,
    Principal,
    ResultEvent,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
    ToolOutput,
    ToolProgressEvent,
    Unauthenticated,
    callback,
    find_server,
    tool,
)
from linch_rust._remote import login

__all__ = ["pg_dsn"]

TOKEN = "tok-acme-remote-test-0001"
TRANSPORTS = pytest.mark.parametrize("transport", ["unix", "ws"])


@dataclass(frozen=True)
class Listening:
    """Where a listening server is, as it printed its addresses."""

    socket: str
    websocket: str

    def address(self, transport: str) -> str:
        return self.socket if transport == "unix" else self.websocket


@asynccontextmanager
async def listening(
    *, max_connections: int = 4, postgres_dsns: Sequence[str] = ()
) -> AsyncIterator[Listening]:
    """Run `linch-server --config` with tenants `acme` (token `TOKEN`) and `beta`."""
    # Short: a socket path is at most about 100 bytes.
    directory = Path(tempfile.mkdtemp(prefix="lr-", dir="/tmp"))
    allow = {
        "scripted_provider": True,
        "max_connections": max_connections,
        "postgres_dsns": list(postgres_dsns),
    }
    beta = {"tokens": [{"value": "tok-beta-remote-test-0001"}], "allow": {}}
    config = {
        "socket": str(directory / "linch.sock"),
        "http": "127.0.0.1:0",
        "tenants": {"acme": {"tokens": [{"value": TOKEN}], "allow": allow}, "beta": beta},
    }
    path = directory / "server.json"
    path.write_text(json.dumps(config))
    process = await asyncio.create_subprocess_exec(
        find_server(), "--config", str(path), stderr=asyncio.subprocess.PIPE, cwd=directory
    )
    assert process.stderr is not None
    try:
        addresses: dict[str, str] = {}
        while len(addresses) < 2:
            line = await asyncio.wait_for(process.stderr.readline(), 30)
            assert line, "the server exited before it listened"
            _, _, address = line.decode().strip().partition("listening on ")
            if address:
                addresses[address.split(":")[0]] = address
        yield Listening(addresses["unix"], addresses["ws"])
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 15)
        shutil.rmtree(directory, ignore_errors=True)


def acme(turns: list[dict[str, Any]], **extra: Any) -> AgentConfig:
    """A scripted config in tenant `acme`'s namespace, in mode `default`."""
    fields = {
        "namespace": "acme/support",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        **extra,
    }
    return cast(AgentConfig, fields)


@TRANSPORTS
async def test_a_host_tool_and_a_callback_run_here_as_events_stream(transport: str) -> None:
    seen: list[str] = []
    asked: list[str] = []

    @tool(scope="read")
    async def lookup(email: str) -> ToolOutput:
        """Find a customer by email."""
        seen.append(email)
        return ToolOutput({"name": "Ada"}, metadata={"source": "crm"})

    @callback("pre_tool_use", "audit", 1, matcher=["lookup"])
    async def audit(request: dict[str, Any]) -> dict[str, Any]:
        asked.append(request["tool_name"])
        return {"action": "continue"}

    turns = [tool_turn("c1", "lookup", {"email": "ada@example.com"}), text_turn("Found.")]
    async with listening() as at, Server.connect(at.address(transport), token=TOKEN) as server:
        agent = await server.open_agent(acme(turns), tools=[lookup], callbacks=[audit])
        run = await agent.run("s1", "Who is ada@example.com?")
        envelopes = [envelope async for envelope in run]
        replayed = await agent.events(run.id)

    assert seen == ["ada@example.com"] and asked == ["lookup"]
    assert isinstance(envelopes[0].event, InitEvent)
    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.tool_use_id == "c1" and end.metadata == {"source": "crm"}
    assert any(isinstance(e.event, HookEvent) for e in envelopes)
    result = envelopes[-1].event
    assert isinstance(result, ResultEvent) and result.final_text == "Found."
    stream_only = HookEvent | PartialAssistantEvent | ToolProgressEvent
    durable = [e for e in envelopes if not isinstance(e.event, stream_only)]
    assert [(e.seq, e.event) for e in replayed] == [(e.seq, e.event) for e in durable]


@TRANSPORTS
async def test_a_suspended_run_is_approved_over_the_same_connection(transport: str) -> None:
    saved: list[str] = []

    @tool(scope="write")
    async def save(text: str) -> str:
        saved.append(text)
        return "saved"

    turns = [tool_turn("c1", "save", {"text": "hello"}), text_turn("Done.")]
    principal: Principal = {"tenant": "acme", "user": "ada"}
    async with listening() as at, Server.connect(at.address(transport), token=TOKEN) as server:
        agent = await server.open_agent(acme(turns), tools=[save])
        run = await agent.run("s1", "Save hello", principal=principal)
        stopped = await run.result()
        assert isinstance(stopped, SuspendedEvent) and saved == []
        approved = await agent.approve(run.id, {"c1": Allow()}, principal=principal)
        result = await approved.result()

    assert saved == ["hello"]
    assert isinstance(result, ResultEvent) and result.final_text == "Done."


@TRANSPORTS
async def test_cancelling_a_run_cancels_its_host_tool_here(transport: str) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    @tool(scope="read")
    async def wait(q: str) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return q

    turns = [tool_turn("c1", "wait", {"q": "x"}), text_turn("unused")]
    async with listening() as at, Server.connect(at.address(transport), token=TOKEN) as server:
        agent = await server.open_agent(acme(turns), tools=[wait])
        run = await agent.run("s1", "Wait")
        await asyncio.wait_for(started.wait(), 10)
        assert await run.cancel()
        result = await run.result()
        await asyncio.wait_for(cancelled.wait(), 10)

    assert isinstance(result, ResultEvent) and result.subtype == "aborted"


@TRANSPORTS
async def test_an_unknown_token_is_refused_without_echoing_it(transport: str) -> None:
    async with listening() as at:
        with pytest.raises(Unauthenticated) as refused:
            async with Server.connect(at.address(transport), token="tok-wrong-0002"):
                pass

    assert refused.value.code == -32003
    assert "tok-wrong-0002" not in str(refused.value)


@TRANSPORTS
async def test_the_tenants_checks_refuse_what_it_may_not_do(transport: str) -> None:
    async with listening() as at, Server.connect(at.address(transport), token=TOKEN) as server:
        with pytest.raises(NotAllowed) as namespace:
            await server.open_agent({**acme([]), "namespace": "beta"})
        with pytest.raises(NotAllowed) as tasks:
            await server.open_agent({**acme([]), "tasks": True})
        agent = await server.open_agent(acme([text_turn("unused")]))
        with pytest.raises(NotAllowed) as principal:
            await agent.run("s1", "hi", principal={"tenant": "beta", "user": "eve"})

    assert (namespace.value.kind, tasks.value.kind) == ("namespace", "auto_allow")
    assert principal.value.kind == "principal"


@TRANSPORTS
async def test_a_tenant_at_its_connection_cap_is_refused(transport: str) -> None:
    async with listening(max_connections=1) as at:
        address = at.address(transport)
        async with Server.connect(address, token=TOKEN):
            with pytest.raises(LimitReached) as refused:
                async with Server.connect(address, token=TOKEN):
                    pass
        # The place is free again once the first connection closed.
        async with Server.connect(address, token=TOKEN) as server:
            await server.open_agent(acme([]))

    assert refused.value.kind == "connections"


async def test_a_login_past_the_pending_login_cap_is_refused_as_a_limit() -> None:
    async with listening() as at:
        path = at.socket.removeprefix("unix:")
        # 32 silent connections hold the server's places for a login until
        # its 10 second timeout, and it refuses the other 8 at once. Wait
        # for those 8, so no place is still free when the client logs in.
        held = [await asyncio.open_unix_connection(path) for _ in range(40)]
        waits = {asyncio.ensure_future(reader.readline()) for reader, _ in held}
        try:
            answered = 0
            while answered < 8:
                done, waits = await asyncio.wait(
                    waits, timeout=5, return_when=asyncio.FIRST_COMPLETED
                )
                assert done, "the server refused fewer than 8 silent connections"
                answered += len(done)
            with pytest.raises(LimitReached) as refused:
                async with Server.connect(at.socket, token=TOKEN):
                    pass
        finally:
            for wait in waits:
                wait.cancel()
            for _, writer in held:
                writer.close()

    assert (refused.value.kind, refused.value.max) == ("logins", 32)


async def test_a_login_the_server_refused_before_the_write_still_reads_its_answer() -> None:
    # What the real server does past its pending-login cap, in the order
    # it loses on a fast machine: answer with a null id and close, all
    # before the client writes its `connection.auth`.
    client, server = socket.socketpair()
    refusal = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32005, "message": "at most 32 logins", "data": {"kind": "logins"}},
    }
    server.sendall(json.dumps(refusal).encode() + b"\n")
    server.close()
    client.setblocking(False)
    try:
        with pytest.raises(LimitReached) as refused:
            await login(asyncio.get_running_loop(), client, TOKEN)
    finally:
        client.close()

    assert refused.value.kind == "logins"


async def test_a_postgres_run_suspended_on_the_socket_is_approved_over_the_websocket(
    pg_dsn: str,
) -> None:
    saved: list[str] = []

    @tool(scope="write")
    async def save(text: str) -> str:
        saved.append(text)
        return "saved"

    stores = {"type": "postgres", "dsn": pg_dsn}
    async with listening(postgres_dsns=[pg_dsn]) as at:
        async with Server.connect(at.socket, token=TOKEN) as server:
            first = acme([tool_turn("c1", "save", {"text": "hi"})], stores=stores)
            agent = await server.open_agent(first, tools=[save])
            run = await agent.run("s1", "Save hi", run_id="remote-1")
            assert isinstance(await run.result(), SuspendedEvent)
        async with Server.connect(at.websocket, token=TOKEN) as server:
            second = acme([text_turn("Saved.")], stores=stores)
            agent = await server.open_agent(second, tools=[save])
            result = await (await agent.approve("remote-1", {"c1": Allow()})).result()

    assert saved == ["hi"]
    assert isinstance(result, ResultEvent) and result.final_text == "Saved."


async def test_a_remote_connection_names_its_tenant() -> None:
    async with listening() as at:
        async with Server.connect(at.socket, token=TOKEN) as server:
            assert server.tenant == "acme"
        # The WebSocket upgrade names it in its `Linch-Tenant` header.
        async with Server.connect(at.websocket, token=TOKEN) as server:
            assert server.tenant == "acme"
    async with Server() as server:
        assert server.tenant is None


def test_an_address_must_name_a_socket_or_a_websocket() -> None:
    for bad in ["http://127.0.0.1:8731/rpc", "/run/linch/linch.sock", "unix:"]:
        with pytest.raises(ValueError, match="unix:PATH"):
            Server.connect(bad, token=TOKEN)


async def test_a_remote_server_has_no_process_here() -> None:
    async with listening() as at, Server.connect(at.socket, token=TOKEN) as server:
        with pytest.raises(LinchError, match="no process"):
            _ = server.pid
