"""`Server`: a `linch-server` child process spoken to over stdio."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any

from ._binary import find_server
from ._rpc import Connection
from .agent import Agent
from .callbacks import Callback
from .config import AgentConfig
from .errors import LinchError
from .tools import HostTool

# Server lines carry whole envelopes, tool results included.
_LINE_LIMIT = 64 * 1024 * 1024
_EXIT_TIMEOUT = 10.0


class Server:
    """A `linch-server` sidecar, spawned as a child process.

    Use it as an async context manager. Leaving the block closes the
    server's input: it drops its runs where they stand (a Postgres run stays
    resumable), exits, and is killed if it has not after 10 seconds.

    The server keeps provider keys from the processes it starts (`Bash`, a
    stdio MCP server) and, on Linux, from `/proc/<server pid>/environ`. This
    process is not protected that way: keys in its own environment when it
    started stay readable to any process of the same user, the server's
    children included, through `/proc/<this pid>/environ`, and `env` does
    not change that. When agents run untrusted commands, keep keys out of
    this process's environment (read them from a secret store) and hand
    them to the server in `env`.

    Args:
        binary: Path of the server binary; found by `find_server` when absent
            (the wheel's bundled binary, then `LINCH_SERVER_BIN`, then cargo's
            target dir).
        env: The server's whole environment, where it reads provider keys
            such as `DEEPSEEK_API_KEY`; nothing else is passed on. This
            process's environment when absent.
    """

    def __init__(
        self, binary: str | os.PathLike[str] | None = None, *, env: Mapping[str, str] | None = None
    ) -> None:
        self._binary = binary
        self._env = None if env is None else dict(env)
        self._process: asyncio.subprocess.Process | None = None
        self._conn: Connection | None = None

    async def __aenter__(self) -> Server:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def pid(self) -> int:
        """The server process id."""
        return self._started()[0].pid

    async def start(self) -> None:
        """Spawn the server; `async with` calls this."""
        if self._process is not None:
            raise LinchError("the server is already started")
        binary = self._binary if self._binary is not None else find_server()
        self._process = await asyncio.create_subprocess_exec(
            binary,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=self._env,
            limit=_LINE_LIMIT,
        )
        assert self._process.stdout is not None and self._process.stdin is not None
        self._conn = Connection(self._process.stdout, self._process.stdin)

    async def close(self) -> None:
        """Close the server's input and wait for it to exit; `async with` calls this."""
        process, conn = self._started()
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), _EXIT_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
        await conn.finish()

    async def open_agent(
        self,
        config: AgentConfig,
        tools: Sequence[HostTool] = (),
        callbacks: Sequence[Callback] = (),
    ) -> Agent:
        """Open an agent from its config, with host tools and callbacks.

        Args:
            config: The agent as data: `namespace`, `spec` and `provider`,
                and optionally `stores`, `workspace`, `bash`, `permissions`,
                `mcp_servers`, `skills`, `subagents`, `workers`, `mailbox`,
                `config_dir`, `filesystem` and `tasks` (see `AgentConfig`).
                It never carries a provider key.
            tools: Host tools made with `tool`, run in this process.
            callbacks: Intercept callbacks made with `callback`, answered in
                this process.

        Returns:
            The opened agent.

        Raises:
            OpenError: The config, provider, store or an MCP server failed.
            ProtocolError: The config or a declaration is invalid.
        """
        _, conn = self._started()
        params: dict[str, Any] = dict(config)
        if tools:
            params["tools"] = [host_tool.spec() for host_tool in tools]
        if callbacks:
            params["callbacks"] = [declared.spec() for declared in callbacks]
        opened = await conn.request("agent.open", params)
        handle = str(opened["agent"])
        conn.bind(
            handle,
            {host_tool.name: host_tool for host_tool in tools},
            {(declared.point, declared.policy_id): declared for declared in callbacks},
        )
        return Agent(conn, handle, str(opened["contract"]))

    def _started(self) -> tuple[asyncio.subprocess.Process, Connection]:
        if self._process is None or self._conn is None:
            raise LinchError("the server is not started; use `async with Server()`")
        return self._process, self._conn
