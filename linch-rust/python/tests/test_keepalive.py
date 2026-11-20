"""The `liveness` section of `linch-server --config`, against the real binary.

The keepalive times are whole seconds, the kernel's unit. A sub-second time in milliseconds
used to be accepted, rounded to 0 by the kernel's `setsockopt` (EINVAL), and logged as "cannot
set TCP keepalive on a connection" for every connection, which then had no keepalive at all.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from pathlib import Path

import pytest

from linch_rust import find_server

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
        seen = b""
        while True:  # the startup lines, up to the first "listening" (or the end of stderr)
            line = await asyncio.wait_for(proc.stderr.readline(), 30)
            seen += line
            if not line or b"listening" in line:
                break
        if b"listening" not in seen:  # refused at startup: exit status 2, reason on stderr
            return f"exit {await asyncio.wait_for(proc.wait(), 30)}", seen.decode(errors="replace")
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
        {"keepalive_idle_secs": 2, "keepalive_interval_secs": 1, "keepalive_probes": 2}
    )
    assert " 101 " in status, status
    assert "cannot set TCP keepalive" not in stderr, stderr


async def test_a_sub_second_keepalive_time_is_refused_at_startup() -> None:
    # The kernel takes whole seconds: a millisecond name is an unknown field, and 0 seconds is
    # refused, both at startup (status 2; the unknown field is not named, the error lists the
    # fields expected), not once per connection.
    for liveness, field in [
        ({"keepalive_idle_ms": 1000, "keepalive_interval_ms": 500}, "unknown field"),
        ({"keepalive_idle_secs": 0}, "liveness.keepalive_idle_secs"),
    ]:
        status, stderr = await _stderr_after_two_connections(liveness)
        assert status.startswith("exit 2"), status
        assert field in stderr, stderr
