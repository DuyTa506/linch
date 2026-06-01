"""Persistent SQLite-backed :class:`~agent_kit.filesystem.backend.FileBackend`.

Mirrors :class:`agent_kit.memory.sqlite.SqliteMemoryStore`: all DB I/O runs on a
dedicated single-thread executor so async callers never block the event loop.
Use under :class:`~agent_kit.filesystem.backend.CompositeFileBackend` to make a
subtree (e.g. ``/memories/``) survive across sessions.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .backend import _slice_lines, normalize_path


class SqliteFileBackend:
    """A virtual filesystem persisted to a SQLite ``files`` table."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path not in (":memory:", ""):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentkit_fs")
        self._conn: sqlite3.Connection = self._executor.submit(self._create_db).result()

    def _create_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    async def _run(self, fn, *args):  # type: ignore[no-untyped-def]
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        path = normalize_path(path)
        row = await self._run(self._sync_get, path)
        if row is None:
            raise FileNotFoundError(path)
        return _slice_lines(row, offset, limit)

    def _sync_get(self, path: str) -> str | None:
        cur = self._conn.execute("SELECT content FROM files WHERE path = ?", (path,))
        row = cur.fetchone()
        return row["content"] if row is not None else None

    async def write(self, path: str, content: str) -> None:
        await self._run(self._sync_write, normalize_path(path), content)

    def _sync_write(self, path: str, content: str) -> None:
        self._conn.execute(
            """
            INSERT INTO files(path, content, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at
            """,
            (path, content, time.time()),
        )
        self._conn.commit()

    async def ls(self, prefix: str = "") -> list[str]:
        return await self._run(self._sync_ls, prefix)

    def _sync_ls(self, prefix: str) -> list[str]:
        if not prefix:
            cur = self._conn.execute("SELECT path FROM files ORDER BY path")
            return [row["path"] for row in cur.fetchall()]
        pfx = normalize_path(prefix)
        cur = self._conn.execute(
            "SELECT path FROM files WHERE path = ? OR path LIKE ? ORDER BY path",
            (pfx, pfx.rstrip("/") + "/%"),
        )
        return [row["path"] for row in cur.fetchall()]

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        return await self._run(self._sync_edit, normalize_path(path), old, new, replace_all)

    def _sync_edit(self, path: str, old: str, new: str, replace_all: bool) -> int:
        text = self._sync_get(path)
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
        self._conn.execute(
            "UPDATE files SET content = ?, updated_at = ? WHERE path = ?",
            (updated, time.time(), path),
        )
        self._conn.commit()
        return count if replace_all else 1

    async def exists(self, path: str) -> bool:
        return await self._run(self._sync_get, normalize_path(path)) is not None

    async def delete(self, path: str) -> None:
        await self._run(self._sync_delete, normalize_path(path))

    def _sync_delete(self, path: str) -> None:
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    def close(self) -> None:
        self._executor.submit(self._conn.close).result()
        self._executor.shutdown(wait=False)

    def __enter__(self) -> SqliteFileBackend:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
