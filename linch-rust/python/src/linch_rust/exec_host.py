"""Remote execution: this process runs the `Bash` commands of an agent.

An agent whose config says `{"backend": "remote"}` sends its commands to the
client that opened it, with the server requests `exec.start`, `exec.read`
and `exec.kill`. Pass an `ExecutionHost` to `Server.open_agent(execution=...)`
to serve them; `SubprocessHost` is a reference host that runs each command
as `/bin/sh -c` in its own process group on this machine.

The commands run wherever the host runs them, with the host's privileges:
the server's sandbox does not apply. The server sends no secret or server
path (its `PATH`, `HOME`, `USER`, `SHELL` and `TMPDIR` are left out), only
the command, the policy's `cwd` and a few variables such as `LANG`.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["ExecChunk", "ExecStart", "ExecutionHost", "SubprocessHost"]

# The most of one stream a read returns, so an answer stays far below the
# server's line limit; the rest comes with the next read.
_READ_LIMIT = 256 * 1024
# How long a kill waits for the shell to be reaped, and for its pipes to drain.
_REAP_TIMEOUT = 5.0
_DRAIN_TIMEOUT = 1.0
# The host's own identity variables: the server's are not sent.
_HOST_ENV = ("PATH", "HOME", "USER", "SHELL", "TMPDIR")


@dataclass(frozen=True)
class ExecStart:
    """One command the server asks the host to start (`exec.start`).

    Attributes:
        agent: The handle of the agent the command belongs to.
        exec_id: Names the command in later reads and kills.
        command: The shell command, for `/bin/sh -c`.
        cwd: The directory it starts in; `None` when the policy names none,
            which leaves the choice to the host.
        env: The variables the server allows the command (`LANG` and the
            like), never its own `PATH`, `HOME`, `USER`, `SHELL` or `TMPDIR`.
        background: Whether the model does not wait for it.
        session_id: The session that owns the call.
    """

    agent: str
    exec_id: str
    command: str
    cwd: str | None
    env: Mapping[str, str]
    background: bool
    session_id: str


@dataclass(frozen=True)
class ExecChunk:
    """What a command printed since the last read, and its end, once.

    Attributes:
        stdout: New standard output, decoded as UTF-8.
        stderr: New standard error, decoded as UTF-8.
        exit: `{"code": n}` or `{"signal": n}`, present once, after all the
            output; the host forgets the command after returning it.
    """

    stdout: str = ""
    stderr: str = ""
    exit: Mapping[str, int] | None = None

    def wire(self) -> dict[str, Any]:
        """The `exec.read` and `exec.kill` result; empty parts are left out."""
        out: dict[str, Any] = {}
        if self.stdout:
            out["stdout"] = self.stdout
        if self.stderr:
            out["stderr"] = self.stderr
        if self.exit is not None:
            out["exit"] = dict(self.exit)
        return out


@runtime_checkable
class ExecutionHost(Protocol):
    """What serves a `remote` backend: start, read and kill commands.

    The server wraps every call in its own timeout, cancels the ones it
    gives up on (the handler's task is cancelled), and treats a missing
    answer as an interrupted call, so a host answers truthfully or not at
    all.
    """

    async def start(self, request: ExecStart) -> None:
        """Start a command; raise to refuse it (the model reads the message)."""
        ...

    async def read(self, agent: str, exec_id: str, wait: float) -> ExecChunk:
        """Wait up to `wait` seconds for output or the end; take what is new.

        Answer at once when there is something to take. Taking must happen
        only when the answer is returned, so a cancelled read loses nothing.
        """
        ...

    async def kill(self, agent: str, exec_id: str) -> ExecChunk:
        """Kill the command and everything it started; answer once it is dead.

        The answer carries what it printed since the last read, and its exit.
        """
        ...

    def close(self, agent: str) -> None:
        """Kill every command of `agent`: its connection or handle is gone."""
        ...


class SubprocessHost:
    """Runs commands as `/bin/sh -c` children of this process.

    Each command leads its own process group and has no stdin. Killing a
    command, or its shell exiting, kills the group. Closing an agent (or
    the connection ending) kills its commands, so nothing outlives the
    server that asked for it.

    Args:
        cwd: Where a command starts when the server names no directory;
            this process's working directory when absent.
        env: Variables added to each command's environment, after the host's
            own `PATH`, `HOME`, `USER`, `SHELL` and `TMPDIR` and before the
            ones the server allows.
    """

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._cwd = None if cwd is None else os.fspath(cwd)
        self._env = dict(env or {})
        self._commands: dict[tuple[str, str], _Command] = {}

    async def start(self, request: ExecStart) -> None:
        """Start `request.command`; a bad directory or shell raises."""
        env = {key: os.environ[key] for key in _HOST_ENV if key in os.environ}
        env.update(self._env)
        env.update(request.env)
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            request.command,
            cwd=request.cwd or self._cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._commands[request.agent, request.exec_id] = _Command(proc)

    async def read(self, agent: str, exec_id: str, wait: float) -> ExecChunk:
        """Wait for output or the end, then take up to 256 KiB of each stream."""
        command = self._command(agent, exec_id)
        if not command.ready():
            command.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(command.changed.wait(), wait)
        chunk = command.take(_READ_LIMIT)
        if chunk.exit is not None:
            del self._commands[agent, exec_id]
        return chunk

    async def kill(self, agent: str, exec_id: str) -> ExecChunk:
        """Kill the command's group, wait for it, and return what is left."""
        command = self._command(agent, exec_id)
        await command.kill()
        del self._commands[agent, exec_id]
        return command.take(None)

    def close(self, agent: str) -> None:
        """Kill every command of `agent` at once."""
        for key in [key for key in self._commands if key[0] == agent]:
            self._commands.pop(key).kill_now()

    def _command(self, agent: str, exec_id: str) -> _Command:
        try:
            return self._commands[agent, exec_id]
        except KeyError:
            raise LookupError(f"no command {exec_id!r}: it ended or was killed") from None


@dataclass
class _Command:
    """One running command: its pipes pumped into buffers, and how it ended."""

    proc: asyncio.subprocess.Process
    out: bytearray = field(default_factory=bytearray)
    err: bytearray = field(default_factory=bytearray)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    exit: dict[str, int] | None = None

    def __post_init__(self) -> None:
        self._decoders = (
            codecs.getincrementaldecoder("utf-8")("replace"),
            codecs.getincrementaldecoder("utf-8")("replace"),
        )
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self._pumps = [
            asyncio.create_task(self._pump(self.proc.stdout, self.out)),
            asyncio.create_task(self._pump(self.proc.stderr, self.err)),
        ]
        self._finished = asyncio.create_task(self._finish())

    async def _pump(self, stream: asyncio.StreamReader, into: bytearray) -> None:
        while data := await stream.read(64 * 1024):
            into.extend(data)
            self.changed.set()

    async def _finish(self) -> None:
        code = await self.proc.wait()
        # What the shell left running goes with it, which also closes the pipes.
        self.kill_now()
        await asyncio.wait(self._pumps, timeout=_DRAIN_TIMEOUT)
        self.exit = {"signal": -code} if code < 0 else {"code": code}
        self.changed.set()

    def ready(self) -> bool:
        """Whether a read has something to take now."""
        return bool(self.out or self.err or self.exit is not None)

    def take(self, limit: int | None) -> ExecChunk:
        """Take up to `limit` bytes of each stream; the exit once both are empty."""
        texts: list[str] = []
        for buffer, decoder in zip((self.out, self.err), self._decoders, strict=True):
            data = bytes(buffer if limit is None else buffer[:limit])
            del buffer[: len(data)]
            texts.append(decoder.decode(data, final=self.exit is not None and not buffer))
        drained = not self.out and not self.err
        return ExecChunk(texts[0], texts[1], self.exit if drained else None)

    def kill_now(self) -> None:
        """SIGKILL the command's process group, if there is one."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)

    async def kill(self) -> None:
        """Kill the group and wait until the shell is reaped and its output drained."""
        self.kill_now()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._finished), _REAP_TIMEOUT)
