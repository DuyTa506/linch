"""Reaching a running `linch-server --config` over its Unix socket or WebSocket.

The socket carries one message per line, as stdio does, and its first
message is `connection.auth` with the token; the WebSocket carries one
message per text frame, and the token goes in the upgrade's
`Authorization: Bearer` header. The WebSocket needs the `websocket` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol

from ._rpc import Connection, StreamTransport
from .errors import ConnectionClosed, LimitReached, Unauthenticated

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

# Server messages carry whole envelopes, tool results included.
LINE_LIMIT = 64 * 1024 * 1024
_CLOSE_TIMEOUT = 10.0
_SCHEMES = ("unix:", "ws://", "wss://")


def check_address(address: str) -> None:
    """Refuse an address that is not `unix:PATH`, `ws://...` or `wss://...`.

    Raises:
        ValueError: The address names another scheme.
    """
    if not address.startswith(_SCHEMES) or address in _SCHEMES:
        raise ValueError(
            f"a server address is unix:PATH, ws://HOST:PORT/rpc or wss://..., got {address!r}"
        )


class Link(Protocol):
    """One open connection to a remote server, and how it closes."""

    @property
    def connection(self) -> Connection:
        """The JSON-RPC connection over it."""
        ...

    async def close(self) -> None:
        """End the connection; the server drops its runs where they stand."""
        ...


async def open_link(address: str, token: str) -> Link:
    """Connect to `address` and present `token`.

    Raises:
        Unauthenticated: The server does not know the token.
        LimitReached: The tenant holds all its connections, or too many
            connections wait to log in.
        ConnectionClosed: The WebSocket upgrade failed otherwise.
        OSError: Nothing listens at the address.
    """
    check_address(address)
    if address.startswith("unix:"):
        return await _socket(address.removeprefix("unix:"), token)
    return await _websocket(address, token)


class _SocketLink:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connection = Connection(StreamTransport(reader, writer))
        self._writer = writer

    async def close(self) -> None:
        # Half-close, as closing a child's stdin: the server finishes what
        # it read, writes its last answers and closes its end.
        with contextlib.suppress(OSError):
            self._writer.write_eof()
        finished = asyncio.ensure_future(self.connection.finish())
        await asyncio.wait([finished], timeout=_CLOSE_TIMEOUT)
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()
        await finished


async def _socket(path: str, token: str) -> Link:
    reader, writer = await asyncio.open_unix_connection(path, limit=LINE_LIMIT)
    link = _SocketLink(reader, writer)
    try:
        await link.connection.request("connection.auth", {"token": token})
    except BaseException:
        await link.close()
        raise
    return link


class _WebSocketTransport:
    """One message per text frame."""

    def __init__(self, socket: ClientConnection) -> None:
        self._socket = socket

    async def receive(self) -> bytes | str | None:
        from websockets.exceptions import ConnectionClosed as Closed

        try:
            # Undecoded: orjson parses the UTF-8 bytes directly.
            return await self._socket.recv(decode=False)
        except Closed:
            return None

    async def send(self, message: str) -> None:
        from websockets.exceptions import ConnectionClosed as Closed

        try:
            await self._socket.send(message)
        except Closed as error:
            raise ConnectionClosed(f"writing to the server failed: {error}") from error


class _WebSocketLink:
    def __init__(self, socket: ClientConnection) -> None:
        self.connection = Connection(_WebSocketTransport(socket))
        self._socket = socket

    async def close(self) -> None:
        await self._socket.close()
        await self.connection.finish()


async def _websocket(url: str, token: str) -> Link:
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidHandshake, InvalidStatus
    except ImportError as error:
        raise ImportError(
            "Server.connect over WebSocket needs the websocket extra: "
            "pip install 'linch-rust[websocket]'"
        ) from error
    try:
        socket = await connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            # The server speaks no compression, and its messages can be large.
            compression=None,
            max_size=LINE_LIMIT,
        )
    except InvalidStatus as error:
        status = error.response.status_code
        if status == 401:
            raise Unauthenticated(-32003, "the server refused the token (HTTP 401)") from None
        if status == 429:
            body = error.response.body.decode(errors="replace") if error.response.body else ""
            message = body or "the tenant holds all its connections (HTTP 429)"
            raise LimitReached(-32005, message, {"kind": "connections"}) from None
        raise ConnectionClosed(f"the server refused the WebSocket upgrade: HTTP {status}") from None
    except InvalidHandshake as error:
        raise ConnectionClosed(f"the WebSocket upgrade failed: {error}") from None
    return _WebSocketLink(socket)
