"""The stdout pipe of a spawned `linch-server` is enlarged after spawn.

The server joins queued events into lines of up to 64 KiB, so with the default 64 KiB pipe a
full batch does not fit even in an empty pipe and the server blocks on every write of a burst.
"""

from __future__ import annotations

import fcntl
import sys
from typing import Any

import pytest

from linch_rust import Server, find_server

PIPE_SIZE = 256 * 1024
F_GETPIPE_SZ = 1032

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="F_SETPIPE_SZ is Linux only")


def _binary() -> str:
    try:
        return str(find_server())
    except FileNotFoundError:
        pytest.skip("no linch-server binary")


def _stdout_fd(server: Server) -> int:
    process = server._process
    assert process is not None and process.stdout is not None
    pipe = process.stdout._transport.get_extra_info("pipe")  # type: ignore[attr-defined]
    return int(pipe.fileno())


async def test_stdout_pipe_holds_a_full_batch() -> None:
    async with Server(binary=_binary()) as server:
        assert fcntl.fcntl(_stdout_fd(server), F_GETPIPE_SZ) >= PIPE_SIZE


async def test_refused_resize_keeps_the_default_and_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    real = fcntl.fcntl

    def refuse(fd: int, op: int, *args: Any) -> Any:
        if op == getattr(fcntl, "F_SETPIPE_SZ", 1031):
            raise PermissionError(1, "Operation not permitted")
        return real(fd, op, *args)

    monkeypatch.setattr(fcntl, "fcntl", refuse)
    async with Server(binary=_binary()) as server:
        assert fcntl.fcntl(_stdout_fd(server), F_GETPIPE_SZ) == 64 * 1024
        assert server.info is not None
