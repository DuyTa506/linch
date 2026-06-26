"""Virtual FileBackend extension template.

This is a minimal async virtual filesystem. Replace the dict with your storage
client while preserving path normalization and FileBackend method behavior.
"""

from __future__ import annotations

import asyncio

from linch.filesystem import normalize_path


class TemplateFileBackend:
    """Copyable FileBackend skeleton."""

    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        path = normalize_path(path)
        async with self._lock:
            if path not in self._files:
                raise FileNotFoundError(path)
            text = self._files[path]
        return _slice_lines(text, offset=offset, limit=limit)

    async def write(self, path: str, content: str) -> None:
        async with self._lock:
            self._files[normalize_path(path)] = content

    async def ls(self, prefix: str = "") -> list[str]:
        normalized = normalize_path(prefix) if prefix else ""
        async with self._lock:
            paths = sorted(self._files)
        if not normalized:
            return paths
        return [
            path
            for path in paths
            if path == normalized or path.startswith(normalized.rstrip("/") + "/")
        ]

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        path = normalize_path(path)
        async with self._lock:
            if path not in self._files:
                raise FileNotFoundError(path)
            text = self._files[path]
            count = text.count(old)
            if count == 0:
                raise ValueError(f"old string not found in {path}")
            if count > 1 and not replace_all:
                raise ValueError(f"old string is not unique in {path}")
            self._files[path] = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        return count if replace_all else 1

    async def exists(self, path: str) -> bool:
        async with self._lock:
            return normalize_path(path) in self._files

    async def delete(self, path: str) -> None:
        async with self._lock:
            self._files.pop(normalize_path(path), None)


def _slice_lines(text: str, *, offset: int = 0, limit: int | None = None) -> str:
    if offset <= 1 and not limit:
        return text
    lines = text.split("\n")
    start = max(0, offset - 1)
    end = start + limit if limit and limit > 0 else len(lines)
    return "\n".join(lines[start:end])
