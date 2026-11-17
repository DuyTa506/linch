"""Live: a local stdio MCP server whose tools the model calls; its process dies with the agent."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from live_core_support import (
    PROVIDERS,
    VLLM,
    Live,
    drive,
    live_server,
    make_config,
    retry_once,
    server_env,
    text_of,
)

from linch_rust import InitEvent, Server

pytestmark = pytest.mark.live

SERVER = Path(__file__).with_name("mcp_tiny_server.py")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().split(")")[-1].split()[0] != "Z"


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_the_model_calls_two_mcp_tools_and_the_server_dies_with_the_agent(
    live: Live, tmp_path: Path
) -> None:
    pid_file = tmp_path / f"mcp-{time.monotonic_ns()}.pid"
    config = make_config(
        live,
        live.spec(max_turns=6),
        permissions={"mode": "skip_dangerous", "project_root": "/"},
        mcp_servers=[
            {
                "name": "tiny",
                "transport": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-I", str(SERVER)],
                    "env": {"MCP_PID_FILE": str(pid_file), "MCP_STUBBORN": "1"},
                },
                "call_timeout_ms": 30_000,
            }
        ],
    )
    async with live_server() as server:
        agent = await server.open_agent(config)
        pid = int(pid_file.read_text())
        assert alive(pid)
        run = await agent.run(
            "s1",
            "Call mcp__tiny__add with a=19 and b=23. Then call mcp__tiny__shout with text "
            "'linch'. Then report both results.",
        )
        trace = await drive(agent, run)
        assert alive(pid), "the server must stay up between runs of one agent"
        await agent.close()
        deadline = time.monotonic() + 10
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not alive(pid), "the MCP server process outlived agent.close()"

    [init] = trace.of(InitEvent)
    assert {"mcp__tiny__add", "mcp__tiny__shout"} <= set(init.tools)
    [added] = trace.ends("mcp__tiny__add")
    [shouted] = trace.ends("mcp__tiny__shout")
    assert text_of(added) == "42" and text_of(shouted) == "LINCH"
    final = trace.result.final_text or ""
    assert "42" in final and "LINCH" in final


@pytest.mark.parametrize("live", PROVIDERS[:1])
@retry_once
async def test_the_mcp_server_is_killed_when_the_server_connection_closes(
    live: Live, tmp_path: Path
) -> None:
    pid_file = tmp_path / f"mcp-{time.monotonic_ns()}.pid"
    config = make_config(
        live,
        mcp_servers=[
            {
                "name": "tiny",
                "transport": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-I", str(SERVER)],
                    "env": {"MCP_PID_FILE": str(pid_file), "MCP_STUBBORN": "1"},
                },
            }
        ],
    )
    async with live_server() as server:
        await server.open_agent(config)  # no agent.close(): leave through the connection
        pid = int(pid_file.read_text())
        assert alive(pid)
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(pid), "the MCP server process outlived the linch-server sidecar"


@pytest.mark.xfail(
    strict=True,
    reason="finding: a stdio MCP server that ignores EOF and SIGTERM outlives a SIGKILLed "
    "linch-server (Bash has a lifeline watchdog, MCP stdio has none)",
)
async def test_the_mcp_server_does_not_outlive_a_sigkilled_sidecar(tmp_path: Path) -> None:
    pid_file = tmp_path / f"mcp-{time.monotonic_ns()}.pid"
    config = make_config(
        VLLM,
        mcp_servers=[
            {
                "name": "tiny",
                "transport": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-I", str(SERVER)],
                    "env": {"MCP_PID_FILE": str(pid_file), "MCP_STUBBORN": "1"},
                },
            }
        ],
    )
    server = Server(env=server_env())
    await server.start()
    pid = 0
    try:
        await server.open_agent(config)
        pid = int(pid_file.read_text())
        assert alive(pid)
        os.kill(server.pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        survived = alive(pid)
    finally:
        if pid and alive(pid):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await server.close()
    assert not survived, "the MCP server outlived a SIGKILLed linch-server"
