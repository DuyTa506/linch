"""A `remote` execution backend: the commands run in this process.

`SubprocessHost` runs them with `asyncio.create_subprocess_exec`; the server
only sends `exec.start`, `exec.read` and `exec.kill`.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from scripted import config, text_turn, tool_turn

import linch_rust
from linch_rust import (
    Agent,
    AgentConfig,
    ExecStart,
    Principal,
    RemoteExecution,
    Server,
    SubprocessHost,
    ToolCallEndEvent,
)

REMOTE: RemoteExecution = {"backend": "remote"}
CORE = Path(__file__).resolve().parents[2] / "crates/linch-tools/src/exec"


def agent_config(root: Path, turns: list[dict[str, Any]], **extra: Any) -> AgentConfig:
    return config(turns, mode="skip_dangerous", workspace=str(root), execution=REMOTE, **extra)


def result_text(end: ToolCallEndEvent) -> str:
    return "".join(part.get("text", "") for part in end.result["content"])


async def bash_end(
    root: Path, command: str, host: SubprocessHost | None = None, **extra: Any
) -> ToolCallEndEvent:
    turns = [tool_turn("c1", "Bash", {"command": command, **extra}), text_turn("done")]
    async with Server() as server:
        agent = await server.open_agent(
            agent_config(root, turns), execution=host or SubprocessHost()
        )
        run = await agent.run("s1", "go")
        [end] = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]
    return end


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def pid_in(path: Path) -> int:
    """The pid a command wrote to `path`, once it did."""
    deadline = time.monotonic() + 10
    while not (path.exists() and path.read_text().strip()):
        assert time.monotonic() < deadline, f"{path} was never written"
        await asyncio.sleep(0.05)
    return int(path.read_text())


async def gone(pid: int, what: str) -> None:
    deadline = time.monotonic() + 10
    while alive(pid):
        assert time.monotonic() < deadline, f"process {pid} outlived {what}"
        await asyncio.sleep(0.05)


class Recording(SubprocessHost):
    """A host that keeps each `ExecStart` it served."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[ExecStart] = []

    async def start(self, request: ExecStart) -> None:
        self.starts.append(request)
        await super().start(request)


async def test_the_host_gets_the_principal_of_the_run_that_made_each_command(
    tmp_path: Path,
) -> None:
    ada: Principal = {"tenant": "t", "user": "ada", "roles": ["support"]}
    bob: Principal = {"tenant": "t", "user": "bob"}
    call = tool_turn("c1", "Bash", {"command": "true"})
    turns = [call, text_turn("a"), call, text_turn("b"), call, text_turn("c")]
    host = Recording()
    async with Server() as server:
        agent = await server.open_agent(agent_config(tmp_path, turns), execution=host)
        for session, principal in (("s1", ada), ("s2", bob), ("s3", None)):
            run = await agent.run(session, "go", principal=principal)
            [_ async for _ in run]
    assert [start.principal for start in host.starts] == [
        {"tenant": "t", "user": "ada", "roles": ["support"]},
        {"tenant": "t", "user": "bob", "roles": []},
        None,
    ]
    assert [start.session_id for start in host.starts] == ["s1", "s2", "s3"]


@pytest.mark.source
def test_the_typed_policy_names_every_field_the_server_reads() -> None:
    body = (CORE / "remote.rs").read_text().split("pub struct RemotePolicy {", 1)[1]
    fields = set(re.findall(r"^\s*pub (\w+):", body.split("\n}", 1)[0], re.MULTILINE))
    typed = RemoteExecution.__required_keys__ | RemoteExecution.__optional_keys__
    assert fields | {"backend"} == typed


async def test_a_command_runs_in_this_process_with_its_output_and_exit_code(
    tmp_path: Path,
) -> None:
    end = await bash_end(tmp_path, "echo out; echo err >&2; pwd; exit 3", SubprocessHost(cwd="/"))
    assert end.is_error and end.error_code == "exit_status"
    assert result_text(end) == "out\n/\n[stderr]\nerr\n[exit code 3]"


