"""Background `Bash`: a command outlives its run, not its session or server."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from scripted import config, text_turn, tool_turn

from linch_rust import Agent, Server, ToolCallEndEvent

START = {"command": "sleep 300", "run_in_background": True}


async def start(agent: Agent) -> tuple[str, int]:
    """Run a turn that starts `sleep 300` in the background; its id and pid."""
    run = await agent.run("s1", "Start it")
    [end] = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]
    assert end.metadata is not None
    return end.metadata["process_id"], end.metadata["pid"]


def alive(pid: int) -> bool:
    """Whether `pid` is in the process table, zombies included."""
    return Path(f"/proc/{pid}").exists()


async def gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while alive(pid):
        assert time.monotonic() < deadline, f"process {pid} outlived its server"
        await asyncio.sleep(0.05)


def agent_config(root: Path, turns: list[dict[str, object]]) -> object:
    return config(turns, mode="skip_dangerous", workspace=str(root))


async def test_a_process_belongs_to_its_agent_and_ends_with_the_server(tmp_path: Path) -> None:
    turns = [tool_turn("c1", "Bash", START), text_turn("ok")]
    async with Server() as server:
        agent = await server.open_agent(agent_config(tmp_path, turns))
        process_id, pid = await start(agent)
        assert alive(pid)

        later = [
            tool_turn("c2", "BashOutput", {"process_id": process_id}),
            tool_turn("c3", "BashKill", {"process_id": process_id}),
            text_turn("stopped"),
        ]
        other = await server.open_agent(agent_config(tmp_path, later))
        # Another agent has its own registry: the id names nothing there.
        run = await other.run("s1", "Check it")
        ends = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]
        assert [end.error_code for end in ends] == ["no_such_process", "no_such_process"]
        assert alive(pid)
    await gone(pid)


async def test_a_killed_server_takes_its_background_commands_along(tmp_path: Path) -> None:
    async with Server() as server:
        agent = await server.open_agent(agent_config(tmp_path, [tool_turn("c1", "Bash", START)]))
        _, pid = await start(agent)
        assert alive(pid)
        os.kill(server.pid, signal.SIGKILL)
        await gone(pid)


async def test_closing_the_session_ends_its_background_commands(tmp_path: Path) -> None:
    turns = [tool_turn("c1", "Bash", START), text_turn("ok")]
    async with Server() as server:
        agent = await server.open_agent(agent_config(tmp_path, turns))
        _, pid = await start(agent)
        await agent.close_session("s1")
        await gone(pid)
