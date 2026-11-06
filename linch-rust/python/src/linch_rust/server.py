"""`Server`: a `linch-server` child process over stdio, or a running server
reached over its Unix socket or WebSocket."""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any

from ._binary import find_server
from ._remote import LINE_LIMIT, Link, check_address, open_link
from ._rpc import Connection, StreamTransport
from .agent import Agent, Skipped
from .callbacks import Callback
from .config import AgentConfig
from .errors import LinchError
from .run import Observer
from .tools import HostTool

if TYPE_CHECKING:
    from .otel import Telemetry

_EXIT_TIMEOUT = 10.0


class Server:
    """A `linch-server` sidecar, spawned as a child process, or one running
    elsewhere and reached with `Server.connect`.

    Use it as an async context manager. Leaving the block closes the
    server's input: it drops this client's runs where they stand (a
    Postgres run stays resumable). A child process then exits, and is
    killed if it has not after 10 seconds.

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
            (`LINCH_SERVER_BIN`, then the wheel's bundled binary, then cargo's
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
        self._remote: tuple[str, str] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._link: Link | None = None
        self._conn: Connection | None = None

    @classmethod
    def connect(cls, address: str, *, token: str) -> Server:
        """A server already running with `linch-server --config`, reached at `address`.

        Use it as `Server()` is: `async with Server.connect(...) as server:`
        logs in, and leaving the block closes the connection. The connection
        acts for the token's tenant, so every `open_agent` passes the
        tenant's allowlists; host tools, callbacks, approvals and the event
        stream work as over stdio. The server owns provider keys: they are
        read from its environment, never sent from here.

        Args:
            address: Where the server listens, as it prints at startup:
                `unix:/run/linch/linch.sock` for its Unix socket, or
                `ws://127.0.0.1:8731/rpc` for its WebSocket (`wss://` behind
                TLS). A WebSocket needs the `websocket` extra.
            token: A bearer token of the server's config; it is sent as
                `connection.auth` on the socket and as `Authorization:
                Bearer` on the WebSocket upgrade, and never logged.

        Returns:
            The server, not yet connected.

        Raises:
            ValueError: The address is not `unix:`, `ws://` or `wss://`.
        """
        check_address(address)
        server = cls()
        server._remote = (address, token)
        return server

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
        """The server process id, for a server this client spawned."""
        self._started()
        if self._process is None:
            raise LinchError("a server reached with Server.connect has no process here")
        return self._process.pid

    @property
    def tenant(self) -> str | None:
        """The tenant a `Server.connect` socket connection acts for, as the
        server named it at login.

        `None` over a WebSocket, whose upgrade does not name it, and for a
        server spawned here, which has no tenants.
        """
        self._started()
        return self._link.tenant if self._link is not None else None

    async def start(self) -> None:
        """Spawn the server, or connect and log in; `async with` calls this.

        Raises:
            Unauthenticated: A remote server does not know the token.
            LimitReached: The tenant holds all its connections, or too many
                connections wait to log in to a remote socket.
            ConnectionClosed: A WebSocket upgrade was refused otherwise.
            OSError: Nothing listens at a remote address.
        """
        if self._conn is not None:
            raise LinchError("the server is already started")
        if self._remote is not None:
            self._link = await open_link(*self._remote)
            self._conn = self._link.connection
            return
        binary = self._binary if self._binary is not None else find_server()
        self._process = await asyncio.create_subprocess_exec(
            binary,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=self._env,
            limit=LINE_LIMIT,
        )
        assert self._process.stdout is not None and self._process.stdin is not None
        self._conn = Connection(StreamTransport(self._process.stdout, self._process.stdin))

    async def close(self) -> None:
        """Close the server's input and wait for it; `async with` calls this.

        A child process is waited for until it exits; a remote connection
        until the server closed its end, or 10 seconds.
        """
        conn = self._started()
        if self._link is not None:
            await self._link.close()
            return
        process = self._process
        assert process is not None
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
        *,
        telemetry: Telemetry | None = None,
    ) -> Agent:
        """Open an agent from its config, with host tools and callbacks.

        Args:
            config: The agent as data: `namespace`, `spec` and `provider`,
                and optionally `stores`, `workspace`, `bash`, `permissions`,
                `mcp_servers`, `skills`, `subagents`, `workers`, `mailbox`,
                `config_dir`, `filesystem`, `tasks`, `deep_agent` and
                `profile` (see `AgentConfig`). It never carries a provider
                key.
            tools: Host tools made with `tool`, run in this process.
            callbacks: Intercept callbacks made with `callback`, answered in
                this process.
            telemetry: The OpenTelemetry providers its runs are traced and
                measured with (see `linch_rust.otel`); the global ones when
                absent and the `otel` extra is installed, none otherwise.

        Returns:
            The opened agent; `Agent.skipped` lists the skill and subagent
            definitions it left out.

        Raises:
            OpenError: The config, provider, store or an MCP server failed.
            ProtocolError: The config or a declaration is invalid.
            NotAllowed: A remote server's allowlists refused the config.
        """
        conn = self._started()
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
        skipped = [
            Skipped(str(item["path"]), str(item["reason"]), str(item["detail"]))
            for item in opened.get("skipped", [])
        ]
        observers = _observers(config, telemetry)
        return Agent(conn, handle, str(opened["contract"]), observers, skipped)

    def _started(self) -> Connection:
        if self._conn is None:
            raise LinchError("the server is not started; use `async with Server()`")
        return self._conn


def _observers(
    config: AgentConfig, telemetry: Telemetry | None
) -> Callable[[], Observer | None] | None:
    """Make each run's tracer, preset with the config's provider and model."""
    if telemetry is None:
        try:
            from .otel import Telemetry
        except ImportError:  # the `otel` extra is not installed
            return None
        telemetry = Telemetry()
    provider = config["provider"].get("id")
    model = config["spec"].get("model")
    return functools.partial(
        telemetry.observer,
        provider=provider if isinstance(provider, str) else None,
        model=model if isinstance(model, str) else None,
    )