async def test_the_command_gets_the_hosts_environment_not_the_servers(tmp_path: Path) -> None:
    host = SubprocessHost(env={"FROM_HOST": "yes"})
    end = await bash_end(tmp_path, "echo $FROM_HOST $HOME; env | grep -c _API_KEY", host)
    text = result_text(end)
    assert text.startswith(f"yes {os.environ.get('HOME', '')}\n0"), text


async def test_a_large_output_arrives_in_pieces_and_is_capped_by_the_server(
    tmp_path: Path,
) -> None:
    end = await bash_end(tmp_path, "head -c 1000000 /dev/zero | tr '\\0' x")
    assert "bytes omitted" in result_text(end)
    assert not end.is_error


async def test_a_timeout_kills_the_command_in_this_process(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    command = f"echo $$ > {pidfile}; sleep 300"
    end = await bash_end(tmp_path, command, timeout_ms=700)
    assert end.error_code == "timeout"
    assert "timed out after 700 ms" in result_text(end)
    await gone(await pid_in(pidfile), "its timeout")


async def test_cancelling_the_run_kills_the_command(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    turns = [tool_turn("c1", "Bash", {"command": f"echo $$ > {pidfile}; sleep 300"})]
    async with Server() as server:
        agent = await server.open_agent(
            agent_config(tmp_path, turns + [text_turn("x")]), execution=SubprocessHost()
        )
        run = await agent.run("s1", "go")
        pid = await pid_in(pidfile)
        assert alive(pid)
        assert await run.cancel()
        await gone(pid, "its cancel")


async def test_closing_the_session_kills_a_background_command(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    command = f"echo $$ > {pidfile}; sleep 300"
    turns = [tool_turn("c1", "Bash", {"command": command, "run_in_background": True})]
    async with Server() as server:
        agent = await server.open_agent(
            agent_config(tmp_path, [*turns, text_turn("started")]), execution=SubprocessHost()
        )
        run = await agent.run("s1", "start it")
        [end] = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]
        assert end.metadata is not None and "pid" not in end.metadata
        pid = await pid_in(pidfile)
        assert alive(pid)
        await agent.close_session("s1")
        await gone(pid, "its session")


async def test_a_command_does_not_outlive_the_server(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    turns = [tool_turn("c1", "Bash", {"command": f"echo $$ > {pidfile}; sleep 300"})]
    server = Server()
    await server.start()
    agent = await server.open_agent(
        agent_config(tmp_path, [*turns, text_turn("x")]), execution=SubprocessHost()
    )
    await agent.run("s1", "go")
    pid = await pid_in(pidfile)
    # The server dies without a word; the connection's end kills the command.
    os.kill(server.pid, signal.SIGKILL)
    await gone(pid, "its server")
    await server.close()


async def test_the_file_tools_are_left_out_unless_the_workspace_is_shared(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("hi")
    read = {"file_path": str(tmp_path / "a.txt")}
    async with Server() as server:
        for execution, has_read in [(REMOTE, False), ({**REMOTE, "shared_workspace": True}, True)]:
            turns = [tool_turn("c1", "Read", read), text_turn("done")]
            cfg = config(turns, mode="skip_dangerous", workspace=str(tmp_path), execution=execution)
            agent: Agent = await server.open_agent(cfg, execution=SubprocessHost())
            run = await agent.run("s1", "go")
            [end] = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]
            assert (not end.is_error) is has_read, result_text(end)


async def test_a_remote_backend_needs_a_host(tmp_path: Path) -> None:
    async with Server() as server:
        with pytest.raises(ValueError, match="execution"):
            await server.open_agent(agent_config(tmp_path, [text_turn("x")]))


async def test_one_host_serves_two_connections_without_mixing_their_commands(
    tmp_path: Path,
) -> None:
    """Each server numbers its agents and commands from 1: the host tells them apart."""
    host = SubprocessHost()
    ends: dict[str, ToolCallEndEvent] = {}

    async def run_on(name: str, command: str) -> None:
        turns = [tool_turn("c1", "Bash", {"command": command}), text_turn("done")]
        async with Server() as server:
            agent = await server.open_agent(agent_config(tmp_path, turns), execution=host)
            run = await agent.run("s1", "go")
            [ends[name]] = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]

    await asyncio.gather(
        run_on("a", "sleep 1; echo first"),
        run_on("b", "sleep 1; echo second"),
    )
    assert result_text(ends["a"]) == "first\n[exit code 0]"
    assert result_text(ends["b"]) == "second\n[exit code 0]"


async def test_closing_one_connection_leaves_the_commands_of_another(tmp_path: Path) -> None:
    host = SubprocessHost()
    pids = [tmp_path / "pid-a", tmp_path / "pid-b"]
    turns = [
        [tool_turn("c1", "Bash", {"command": f"echo $$ > {pidfile}; sleep 300"}), text_turn("x")]
        for pidfile in pids
    ]
    async with Server() as keeper:
        kept = await keeper.open_agent(agent_config(tmp_path, turns[1]), execution=host)
        await kept.run("s1", "go")
        async with Server() as leaver:
            left = await leaver.open_agent(agent_config(tmp_path, turns[0]), execution=host)
            await left.run("s1", "go")
            gone_pid, kept_pid = await pid_in(pids[0]), await pid_in(pids[1])
            assert alive(gone_pid) and alive(kept_pid)
        await gone(gone_pid, "its connection")
        assert alive(kept_pid)
        host.close(next(iter(host._commands))[0])
        await gone(kept_pid, "its host")


async def test_a_dropped_connection_reaps_a_background_command_and_a_reconnect_sees_it_gone(
    tmp_path: Path,
) -> None:
    pidfile = tmp_path / "pid"
    command = f"echo $$ > {pidfile}; sleep 300"
    turns = [tool_turn("c1", "Bash", {"command": command, "run_in_background": True})]
    host = SubprocessHost()
    server = Server()
    await server.start()
    agent = await server.open_agent(
        agent_config(tmp_path, [*turns, text_turn("started")]), execution=host
    )
    run = await agent.run("s1", "start it")
    [end] = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]
    assert end.metadata is not None
    process_id = end.metadata["process_id"]
    pid = await pid_in(pidfile)
    assert alive(pid)
    # The connection drops with the command still running in the background.
    os.kill(server.pid, signal.SIGKILL)
    await gone(pid, "its connection")
    await server.close()
    assert not host._commands

    # A reconnect is a new server: it does not know the process, and says so.
    again = [tool_turn("c2", "BashOutput", {"process_id": process_id}), text_turn("x")]
    async with Server() as server:
        agent = await server.open_agent(agent_config(tmp_path, again), execution=host)
        run = await agent.run("s1", "look")
        [end] = [e.event async for e in run if isinstance(e.event, ToolCallEndEvent)]
    assert end.is_error, result_text(end)
    assert end.error_code == "no_such_process"
    assert "the server restarted" in result_text(end)


# A client process that starts a background command through `SubprocessHost`
# and then waits to be killed.
CLIENT = """
import asyncio, sys
from linch_rust import ExecStart, SubprocessHost

async def main(command: str) -> None:
    host = SubprocessHost()
    await host.start(ExecStart("a", "e1", command, None, {}, True, "s1"))
    print("started", flush=True)
    await asyncio.sleep(300)

asyncio.run(main(sys.argv[1]))
"""


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def test_a_command_does_not_outlive_a_killed_client(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    command = f"echo $$ > {pidfile}; sleep 300"
    # The package is on pytest's path, not on the child's: hand it over.
    source = str(Path(linch_rust.__file__).parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([source, os.environ.get("PYTHONPATH", "")])}
    client = subprocess.Popen(
        [sys.executable, "-c", CLIENT, command], stdout=subprocess.PIPE, text=True, env=env
    )
    pid = 0
    try:
        assert client.stdout is not None and client.stdout.readline().strip() == "started"
        pid = await pid_in(pidfile)
        assert group_alive(pid)
        # The client dies without a word: no destructor, no `finally`.
        client.kill()
        client.wait()
        deadline = time.monotonic() + 5
        while group_alive(pid):
            assert time.monotonic() < deadline, f"command group {pid} outlived its client"
            await asyncio.sleep(0.05)
    finally:
        client.kill()
        if pid and group_alive(pid):
            os.killpg(pid, signal.SIGKILL)
