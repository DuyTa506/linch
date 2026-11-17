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
# The most of one stream the host keeps for a read to take. Past it the pipe
# is not read, so the operating system blocks the command until a read frees
# room, and a command like `yes` cannot fill this process's memory.
_BUFFER_CAP = 4 * 1024 * 1024
# The most of one stream a kill answers with (its last bytes): a kill answer
# stays well below the server's line limit, whatever the command printed.
_KILL_LIMIT = _READ_LIMIT
# How long a kill waits for the shell to be reaped, and for its pipes to drain.
_REAP_TIMEOUT = 5.0
_DRAIN_TIMEOUT = 1.0
# How long a live command may go without a read before the host kills it: the
# server polls every second, so only a gone or stuck server stops reading.
_IDLE_TIMEOUT = 120.0
# The host's own identity variables: the server's are not sent.
_HOST_ENV = ("PATH", "HOME", "USER", "SHELL", "TMPDIR")


@dataclass(frozen=True)
class ExecStart:
    """One command the server asks the host to start (`exec.start`).

    Attributes:
        agent: Names the agent the command belongs to, unique across the
            connections one host serves (it carries the connection's tag).
        exec_id: Names the command in later reads and kills.
        command: The shell command, for `/bin/sh -c`.
        cwd: The directory it starts in; `None` when the policy names none,
            which leaves the choice to the host.
        env: The variables the server allows the command (`LANG` and the
            like), never its own `PATH`, `HOME`, `USER`, `SHELL` or `TMPDIR`.
        background: Whether the model does not wait for it.
        session_id: The session that owns the call.
        principal: Who the run that made the call acts for (`tenant`, `user`
            and `roles`, `roles` empty when it has none): the `principal` of
            its `run`. `None` when the run has none. Runs of several
            principals on one agent share this host, so a host that serves
            more than one must scope or deny the command by it (a working
            directory, an account or a container per principal);
            `SubprocessHost` ignores it and runs every command the same way.
    """

    agent: str
    exec_id: str
    command: str
    cwd: str | None
    env: Mapping[str, str]
    background: bool
    session_id: str
    principal: Mapping[str, Any] | None = None


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
    all. The `agent` of every call names the agent and its connection, so
    one host may serve several connections.
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
        Without the exit (the command is not known to be dead) the server
        reports the call as interrupted, not as killed.
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
    server that asked for it; so does a command nobody reads for
    `idle_timeout` seconds. At most 4 MiB of each stream wait for a read: past
    that the command blocks on its pipe until the server reads.

    A command runs as this process's user, so it can read this process's
    environment through `/proc/<pid>/environ`: keep secrets out of it.

    Args:
        cwd: Where a command starts when the server names no directory;
            this process's working directory when absent.
        env: Variables added to each command's environment, after the host's
            own `PATH`, `HOME`, `USER`, `SHELL` and `TMPDIR` and before the
            ones the server allows.
        idle_timeout: Seconds a live command may go without a read before it
            is killed.
    """

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        idle_timeout: float = _IDLE_TIMEOUT,
    ) -> None:
        self._cwd = None if cwd is None else os.fspath(cwd)
        self._env = dict(env or {})
        self._idle_timeout = idle_timeout
        self._commands: dict[tuple[str, str], _Command] = {}
        # Commands no longer served whose tasks still run; held so they finish.
        self._retired: set[_Command] = set()

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
        key = (request.agent, request.exec_id)
        command = _Command(proc)
        self._commands[key] = command
        command.deadman = asyncio.create_task(self._deadman(key, command))

    async def read(self, agent: str, exec_id: str, wait: float) -> ExecChunk:
        """Wait for output or the end, then take up to 256 KiB of each stream."""
        command = self._command(agent, exec_id)
        command.reading += 1
        try:
            if not command.ready():
                command.changed.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(command.changed.wait(), wait)
            chunk = command.take(_READ_LIMIT)
        finally:
            command.reading -= 1
            command.touch()
        if chunk.exit is not None:
            self._forget((agent, exec_id), command)
        return chunk

    async def kill(self, agent: str, exec_id: str) -> ExecChunk:
        """Kill the command's group, wait for it, and return what is left.

        The answer has the exit only when the shell was reaped, and at most
        the last 256 KiB of each stream.
        """
        command = self._command(agent, exec_id)
        chunk = await command.kill()
        if chunk.exit is not None:
            self._forget((agent, exec_id), command)
        return chunk

    def close(self, agent: str) -> None:
        """Kill every command of `agent` at once."""
        for key in [key for key in self._commands if key[0] == agent]:
            command = self._commands[key]
            command.abandon()
            self._forget(key, command)

    def _command(self, agent: str, exec_id: str) -> _Command:
        try:
            return self._commands[agent, exec_id]
        except KeyError:
            raise LookupError(f"no command {exec_id!r}: it ended or was killed") from None

    def _forget(self, key: tuple[str, str], command: _Command) -> None:
        """Stop serving `command`; it is held until its tasks end."""
        if self._commands.get(key) is not command:
            return
        del self._commands[key]
        if command.deadman is not None and command.deadman is not asyncio.current_task():
            command.deadman.cancel()
        if not command.finished.done():
            self._retired.add(command)
            command.finished.add_done_callback(lambda _: self._retired.discard(command))

    async def _deadman(self, key: tuple[str, str], command: _Command) -> None:
        """Kill `command` once nothing has read it for `idle_timeout` seconds."""
        loop = asyncio.get_running_loop()
        while True:
            idle = 0.0 if command.reading else loop.time() - command.touched
            if idle >= self._idle_timeout:
                break
            await asyncio.sleep(self._idle_timeout - idle)
        command.abandon()
        self._forget(key, command)


@dataclass(eq=False)
class _Command:
    """One running command: its pipes pumped into buffers, and how it ended."""

    proc: asyncio.subprocess.Process
    out: bytearray = field(default_factory=bytearray)
    err: bytearray = field(default_factory=bytearray)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    space: asyncio.Event = field(default_factory=asyncio.Event)
    exit: dict[str, int] | None = None
    # Reads in progress, and when the last one ended (or the command began).
    reading: int = 0
    touched: float = 0.0
    # Pumps waiting for room in a full buffer; no one reads the command now,
    # so a full buffer is dropped instead of waited for.
    stalled: int = 0
    discard: bool = False
    deadman: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self._decoders = (
            codecs.getincrementaldecoder("utf-8")("replace"),
            codecs.getincrementaldecoder("utf-8")("replace"),
        )
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self.touch()
        self._pumps = [
            asyncio.create_task(self._pump(self.proc.stdout, self.out)),
            asyncio.create_task(self._pump(self.proc.stderr, self.err)),
        ]
        self.finished = asyncio.create_task(self._finish())

    def touch(self) -> None:
        """Note that the server just used this command."""
        self.touched = asyncio.get_running_loop().time()

    async def _pump(self, stream: asyncio.StreamReader, into: bytearray) -> None:
        while True:
            # A full buffer stops the reading: the pipe fills and the command waits.
            while len(into) >= _BUFFER_CAP and not self.discard:
                self.stalled += 1
                try:
                    self.space.clear()
                    await self.space.wait()
                finally:
                    self.stalled -= 1
            data = await stream.read(64 * 1024)
            if not data:
                return
            if self.discard and len(into) >= _BUFFER_CAP:
                continue
            into.extend(data)
            self.changed.set()

    async def _finish(self) -> None:
        code = await self.proc.wait()
        # What the shell left running goes with it, which also closes the pipes.
        self.kill_now()
        # A pump that waits for room has more to read: it is not slow, the
        # server is, so the exit waits for it.
        while True:
            _, pending = await asyncio.wait(self._pumps, timeout=_DRAIN_TIMEOUT)
            if not pending or not self.stalled:
                break
        self.exit = {"signal": -code} if code < 0 else {"code": code}
        self.changed.set()

    def ready(self) -> bool:
        """Whether a read has something to take now."""
        return bool(self.out or self.err or self.exit is not None)

    def take(self, limit: int) -> ExecChunk:
        """Take up to `limit` bytes of each stream; the exit once both are empty."""
        texts: list[str] = []
        for buffer, decoder in zip((self.out, self.err), self._decoders, strict=True):
            data = bytes(buffer[:limit])
            del buffer[: len(data)]
            texts.append(decoder.decode(data, final=self.exit is not None and not buffer))
        self.space.set()
        drained = not self.out and not self.err
        return ExecChunk(texts[0], texts[1], self.exit if drained else None)

    def take_tail(self, limit: int) -> ExecChunk:
        """Take the last `limit` bytes of each stream, drop the rest, and add the exit."""
        texts: list[str] = []
        for buffer, decoder in zip((self.out, self.err), self._decoders, strict=True):
            if len(buffer) > limit:
                decoder.reset()
            data = bytes(buffer[-limit:])
            buffer.clear()
            texts.append(decoder.decode(data, final=self.exit is not None))
        self.space.set()
        return ExecChunk(texts[0], texts[1], self.exit)

    def kill_now(self) -> None:
        """SIGKILL the command's process group, if there is one."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)

    def abandon(self) -> None:
        """Kill the group and stop waiting for anyone to read what it printed."""
        self.discard = True
        self.space.set()
        self.kill_now()

    async def kill(self) -> ExecChunk:
        """Kill the group, wait until the shell is reaped, and take its last output.

        The exit is in the answer only if the shell was reaped in time.
        """
        self.abandon()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self.finished), _REAP_TIMEOUT)
        return self.take_tail(_KILL_LIMIT)
