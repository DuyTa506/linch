"""The `server.info` handshake: what a client learns at connect, and the named
errors for a server it cannot speak to."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from test_protocol import Answering
from test_remote import TOKEN, TRANSPORTS, listening

from linch_rust import (
    HandshakeTimeout,
    IncompatibleServer,
    LinchError,
    Server,
    ServerInfo,
)
from linch_rust._rpc import Connection, StreamTransport
from linch_rust.info import PROTOCOL_MAX, PROTOCOL_MIN, handshake

WIRE: dict[str, Any] = {
    "server_version": "0.1.1",
    "protocol": 1,
    "contract_version": 1,
    "capabilities": ["cache_isolation", "run_steer"],
    "backends": ["local", "sandbox", "docker", "remote"],
    "limits": {"max_message_bytes": 16777216, "default_max_runs": 16},
}


class Recording(Answering):
    """An `Answering` writer that keeps each request it was sent."""

    def __init__(self, reader: asyncio.StreamReader, lines: list[dict[str, Any]]) -> None:
        super().__init__(reader, lines)
        self.requests: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.requests.append(json.loads(data))
        super().write(data)


def connection(
    lines: list[dict[str, Any]],
) -> tuple[Connection, Recording, asyncio.StreamReader]:
    reader = asyncio.StreamReader()
    writer = Recording(reader, lines)
    conn = Connection(StreamTransport(reader, cast(asyncio.StreamWriter, writer)))
    return conn, writer, reader


async def finish(conn: Connection, reader: asyncio.StreamReader) -> None:
    reader.feed_eof()
    await conn.finish()


async def test_the_answer_is_read_into_a_server_info() -> None:
    conn, writer, reader = connection([{"jsonrpc": "2.0", "result": WIRE}])

    info = await handshake(conn)

    assert info == ServerInfo(
        server_version="0.1.1",
        protocol=1,
        contract_version=1,
        capabilities=frozenset({"cache_isolation", "run_steer"}),
        backends=("local", "sandbox", "docker", "remote"),
        max_message_bytes=16777216,
        default_max_runs=16,
    )
    assert info.has("cache_isolation") and not info.has("workflow")
    [request] = writer.requests
    assert request["method"] == "server.info"
    assert request["params"]["client_protocol"] == PROTOCOL_MAX
    await finish(conn, reader)


async def test_a_later_server_may_add_members_and_capabilities() -> None:
    later = {**WIRE, "capabilities": ["cache_isolation", "something_new"], "extra": {"x": 1}}
    conn, _, reader = connection([{"jsonrpc": "2.0", "result": later}])

    info = await handshake(conn)

    assert info.has("something_new")
    await finish(conn, reader)


@pytest.mark.parametrize("protocol", [PROTOCOL_MIN - 1, PROTOCOL_MAX + 1])
async def test_a_protocol_outside_the_range_is_refused_by_name(protocol: int) -> None:
    conn, _, reader = connection([{"jsonrpc": "2.0", "result": {**WIRE, "protocol": protocol}}])

    with pytest.raises(IncompatibleServer) as refused:
        await handshake(conn)

    assert refused.value.protocol == protocol
    assert refused.value.server_version == "0.1.1"
    assert refused.value.supported == (PROTOCOL_MIN, PROTOCOL_MAX)
    assert f"protocol {protocol}" in str(refused.value)
    assert isinstance(refused.value, LinchError)
    await finish(conn, reader)


async def test_a_server_without_the_method_is_named_not_generic() -> None:
    missing = {"code": -32601, "message": 'no method "server.info"'}
    conn, _, reader = connection([{"jsonrpc": "2.0", "id": 1, "error": missing}])

    with pytest.raises(IncompatibleServer) as refused:
        await handshake(conn)

    assert refused.value.protocol is None
    assert "server.info" in str(refused.value) and "upgrade" in str(refused.value)
    await finish(conn, reader)


async def test_an_answer_without_a_protocol_is_refused_by_name() -> None:
    broken = {key: value for key, value in WIRE.items() if key != "protocol"}
    conn, _, reader = connection([{"jsonrpc": "2.0", "result": broken}])

    with pytest.raises(IncompatibleServer, match="protocol"):
        await handshake(conn)
    await finish(conn, reader)


async def test_a_server_that_never_answers_ends_in_a_named_timeout() -> None:
    conn, _, reader = connection([])

    with pytest.raises(HandshakeTimeout) as late:
        await handshake(conn, timeout=0.05)

    assert "server.info" in str(late.value)
    await finish(conn, reader)


def fake_server(tmp_path: Path, answer: dict[str, Any]) -> Path:
    """A server binary that answers `server.info` with `answer` and writes its pid to `pid`."""
    script = tmp_path / "fake-linch-server"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(tmp_path / 'pid')!r}, 'w').write(str(os.getpid()))\n"
        f"ANSWER = {answer!r}\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    reply = {'jsonrpc': '2.0', 'id': request['id'], **ANSWER}\n"
        "    sys.stdout.write(json.dumps(reply) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


async def test_a_refused_handshake_leaves_no_child_process(tmp_path: Path) -> None:
    binary = fake_server(tmp_path, {"result": {**WIRE, "protocol": PROTOCOL_MAX + 1}})
    server = Server(binary)

    with pytest.raises(IncompatibleServer):
        async with server:
            pytest.fail("the handshake must refuse before the block runs")

    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # The failed start left the server startable again.
    with pytest.raises(LinchError, match="not started"):
        _ = server.info


async def test_a_server_that_predates_the_method_is_named_at_start(tmp_path: Path) -> None:
    missing = {"code": -32601, "message": 'no method "server.info"'}
    binary = fake_server(tmp_path, {"error": missing})

    with pytest.raises(IncompatibleServer) as refused:
        async with Server(binary):
            pytest.fail("the handshake must refuse before the block runs")

    assert refused.value.protocol is None


async def test_a_spawned_server_reports_its_info() -> None:
    async with Server() as server:
        info = server.info

    assert info.protocol == PROTOCOL_MAX
    assert info.contract_version == 1
    assert info.has("cache_isolation") and info.has("run_steer")
    assert "local" in info.backends and "remote" in info.backends
    assert info.max_message_bytes == 16 * 1024 * 1024
    assert info.default_max_runs == 16
    assert info.server_version


@TRANSPORTS
async def test_a_tenant_connection_gets_the_same_info_as_a_local_one(transport: str) -> None:
    async with Server() as local:
        expected = local.info
    async with listening() as at, Server.connect(at.address(transport), token=TOKEN) as server:
        info = server.info

    assert info == expected
    assert "acme" not in repr(info)
