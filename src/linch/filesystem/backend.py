"""Virtual filesystem backends for offloading large tool results.

A ``FileBackend`` is a small async key/value store keyed by path.  It is *not*
the real disk — built-in :class:`~linch.tools.builtin.ReadTool` etc. already
cover real ``cwd`` files.  This virtual filesystem is sandboxed, session-tied,
and serializable, mirroring the Deep Agents ``StateBackend`` / ``StoreBackend``
split:

* :class:`StateFileBackend` — ephemeral, in-memory, typically one per session.
* :class:`CompositeFileBackend` — routes paths by prefix (e.g. ``/memories/``
  to a persistent backend, everything else to an ephemeral one).

Persistent backends (see :mod:`linch.filesystem.sqlite`) implement the same
protocol, so they compose freely under :class:`CompositeFileBackend`.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol, runtime_checkable


def normalize_path(path: str) -> str:
    """Normalize a virtual path to a canonical ``/``-rooted form.

    Collapses duplicate slashes, strips trailing slashes (except root), and
    ensures a single leading slash.  ``"a//b/"`` → ``"/a/b"``; ``""`` → ``"/"``.
    """
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts) if parts else "/"


def _slice_lines(text: str, offset: int, limit: int | None) -> str:
    """Apply a 1-indexed *offset* / *limit* line window to *text*.

    Mirrors the semantics of :class:`~linch.tools.builtin.ReadTool`: an
    ``offset`` of ``0`` or ``1`` starts at the first line; ``limit=None`` (or
    ``<= 0``) returns to the end.
    """
    if (offset is None or offset <= 1) and not limit:
        return text
    lines = text.split("\n")
    start = max(0, offset - 1) if offset and offset > 0 else 0
    end = start + limit if limit and limit > 0 else len(lines)
    return "\n".join(lines[start:end])


@runtime_checkable
class FileBackend(Protocol):
    """Async virtual filesystem protocol.

    All methods take canonical paths (callers should pass through
    :func:`normalize_path`); implementations normalize defensively too.
    """

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        """Return file contents, optionally windowed by 1-indexed line range.

        Raises :class:`FileNotFoundError` when *path* does not exist.
        """
        ...

    async def write(self, path: str, content: str) -> None:
        """Create or overwrite *path* with *content*."""
        ...

    async def ls(self, prefix: str = "") -> list[str]:
        """Return sorted paths, optionally restricted to those under *prefix*."""
        ...

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        """Replace *old* with *new* in *path*; return the replacement count.

        Raises :class:`FileNotFoundError` if *path* is missing and
        :class:`ValueError` if *old* is absent (or not unique when
        ``replace_all`` is ``False``).
        """
        ...

    async def exists(self, path: str) -> bool:
        """Return whether *path* exists."""
        ...

    async def delete(self, path: str) -> None:
        """Remove *path* if present (no error when absent)."""
        ...


class StateFileBackend:
    """Ephemeral in-memory :class:`FileBackend`.

    Holds files in a plain dict.  Cheap to create per session; nothing is
    persisted across sessions.  This is the default backend used for
    result-offload scratch space.
    """

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self._files: dict[str, str] = {}
        if files:
            for path, content in files.items():
                self._files[normalize_path(path)] = content

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        path = normalize_path(path)
        if path not in self._files:
            raise FileNotFoundError(path)
        return _slice_lines(self._files[path], offset, limit)

    async def write(self, path: str, content: str) -> None:
        self._files[normalize_path(path)] = content

    async def ls(self, prefix: str = "") -> list[str]:
        if not prefix:
            return sorted(self._files)
        pfx = normalize_path(prefix)
        # Match the prefix as a path segment boundary, not a raw substring.
        return sorted(p for p in self._files if p == pfx or p.startswith(pfx.rstrip("/") + "/"))

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        path = normalize_path(path)
        if path not in self._files:
            raise FileNotFoundError(path)
        text = self._files[path]
        count = text.count(old)
        if count == 0:
            raise ValueError(f"old string not found in {path}")
        if count > 1 and not replace_all:
            raise ValueError(
                f"old string is not unique in {path} ({count} matches); "
                "pass replace_all=true or include more context"
            )
        self._files[path] = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        return count if replace_all else 1

    async def exists(self, path: str) -> bool:
        return normalize_path(path) in self._files

    async def delete(self, path: str) -> None:
        self._files.pop(normalize_path(path), None)


class CompositeFileBackend:
    """Route paths to different backends by longest-matching prefix.

    Example::

        CompositeFileBackend(
            default=StateFileBackend(),
            routes={"/memories/": SqliteFileBackend("memories.db")},
        )

    Files written under ``/memories/`` land in the persistent backend and
    survive across sessions; everything else stays in the ephemeral default.
    """

    def __init__(
        self, *, default: FileBackend, routes: dict[str, FileBackend] | None = None
    ) -> None:
        self._default = default
        # Normalize route prefixes once; keep trailing slash semantics.
        self._routes: dict[str, FileBackend] = {}
        for prefix, backend in (routes or {}).items():
            norm = normalize_path(prefix)
            self._routes[norm if norm == "/" else norm + "/"] = backend

    def _route(self, path: str) -> tuple[FileBackend, str]:
        """Return ``(backend, path)`` — longest matching route wins."""
        path = normalize_path(path)
        best_prefix = ""
        best_backend: FileBackend | None = None
        for prefix, backend in self._routes.items():
            if (path + "/").startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix = prefix
                best_backend = backend
        return (best_backend, path) if best_backend is not None else (self._default, path)

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        backend, p = self._route(path)
        return await backend.read(p, offset=offset, limit=limit)

    async def write(self, path: str, content: str) -> None:
        backend, p = self._route(path)
        await backend.write(p, content)

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        backend, p = self._route(path)
        return await backend.edit(p, old, new, replace_all=replace_all)

    async def exists(self, path: str) -> bool:
        backend, p = self._route(path)
        return await backend.exists(p)

    async def delete(self, path: str) -> None:
        backend, p = self._route(path)
        await backend.delete(p)

    async def ls(self, prefix: str = "") -> list[str]:
        # Union across the default and every routed backend, deduped and sorted.
        seen: set[str] = set()
        backends: list[FileBackend] = [self._default, *self._routes.values()]
        for backend in backends:
            try:
                for p in await backend.ls(prefix):
                    seen.add(p)
            except Exception:
                # A backend that cannot list (e.g. transient store error) must
                # not break listing of the others.
                continue
        return sorted(seen)

    async def aclose(self) -> None:
        seen: set[int] = set()
        for backend in [self._default, *self._routes.values()]:
            if id(backend) in seen:
                continue
            seen.add(id(backend))
            closer = getattr(backend, "aclose", None) or getattr(backend, "close", None)
            if closer is None:
                continue
            result = closer()
            if inspect.isawaitable(result):
                await result

    def close(self) -> None:
        seen: set[int] = set()
        for backend in [self._default, *self._routes.values()]:
            if id(backend) in seen:
                continue
            seen.add(id(backend))
            closer = getattr(backend, "close", None)
            if closer is None:
                continue
            closer()


def resolve_filesystem_backend(value: Any) -> FileBackend | None:
    """Best-effort coercion of a user-supplied value into a ``FileBackend``.

    Accepts a backend instance directly, or pulls a ``filesystem`` attribute /
    key off a ``deps`` object.  Returns ``None`` when nothing usable is found.
    """
    if value is None:
        return None
    if isinstance(value, FileBackend):
        return value
    candidate = None
    if isinstance(value, dict):
        candidate = value.get("filesystem")
    else:
        candidate = getattr(value, "filesystem", None)
    return candidate if isinstance(candidate, FileBackend) else None
