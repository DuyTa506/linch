"""Feature 6: connection liveness (#165) over the WebSocket listener.

Usage: python scenario_live.py [short|default|blocked]...
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import h

from linch_rust import ConnectionClosed, Server
from linch_rust.scripted import MODEL, text_turn

TOKEN = "tok-du-live-0001"
HERE = Path(__file__).resolve().parent
PY = os.environ.get("E2E_PYTHON", sys.executable)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def upgrade(port: int) -> tuple[socket.socket, str]:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(
        (
            f"GET /rpc HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {TOKEN}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    return s, head.split(b"\r\n", 1)[0].decode(errors="replace")


async def server(liveness: dict[str, int] | None, port: int) -> h.Srv:
    allow = h.tenant(None, urls=["http://127.0.0.1:1/v1"], max_connections=1)
    conf: dict[str, Any] = {
        "http": f"127.0.0.1:{port}",
        "tenants": {"acme": {"tokens": [{"value": TOKEN}], "allow": allow}},
    }
    if liveness:
        conf["liveness"] = liveness
    s = h.Srv(conf)
    return await s.start()


async def slot_free_after(port: int, t_stop: float, limit: float) -> float | None:
    """Seconds from t_stop until a new connection is accepted (101) instead of 429."""
    while time.time() - t_stop < limit:
        probe, status = await asyncio.to_thread(upgrade, port)
        probe.close()
        if " 101 " in status:
            return round(time.time() - t_stop, 2)
        await asyncio.sleep(0.1)
    return None


async def silent(liveness: dict[str, int] | None, label: str, limit: float) -> dict[str, Any]:
    out: dict[str, Any] = {"label": label, "liveness": liveness, "load": h.load()}
    port = free_port()
    srv = await server(liveness, port)
    try:
        # (a) a raw peer that finishes the upgrade and then never reads or answers
        peer, status = await asyncio.to_thread(upgrade, port)
        out["raw_upgrade"] = status
        _, again = await asyncio.to_thread(upgrade, port)
        out["second_while_held"] = again  # 429: the slot is taken
        t0 = time.time()
        out["raw_silent_slot_free_s"] = await slot_free_after(port, t0, limit)
        peer.close()
        await asyncio.sleep(0.5)
        # (b) a real WebSocket client process, frozen with SIGSTOP
        proc = subprocess.Popen(
            [
                PY,
                "-c",
                "import asyncio,sys\n"
                "from websockets.asyncio.client import connect\n"
                "async def m():\n"
                f"    async with connect('ws://127.0.0.1:{port}/rpc',"
                f" additional_headers={{'Authorization':'Bearer {TOKEN}'}},"
                " ping_interval=None) as ws:\n"
                "        print('up', flush=True)\n"
                "        await asyncio.sleep(3600)\n"
                "asyncio.run(m())\n",
            ],
            stdout=subprocess.PIPE,
        )
        assert proc.stdout is not None
        proc.stdout.readline()
        await asyncio.sleep(1.0)
        _, held = await asyncio.to_thread(upgrade, port)
        out["second_while_client_alive"] = held
        t1 = time.time()
        os.kill(proc.pid, signal.SIGSTOP)
        try:
            out["sigstop_client_slot_free_s"] = await slot_free_after(port, t1, limit)
        finally:
            os.kill(proc.pid, signal.SIGCONT)
            proc.kill()
        out["server_stderr"] = srv.log[-300:]
    finally:
        await srv.kill()
    return out


async def answering(liveness: dict[str, int], hold_s: float) -> dict[str, Any]:
    out: dict[str, Any] = {"liveness": liveness, "hold_s": hold_s}
    port = free_port()
    srv = await server(liveness, port)
    try:
        async with Server.connect(f"ws://127.0.0.1:{port}/rpc", token=TOKEN) as client:
            await asyncio.sleep(hold_s)
            _, other = await asyncio.to_thread(upgrade, port)
            out["slot_still_taken"] = other
            agent = await client.open_agent(
                {
                    "namespace": "acme",
                    "spec": {"model": MODEL, "max_turns": 2},
                    "provider": {"id": "scripted", "turns": [text_turn("hi")]},
                }
            )
            res = await (await agent.run("s", "hello")).result()
            out["call_after_hold"] = getattr(res, "subtype", repr(res))
    except Exception as error:
        out["error"] = repr(error)
    finally:
        await srv.kill()
    return out


async def blocked(liveness: dict[str, int], block_s: float, threaded: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"liveness": liveness, "block_s": block_s, "threaded": threaded}
    port = free_port()
    srv = await server(liveness, port)
    try:
        async with Server.connect(f"ws://127.0.0.1:{port}/rpc", token=TOKEN) as client:
            t0 = time.time()
            if threaded:
                await asyncio.to_thread(time.sleep, block_s)
            else:
                time.sleep(block_s)  # a synchronous host tool on the event loop
            out["blocked_for_s"] = round(time.time() - t0, 2)
            try:
                agent = await client.open_agent(
                    {
                        "namespace": "acme",
                        "spec": {"model": MODEL, "max_turns": 2},
                        "provider": {"id": "scripted", "turns": [text_turn("hi")]},
                    }
                )
                res = await (await agent.run("s", "hello")).result()
                out["next_call"] = f"ok {getattr(res, 'subtype', None)}"
            except ConnectionClosed as error:
                out["next_call"] = f"ConnectionClosed: {error}"
    except Exception as error:
        out["error"] = repr(error)
    finally:
        await srv.kill()
    return out


async def main() -> None:
    which = sys.argv[1:] or ["short", "blocked"]
    short = {
        "ping_interval_ms": 500,
        "pong_timeout_ms": 2000,
        "keepalive_idle_secs": 1,
        "keepalive_interval_secs": 1,
        "keepalive_probes": 2,
    }
    res: dict[str, Any] = {}
    if "short" in which:
        res["silent_short"] = [await silent(short, f"rep{i}", 20) for i in range(3)]
        h.log("silent_short", res["silent_short"])
        res["answering"] = await answering(short, 6.5)
        h.log("answering", res["answering"])
    if "blocked" in which:
        res["blocked"] = [
            await blocked(short, 3.5, False),
            await blocked(short, 3.5, True),
            await blocked(short, 1.0, False),
        ]
        h.log("blocked", res["blocked"])
    if "default" in which:
        res["silent_default"] = await silent(None, "default liveness", 100)
        h.log("silent_default", res["silent_default"])
    h.dump("liveness_" + "_".join(which) + ".json", res)


if __name__ == "__main__":
    asyncio.run(main())
