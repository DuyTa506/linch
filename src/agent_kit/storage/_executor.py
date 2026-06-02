"""Single-worker-thread SQLite executor.

Moves all blocking ``sqlite3`` I/O off the asyncio event loop while serialising
all access to a single connection through one dedicated OS thread.

Why a dedicated ``max_workers=1`` ``ThreadPoolExecutor`` rather than
``asyncio.to_thread`` or a shared pool:

* ``asyncio.to_thread`` dispatches to the *default* executor, which may use
  many threads.  A ``sqlite3.Connection`` is NOT safe to share across threads
  (even with ``check_same_thread=False``, concurrent commits race).
* ``max_workers=1`` means exactly one OS thread ever touches the connection.
  No ``asyncio.Lock`` is needed; ``check_same_thread=True`` (the default)
  holds as a free correctness assertion.
* The connection is *created* lazily on the worker thread during the first
  operation, so it is never accessed from any other thread.

Usage::

    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript("create table if not exists ...")

    exec_ = SqliteExecutor(path, init=_init_schema)
    result = await exec_.run(lambda conn: conn.execute("select ...").fetchall())
    await exec_.close()

Both an ``async close()`` and a sync ``close_sync()`` are provided so the
executor can be used from async code (``await close()``) and from sync
``__exit__`` context-managers (``close_sync()``).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


class SqliteExecutor:
    """Pins a ``sqlite3`` connection to a single worker thread.

    All calls to :meth:`run` are dispatched to that thread via
    ``loop.run_in_executor``, so the event loop is never blocked.
    Ops are serialised: only one runs at a time (one worker).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        init: Callable[[sqlite3.Connection], None],
        wal: bool = True,
        thread_name: str = "agentkit-sqlite",
    ) -> None:
        self._path = str(path)
        self._wal = wal
        self._init = init
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=thread_name,
        )

    # ── worker-thread internals ──────────────────────────────────────────────

    def _connect(self) -> None:
        """Run on the worker thread — creates and initialises the connection.

        If *init* raises, the connection is closed before re-raising so no
        file handle is leaked.
        """
        conn = sqlite3.connect(self._path)  # check_same_thread=True — correct here
        conn.row_factory = sqlite3.Row
        try:
            if self._wal and self._path not in (":memory:", ""):
                conn.execute("pragma journal_mode=wal")
                conn.commit()
            self._init(conn)
            conn.commit()
        except Exception:
            conn.close()
            raise
        self._conn = conn

    # ── async public interface ───────────────────────────────────────────────

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Dispatch *fn(conn)* to the worker thread; return its result.

        Uses ``loop.run_in_executor`` for OS-native wakeup — no polling.
        Raises if the executor is already closed.
        """
        if self._closed:
            raise RuntimeError("SqliteExecutor is closed")
        loop = asyncio.get_running_loop()

        def _call() -> T:
            conn = self._conn
            if conn is None:
                self._connect()
                conn = self._conn
            assert conn is not None
            return fn(conn)

        return await loop.run_in_executor(self._executor, _call)

    async def close(self) -> None:
        """Close the connection and shut down the worker thread (async path)."""
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()

        def _do_close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        try:
            await loop.run_in_executor(self._executor, _do_close)
        finally:
            # wait=False: _do_close just finished; the thread is idle and will
            # exit on its own.  Avoids a blocking OS thread-join on the event loop.
            self._executor.shutdown(wait=False, cancel_futures=True)

    def close_sync(self) -> None:
        """Close from a non-async context (``__exit__`` / sync ``close()``)."""
        if self._closed:
            return
        self._closed = True

        def _do_close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        try:
            fut = self._executor.submit(_do_close)
            fut.result(timeout=10.0)
        except Exception:
            pass
        finally:
            # wait=False so a hung or timed-out _do_close does not cause
            # close_sync() to block indefinitely beyond the timeout above.
            self._executor.shutdown(wait=False, cancel_futures=True)
