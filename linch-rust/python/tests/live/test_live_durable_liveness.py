"""Live: the liveness section of `linch-server --config` (#165), against the real binary.

RED: keepalive times below one second. The config says each field is "at least 1" (a
millisecond count), but the kernel takes whole seconds, so a sub-second idle time or probe
interval is rounded to 0 and `setsockopt` answers EINVAL: the server logs "cannot set TCP
keepalive on a connection" for every connection and the connection has no keepalive at all.
Skips without a server binary.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from pathlib import Path

import pytest

from linch_rust import find_server

pytestmark = pytest.mark.live

TOKEN = "tok-live-keepalive-0001"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _upgrade(port: int) -> str:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(
            (
                f"GET /rpc HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {TOKEN}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        return s.recv(4096).split(b"\r\n", 1)[0].decode(errors="replace")


async def _stderr_after_two_connections(liveness: dict[str, int]) -> tuple[str, str]:
    try:
        binary = find_server()
    except Exception:
        pytest.skip("no linch-server binary")
    port = _free_port()
    directory = Path(tempfile.mkdtemp(prefix="lv-ka-"))
    config = {
        "http": f"127.0.0.1:{port}",
        "liveness": liveness,
        "tenants": {"acme": {"tokens": [{"value": TOKEN}], "allow": {}}},
    }
    path = directory / "server.json"
    path.write_text(json.dumps(config))
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--config",
        str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=directory,
    )
    assert proc.stderr is not None
    try:
        line = await asyncio.wait_for(proc.stderr.readline(), 30)
        if b"listening" not in line:  # refused at startup: exit status 2, reason on stderr
            rest = (await proc.stderr.read()).decode(errors="replace")
            return f"exit {await proc.wait()}", line.decode(errors="replace") + rest
        status = ""
        for _ in range(2):
            status = await asyncio.to_thread(_upgrade, port)
            await asyncio.sleep(0.2)
        proc.kill()
        rest = (await proc.stderr.read()).decode(errors="replace")
        return status, rest
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def test_whole_second_keepalive_times_are_set_without_a_warning() -> None:
    status, stderr = await _stderr_after_two_connections(
        {"keepalive_idle_ms": 1500, "keepalive_interval_ms": 1500, "keepalive_probes": 2}
    )
    assert " 101 " in status, status
    assert "cannot set TCP keepalive" not in stderr, stderr


async def test_sub_second_keepalive_times_do_not_leave_connections_without_keepalive() -> None:
    # Either the config is refused at startup (status 2, reason on stderr) or the times are
    # rounded up to a whole second and keepalive is set. Today: a warning per connection.
    status, stderr = await _stderr_after_two_connections(
        {"keepalive_idle_ms": 1000, "keepalive_interval_ms": 500, "keepalive_probes": 2}
    )
    assert status.startswith("exit 2") or " 101 " in status, status
    assert "cannot set TCP keepalive" not in stderr, stderr
