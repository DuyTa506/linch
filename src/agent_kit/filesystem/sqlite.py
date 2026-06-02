"""Persistent SQLite-backed :class:`~agent_kit.filesystem.backend.FileBackend`.

Use under :class:`~agent_kit.filesystem.backend.CompositeFileBackend` to make a
subtree (e.g. ``/memories/``) survive across sessions.

All I/O runs on a single dedicated worker thread via
:class:`~agent_kit.storage._executor.SqliteExecutor`, so the asyncio event loop
is never blocked.  Safe for concurrent use from multiple coroutines.

Supports both async and sync context-manager protocols::

    async with SqliteFileBackend(path) as fb:
        await fb.write("/note.txt", "hello")

    with SqliteFileBackend(path) as fb:
        ...   # close() called synchronously on __exit__
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..storage._executor import SqliteExecutor
from .backend import _slice_lines, normalize_path


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )


class SqliteFileBackend:
    """A virtual filesystem persisted to a SQLite ``files`` table."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path not in (":memory:", ""):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._exec = SqliteExecutor(self.path, init=_init_schema, wal=True)

    # ── FileBackend protocol ─────────────────────────────────────────────────

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        p = normalize_path(path)
        content: str | None = await self._exec.run(lambda conn: _get(conn, p))
        if content is None:
            raise FileNotFoundError(p)
        return _slice_lines(content, offset, limit)

    async def write(self, path: str, content: str) -> None:
        p = normalize_path(path)
        await self._exec.run(lambda conn: _write(conn, p, content))

    async def ls(self, prefix: str = "") -> list[str]:
        return await self._exec.run(lambda conn: _ls(conn, prefix))

    async def edit(
        self, path: str, old: str, new: str, *, replace_all: bool = False
    ) -> int:
        p = normalize_path(path)
        return await self._exec.run(lambda conn: _edit(conn, p, old, new, replace_all))

    async def exists(self, path: str) -> bool:
        p = normalize_path(path)
        return await self._exec.run(lambda conn: _get(conn, p)) is not None

    async def delete(self, path: str) -> None:
        p = normalize_path(path)
        await self._exec.run(lambda conn: _delete(conn, p))

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Async close — preferred in async contexts."""
        await self._exec.close()

    def close(self) -> None:
        """Sync close — compatible with ``with`` context-manager."""
        self._exec.close_sync()

    def __enter__(self) -> SqliteFileBackend:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> SqliteFileBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


# ── Sync helpers (worker thread) ─────────────────────────────────────────────


def _get(conn: sqlite3.Connection, path: str) -> str | None:
    row = conn.execute("SELECT content FROM files WHERE path = ?", (path,)).fetchone()
    return row["content"] if row is not None else None


def _write(conn: sqlite3.Connection, path: str, content: str) -> None:
    conn.execute(
        """
        INSERT INTO files(path, content, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            content=excluded.content,
            updated_at=excluded.updated_at
        """,
        (path, content, time.time()),
    )
    conn.commit()


def _ls(conn: sqlite3.Connection, prefix: str) -> list[str]:
    if not prefix:
        cur = conn.execute("SELECT path FROM files ORDER BY path")
        return [row["path"] for row in cur.fetchall()]
    pfx = normalize_path(prefix)
    cur = conn.execute(
        "SELECT path FROM files WHERE path = ? OR path LIKE ? ORDER BY path",
        (pfx, pfx.rstrip("/") + "/%"),
    )
    return [row["path"] for row in cur.fetchall()]


def _edit(
    conn: sqlite3.Connection, path: str, old: str, new: str, replace_all: bool
) -> int:
    text = _get(conn, path)
    if text is None:
        raise FileNotFoundError(path)
    count = text.count(old)
    if count == 0:
        raise ValueError(f"old string not found in {path}")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old string is not unique in {path} ({count} matches); "
            "pass replace_all=true or include more context"
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    conn.execute(
        "UPDATE files SET content = ?, updated_at = ? WHERE path = ?",
        (updated, time.time(), path),
    )
    conn.commit()
    return count if replace_all else 1


def _delete(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM files WHERE path = ?", (path,))
    conn.commit()
