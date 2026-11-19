"""The `server.info` handshake: the first call after connecting.

The client names its own version and learns the server's: the release, the
wire protocol's version, the run contract digest's version, the optional
features it has, the execution backends it implements and its main limits.
A protocol outside `PROTOCOL_MIN` to `PROTOCOL_MAX`, or a server that does
not know the method, ends the connection with `IncompatibleServer` before
anything else is sent.

The server bumps its protocol only for a change an older client would
misread, so a newer server that only adds methods, members or capabilities
is still spoken to. Members and capability names this client does not know
are ignored.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import metadata
from typing import Any

from ._rpc import Connection
from .errors import HandshakeTimeout, IncompatibleServer, ProtocolError

# The protocol versions this client speaks: the server's `PROTOCOL_VERSION`
# in `crates/linch-rpc/src/protocol/info.rs`.
PROTOCOL_MIN = 1
PROTOCOL_MAX = 1
# Generous: a loaded machine is slow, a server that answers does so at once.
HANDSHAKE_TIMEOUT = 15.0
_METHOD_NOT_FOUND = -32601


@dataclass(frozen=True)
class ServerInfo:
    """What a server said about itself in `server.info`.

    The same for every connection to a server, a tenant's included: it holds
    facts of the build, never a tenant's own caps, names or paths.

    Attributes:
        server_version: The server's release.
        protocol: The wire protocol's version.
        contract_version: The run contract digest's version; runs stored
            under another one do not resume.
        capabilities: Names of the optional features the server has, such as
            `cache_isolation` or `exec_idempotency_key`; ask with
            `has`.
        backends: The execution backends the build implements (`local`,
            `sandbox`, `docker`, `remote`). Whether one works on that host,
            or is allowed to a tenant, is decided when an agent opens.
        max_message_bytes: The longest message the server reads.
        default_max_runs: Runs a tenant may drive at once unless its
            allowlists say otherwise.
    """

    server_version: str
    protocol: int
    contract_version: int
    capabilities: frozenset[str]
    backends: tuple[str, ...]
    max_message_bytes: int
    default_max_runs: int

    def has(self, capability: str) -> bool:
        """Whether the server has the optional feature `capability`."""
        return capability in self.capabilities


def _incompatible(
    message: str, protocol: int | None = None, server_version: str | None = None
) -> IncompatibleServer:
    return IncompatibleServer(
        message,
        protocol=protocol,
        server_version=server_version,
        supported=(PROTOCOL_MIN, PROTOCOL_MAX),
    )


def _from_wire(result: dict[str, Any]) -> ServerInfo:
    version = result.get("server_version")
    server_version = version if isinstance(version, str) else None
    protocol = result.get("protocol")
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        raise _incompatible(
            "the server's server.info names no protocol version", None, server_version
        )
    if not PROTOCOL_MIN <= protocol <= PROTOCOL_MAX:
        raise _incompatible(
            f"the server speaks protocol {protocol} (release {server_version or 'unknown'}), "
            f"this client speaks {_range()}: use a client and a server of the same release",
            protocol,
            server_version,
        )
    limits: dict[str, Any] = result.get("limits") or {}
    try:
        return ServerInfo(
            server_version=server_version or "",
            protocol=protocol,
            contract_version=int(result.get("contract_version", 0)),
            capabilities=frozenset(str(name) for name in result.get("capabilities") or ()),
            backends=tuple(str(name) for name in result.get("backends") or ()),
            max_message_bytes=int(limits.get("max_message_bytes", 0)),
            default_max_runs=int(limits.get("default_max_runs", 0)),
        )
    except (TypeError, ValueError, AttributeError) as error:
        raise _incompatible(
            f"the server's server.info is malformed: {error}", protocol, server_version
        ) from None


def _range() -> str:
    if PROTOCOL_MIN == PROTOCOL_MAX:
        return f"protocol {PROTOCOL_MAX}"
    return f"protocols {PROTOCOL_MIN} to {PROTOCOL_MAX}"


def _client_version() -> str | None:
    try:
        return metadata.version("linch-rust")
    except metadata.PackageNotFoundError:
        return None


async def handshake(conn: Connection, *, timeout: float = HANDSHAKE_TIMEOUT) -> ServerInfo:
    """Call `server.info` on `conn` and check the answer.

    Args:
        conn: A connection that has just opened (and logged in, for a
            remote server).
        timeout: Seconds to wait for the answer.

    Returns:
        What the server said about itself.

    Raises:
        IncompatibleServer: The server's protocol is outside the range this
            client speaks, or it has no `server.info`.
        HandshakeTimeout: The server did not answer in time.
        ConnectionClosed: The server went away first.
    """
    params: dict[str, Any] = {"client_protocol": PROTOCOL_MAX}
    version = _client_version()
    if version is not None:
        params["client_version"] = version
    try:
        async with asyncio.timeout(timeout):
            result = await conn.request("server.info", params)
    except TimeoutError:
        raise HandshakeTimeout(
            f"the server did not answer server.info within {timeout:g} s"
        ) from None
    except ProtocolError as error:
        if error.code != _METHOD_NOT_FOUND:
            raise
        raise _incompatible(
            "the server has no server.info: it predates the version handshake, "
            "so upgrade it to the same release as this client"
        ) from None
    return _from_wire(result)
