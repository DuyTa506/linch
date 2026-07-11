"""Lock-serialized SQLite executor.

Serializes access to a single SQLite connection behind a regular threading
lock, and runs the blocking work on a bounded *daemon* thread (via
``run_blocking``) so the event loop is never blocked.  Daemon threads avoid the
non-daemon-executor-teardown hang seen in the managed test sandbox, and the
lock preserves the important correctness property: only one operation touches
the connection at a time.

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
from pathlib import Path
from threading import Lock
from typing import TypeVar

T = TypeVar("T")


class SqliteExecutor:
    """Serializes all access to one ``sqlite3`` connection."""

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
        self._lock = Lock()
        self._close_task: asyncio.Task[None] | None = None

    # ── worker-thread internals ──────────────────────────────────────────────

    def _connect(self) -> None:
        """Create and initialise the connection.

        If *init* raises, the connection is closed before re-raising so no
        file handle is leaked.
        """
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            # Wait for a contended write lock instead of failing fast with
            # SQLITE_BUSY — concurrent `BEGIN IMMEDIATE` drains (e.g. two
            # SqliteMailbox connections) then serialize reliably.
            conn.execute("pragma busy_timeout=5000")
            if self._wal and self._path not in (":memory:", ""):
                conn.execute("pragma journal_mode=wal")
                # WAL + NORMAL is the standard durable setting: every commit is
                # still durable across a process/application crash (the resume
                # contract), the database is never corrupted, and only the last
                # transaction(s) can be lost on a hard OS/power failure. FULL's
                # per-commit fsync is the dominant cost of a durable tool run, so
                # NORMAL removes that fsync tax without weakening crash-resume.
                conn.execute("pragma synchronous=NORMAL")
                conn.commit()
            self._init(conn)
            conn.commit()
        except Exception:
            conn.close()
            raise
        self._conn = conn

    def _locked_call(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run *fn(conn)* under the lock; rollback on error to keep the next
        caller from inheriting a half-open transaction on the shared conn."""
        with self._lock:
            if self._closed:
                raise RuntimeError("SqliteExecutor is closed")
            conn = self._conn
            if conn is None:
                self._connect()
                conn = self._conn
            assert conn is not None
            try:
                return fn(conn)
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    # ── async public interface ───────────────────────────────────────────────

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run *fn(conn)* on a bounded daemon thread under the executor lock."""
        if self._closed:
            raise RuntimeError("SqliteExecutor is closed")
        from .._blocking import run_blocking

        def _call() -> T:
            return self._locked_call(fn)

        return await run_blocking(_call)

    async def close(self) -> None:
        """Close without blocking the event loop behind an active DB operation."""
        task = self._close_task
        if task is None:
            if self._closed and self._conn is None:
                return
            # Reject new work before queueing the physical close. An operation
            # already holding `_lock` is allowed to finish first.
            self._closed = True
            from .._blocking import run_blocking

            task = asyncio.create_task(run_blocking(self._close_connection))
            self._close_task = task
        # One caller being cancelled must not cancel the shared physical close.
        await asyncio.shield(task)

    def _close_connection(self) -> None:
        with self._lock:
            conn = self._conn
            if conn is not None:
                conn.close()
                self._conn = None

    def close_sync(self) -> None:
        """Close from a non-async context (``__exit__`` / sync ``close()``)."""
        if self._closed and self._conn is None:
            return
        self._closed = True
        self._close_connection()
