"""`SubprocessHost` on its own: bounded buffers, kill answers and the deadman timer.

No server is involved; the tests call the host as the server's requests do.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator

import pytest

from linch_rust import ExecStart, SubprocessHost
from linch_rust import exec_host as host_module


def request(command: str, exec_id: str = "exec-1", agent: str = "c1/agent-1") -> ExecStart:
    return ExecStart(agent, exec_id, command, None, {}, False, "s1")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def pid_of(host: SubprocessHost, exec_id: str = "exec-1", agent: str = "c1/agent-1") -> int:
    return host._commands[agent, exec_id].proc.pid


async def until(check: object, what: str) -> None:
    assert callable(check)
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.02)


@pytest.fixture
async def host() -> AsyncIterator[SubprocessHost]:
    host = SubprocessHost()
    yield host
    host.close("c1/agent-1")


async def test_a_command_that_prints_without_end_stops_at_the_buffer_cap(
    host: SubprocessHost,
) -> None:
    await host.start(request("yes"))
    command = host._commands["c1/agent-1", "exec-1"]
    await until(lambda: len(command.out) >= host_module._BUFFER_CAP, "the buffer never filled")
    await asyncio.sleep(0.5)
    # One more 64 KiB read may have been in flight when the cap was reached.
    assert len(command.out) <= host_module._BUFFER_CAP + 64 * 1024
    # Reading frees room, and the command goes on.
    chunk = await host.read("c1/agent-1", "exec-1", 1.0)
    assert len(chunk.stdout) == host_module._READ_LIMIT
    await until(lambda: len(command.out) >= host_module._BUFFER_CAP, "it never went on")


async def test_output_past_the_cap_is_delayed_not_lost(host: SubprocessHost) -> None:
    size = 6 * 1024 * 1024
    await host.start(request(f"head -c {size} /dev/zero | tr '\\0' x; echo done >&2; exit 4"))
    command = host._commands["c1/agent-1", "exec-1"]
    # The reader is slow: the pipe is full and the command is blocked on it.
    await until(lambda: len(command.out) >= host_module._BUFFER_CAP, "the buffer never filled")
    await asyncio.sleep(0.3)
    out = err = ""
    exit_: object = None
    while exit_ is None:
        chunk = await host.read("c1/agent-1", "exec-1", 5.0)
        out, err, exit_ = out + chunk.stdout, err + chunk.stderr, chunk.exit
        await asyncio.sleep(0.01)  # the round trip to the server
    assert len(out) == size, len(out)
    assert err == "done\n"
    assert exit_ == {"code": 4}


async def test_a_marker_after_more_than_the_cap_arrives_before_the_exit(
    host: SubprocessHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shell finishes (the rest fits in the pipe) while the pump still waits
    # for room: the exit must wait for the pump, not for the drain timeout.
    size = host_module._BUFFER_CAP + 10 * 1024
    await host.start(request(f"head -c {size} /dev/zero | tr '\\0' x; echo MARKER"))
    command = host._commands["c1/agent-1", "exec-1"]
    await until(lambda: command.stalled > 0, "the pump never waited for room")
    await asyncio.sleep(1.5)
    # One read empties the whole buffer, with the marker still in the pipe.
    monkeypatch.setattr(host_module, "_READ_LIMIT", 2 * host_module._BUFFER_CAP)
    out, exit_ = "", None
    while exit_ is None:
        chunk = await host.read("c1/agent-1", "exec-1", 5.0)
        out, exit_ = out + chunk.stdout, chunk.exit
        await asyncio.sleep(0.01)  # the round trip to the server
    assert out.endswith("MARKER\n"), out[-20:]
    assert exit_ == {"code": 0}


async def test_a_kill_answer_is_capped_and_still_has_the_exit(host: SubprocessHost) -> None:
    await host.start(request("yes"))
    command = host._commands["c1/agent-1", "exec-1"]
    await until(lambda: len(command.out) >= host_module._BUFFER_CAP, "the buffer never filled")
    chunk = await host.kill("c1/agent-1", "exec-1")
    assert len(chunk.stdout) <= host_module._KILL_LIMIT
    assert chunk.exit == {"signal": 9}
    with pytest.raises(LookupError):
        await host.read("c1/agent-1", "exec-1", 0.1)


async def test_a_kill_of_a_shell_that_was_not_reaped_has_no_exit(
    host: SubprocessHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(self: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(host_module._Command, "_finish", never)
    monkeypatch.setattr(host_module, "_REAP_TIMEOUT", 0.2)
    await host.start(request("sleep 300"))
    pid = pid_of(host)
    chunk = await host.kill("c1/agent-1", "exec-1")
    assert chunk.exit is None
    # Not confirmed dead, so the host still knows it (the deadman cleans up).
    assert ("c1/agent-1", "exec-1") in host._commands
    await until(lambda: not alive(pid), "the shell outlived its kill")


async def test_a_command_nobody_reads_is_killed(host: SubprocessHost) -> None:
    host = SubprocessHost(idle_timeout=0.4)
    await host.start(request("sleep 300"))
    pid = pid_of(host)
    assert alive(pid)
    await until(lambda: not alive(pid), "the command outlived its idle timeout")
    with pytest.raises(LookupError):
        await host.read("c1/agent-1", "exec-1", 0.1)


async def test_a_command_that_is_read_is_not_killed() -> None:
    host = SubprocessHost(idle_timeout=1.0)
    await host.start(request("sleep 300"))
    pid = pid_of(host)
    # A read in flight longer than the idle timeout counts as use, and so
    # does each short one after it.
    await host.read("c1/agent-1", "exec-1", 1.5)
    for _ in range(6):
        await host.read("c1/agent-1", "exec-1", 0.1)
    assert alive(pid)
    assert (await host.kill("c1/agent-1", "exec-1")).exit == {"signal": 9}
    await until(lambda: not alive(pid), "the kill did not kill")


async def test_two_agents_with_the_same_handle_on_two_connections_do_not_collide() -> None:
    host = SubprocessHost()
    await host.start(request("sleep 300", agent="c1/agent-1"))
    await host.start(request("sleep 300", agent="c2/agent-1"))
    first, second = pid_of(host, agent="c1/agent-1"), pid_of(host, agent="c2/agent-1")
    assert first != second
    host.close("c1/agent-1")
    await until(lambda: not alive(first), "close did not kill its own command")
    assert alive(second)
    host.close("c2/agent-1")
    await until(lambda: not alive(second), "close did not kill the other command")
