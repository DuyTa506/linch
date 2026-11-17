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


async def test_a_frozen_websocket_client_is_dropped_within_the_pong_timeout() -> None:
    # RED: "ends one that sent nothing at all (no frame, no pong) for `pong_timeout_ms`". The
    # silence is judged only at each ping tick and must be strictly longer than the timeout, so a
    # peer that froze right after a pong is dropped at timeout + one ping interval: 2.57 s for a
    # 2 s timeout with 0.5 s pings (and 89 s for the 60 s default with 30 s pings).
    import os
    import signal
    import subprocess
    import sys
    import time

    try:
        binary = find_server()
    except Exception:
        pytest.skip("no linch-server binary")
    port = _free_port()
    directory = Path(tempfile.mkdtemp(prefix="lv-fz-"))
    config = {
        "http": f"127.0.0.1:{port}",
        "liveness": {"ping_interval_ms": 500, "pong_timeout_ms": 2000},
        "tenants": {"acme": {"tokens": [{"value": TOKEN}], "allow": {"max_connections": 1}}},
    }
    (directory / "server.json").write_text(json.dumps(config))
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--config",
        str(directory / "server.json"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        cwd=directory,
    )
    assert proc.stderr is not None
    client = None
    try:
        assert b"listening" in await asyncio.wait_for(proc.stderr.readline(), 30)
        script = (
            "import asyncio\n"
            "from websockets.asyncio.client import connect\n"
            "async def m():\n"
            f"    async with connect('ws://127.0.0.1:{port}/rpc', additional_headers="
            f"{{'Authorization': 'Bearer {TOKEN}'}}, ping_interval=None):\n"
            "        print('up', flush=True)\n"
            "        await asyncio.sleep(60)\n"
            "asyncio.run(m())\n"
        )
        dropped = []
        # Four freezes at different phases of the 0.5 s ping cycle.
        for trial in range(4):
            client = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
            assert client.stdout is not None
            client.stdout.readline()
            await asyncio.sleep(1.2 + 0.13 * trial)  # a few pings answered
            assert " 429 " in await asyncio.to_thread(_upgrade, port), "the slot is taken"
            os.kill(client.pid, signal.SIGSTOP)
            t0 = time.time()
            freed = None
            while time.time() - t0 < 10:
                if " 101 " in await asyncio.to_thread(_upgrade, port):
                    freed = time.time() - t0
                    break
                await asyncio.sleep(0.05)
            os.kill(client.pid, signal.SIGCONT)
            client.kill()
            assert freed is not None, "never dropped"
            dropped.append(round(freed, 2))
            await asyncio.sleep(0.3)
        assert max(dropped) <= 2.0 + 0.15, f"dropped after {dropped} s for a 2 s pong timeout"
    finally:
        if client is not None:
            os.kill(client.pid, signal.SIGCONT)
            client.kill()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
