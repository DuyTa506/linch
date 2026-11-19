"""`Server`: a `linch-server` child process over stdio, or a running server
reached over its Unix socket or WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import sys
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
from .exec_host import ExecutionHost
from .info import ServerInfo, handshake
from .provider import ModelInfo, ModelList, ProviderCapabilities
from .run import Observer
from .tools import HostTool

if TYPE_CHECKING:
    from .otel import Telemetry

_EXIT_TIMEOUT = 10.0
# The server joins queued events into lines of up to 64 KiB, so the default 64 KiB
# stdout pipe blocks it on a burst; 256 KiB fits several lines.
_STDOUT_PIPE_SIZE = 256 * 1024
_log = logging.getLogger(__name__)


def _enlarge_stdout_pipe(process: asyncio.subprocess.Process) -> None:
    """Raise the child's stdout pipe to 256 KiB on Linux; a refusal keeps the default."""
    if sys.platform != "linux":
        return
    import fcntl

    try:
        stream: Any = process.stdout
        pipe: Any = stream._transport.get_extra_info("pipe")
        fcntl.fcntl(int(pipe.fileno()), fcntl.F_SETPIPE_SZ, _STDOUT_PIPE_SIZE)
    except (OSError, AttributeError) as error:
        _log.debug("keeping the default stdout pipe size: %s", error)


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
        self._info: ServerInfo | None = None

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
        """The tenant a `Server.connect` connection acts for, as the server
        named it: in the socket's login answer, or in the WebSocket upgrade's
        `Linch-Tenant` header.

        `None` for a server spawned here, which has no tenants.
        """
        self._started()
        return self._link.tenant if self._link is not None else None

    @property
    def info(self) -> ServerInfo:
        """What the server said about itself when this client connected: its
        release, protocol and contract versions, capabilities, backends and
        main limits. The same for every tenant."""
        self._started()
        assert self._info is not None
        return self._info

    async def start(self) -> None:
        """Spawn the server, or connect and log in, then ask `server.info`;
        `async with` calls this.

        Raises:
            IncompatibleServer: The server's protocol is outside the range
                this client speaks, or it has no `server.info`. The child
                is stopped, or the connection closed, before this is raised.
            HandshakeTimeout: The server did not answer `server.info` in time.
            Unauthenticated: A remote server does not know the token.
            LimitReached: The tenant holds all its connections, or too many
                connections wait to log in to a remote socket.
            ConnectionClosed: A WebSocket upgrade was refused otherwise.
            OSError: Nothing listens at a remote address.
        """
        if self._conn is not None:
            raise LinchError("the server is already started")
        await self._open()
        try:
            self._info = await handshake(self._started())
        except BaseException:
            # `async with` does not call `close` when `start` raises: free the
            # child or the tenant's connection place here.
            with contextlib.suppress(Exception):
                await self.close()
            self._conn = self._link = self._process = None
            raise

    async def _open(self) -> None:
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
        _enlarge_stdout_pipe(self._process)
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
        execution: ExecutionHost | None = None,
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
            execution: The host that runs the commands of a `remote`
                execution backend, in this process (`SubprocessHost` is a
                reference one). The agent's commands run wherever it runs
                them; it must kill them when `close(agent)` is called.
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
            ValueError: The config's backend is `remote` and no `execution`
                host was given.
        """
        conn = self._started()
        chosen = config.get("execution")
        if execution is None and chosen is not None and chosen["backend"] == "remote":
            raise ValueError("a remote execution backend needs open_agent(execution=...)")
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
            execution,
        )
        skipped = [
            Skipped(str(item["path"]), str(item["reason"]), str(item["detail"]))
            for item in opened.get("skipped", [])
        ]
        observers = _observers(config, telemetry)
        return Agent(conn, handle, str(opened["contract"]), observers, skipped)

    async def provider_capabilities(
        self, provider: Mapping[str, Any] | AgentConfig, model: str | None = None
    ) -> ProviderCapabilities:
        """What a provider supports for a model, before any agent is open.

        Nothing is sent to the provider except a self-hosted server's one
        window probe, and no key is needed. A cloud provider's answer
        comes from the adapter, not from the vendor.

        Args:
            provider: The `provider` of an agent config (`{"id": "vllm",
                "base_url": ...}`), or the whole `AgentConfig`, whose
                `provider` and `spec.model` are then used.
            model: The model asked about; with an `AgentConfig` it defaults
                to the spec's model.

        Returns:
            The capabilities. `context_source` says whether the window is
            configured, from the server, from the adapter's table or a
            default guess.

        Raises:
            NotAllowed: A remote server does not let the tenant ask, or the
                provider's `base_url` is not one of its allowed endpoints.
            ProtocolError: The provider config is invalid.
        """
        found, name = _provider_and_model(provider, model)
        result = await self._started().request(
            "provider.capabilities", {"provider": found, "model": name}
        )
        return ProviderCapabilities.from_wire(result)

    async def list_models(self, provider: Mapping[str, Any] | AgentConfig) -> ModelList:
        """The models a provider's server lists, in its order.

        Asks the provider's API with the server's key, so the key must be
        set (a self-hosted server's is optional).

        Args:
            provider: The `provider` of an agent config, or the whole
                `AgentConfig`.

        Returns:
            One `ModelInfo` per model; a field the API does not give is
            `None`. The list's `truncated` is true when the server followed
            only the first pages of a longer list.

        Raises:
            ModelsUnsupported: The provider has no model list.
            ProviderRequestFailed: The provider could not be reached or
                answered with an error; the message is fixed text per `kind`.
            LimitReached: A tenant asked more than 10 times in a minute.
            OpenError: The provider's key is not set on the server.
            NotAllowed: As for `provider_capabilities`.
        """
        found, _ = _provider_and_model(provider, None)
        result = await self._started().request("provider.models", {"provider": found})
        models = [ModelInfo.from_wire(item) for item in result["models"]]
        return ModelList(models, truncated=bool(result.get("truncated", False)))

    async def model_info(
        self, provider: Mapping[str, Any] | AgentConfig, model: str | None = None
    ) -> ModelInfo:
        """What the provider's server lists about one model.

        Args:
            provider: The `provider` of an agent config, or the whole
                `AgentConfig`.
            model: The model id; with an `AgentConfig` it defaults to the
                spec's model.

        Returns:
            The model's entry.

        Raises:
            ModelNotFound: The list does not hold the model (`truncated` says
                the list was cut, so it may exist past the cut).
            ModelsUnsupported: As for `list_models`.
            ProviderRequestFailed: As for `list_models`.
            OpenError: As for `list_models`.
            NotAllowed: As for `provider_capabilities`.
        """
        found, name = _provider_and_model(provider, model)
        result = await self._started().request("provider.model", {"provider": found, "model": name})
        return ModelInfo.from_wire(result)

    def _started(self) -> Connection:
        if self._conn is None:
            raise LinchError("the server is not started; use `async with Server()`")
        return self._conn


def _provider_and_model(
    provider: Mapping[str, Any] | AgentConfig, model: str | None
) -> tuple[Mapping[str, Any], str]:
    """The provider config and the model of a call that takes either a
    provider or a whole agent config."""
    if "provider" in provider and "spec" in provider:
        spec = provider["spec"]
        found: Mapping[str, Any] = provider["provider"]
        name = model if model is not None else str(spec.get("model", ""))
    else:
        found, name = provider, model if model is not None else ""
    return found, name


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
