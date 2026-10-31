"""Virtual filesystem backends for offloading large tool results.

A ``FileBackend`` is a small async key/value store keyed by path.  It is *not*
the real disk — built-in :class:`~linch.tools.builtin.ReadTool` etc. already
cover real ``cwd`` files.  This virtual filesystem is sandboxed, session-tied,
and serializable, mirroring the Deep Agents ``StateBackend`` / ``StoreBackend``
split:

* :class:`StateFileBackend` — ephemeral, in-memory, typically one per session.
* :class:`CompositeFileBackend` — routes paths by prefix (e.g. ``/memories/``
  to a persistent backend, everything else to an ephemeral one).

Persistent backends (see :mod:`linch.filesystem.postgres`) implement the same
protocol, so they compose freely under :class:`CompositeFileBackend`.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol, runtime_checkable


def normalize_path(path: str) -> str:
    """Normalize a virtual path to a canonical ``/``-rooted form.

    Collapses duplicate slashes, strips trailing slashes (except root), and
    ensures a single leading slash.  ``"a//b/"`` → ``"/a/b"``; ``""`` → ``"/"``.

    This helper does not reject ``..`` segments; backends that expose paths to
    untrusted callers must apply their own traversal policy.

    Args:
        path: Virtual path to normalize.

    Returns:
        Canonical slash-rooted path.

    Raises:
        ValueError: If ``path`` is not a string.
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

        Args:
            path: Virtual path to read.
            offset: 1-indexed line to start at; ``0`` and ``1`` start at the first line.
            limit: Maximum number of lines; non-positive or ``None`` reads to the end.

        Returns:
            The requested file contents, including only the selected line window.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
        """
        ...

    async def write(self, path: str, content: str) -> None:
        """Create or overwrite a virtual file.

        Args:
            path: Virtual path to create or replace.
            content: Complete text to store at ``path``.
        """
        ...

    async def ls(self, prefix: str = "") -> list[str]:
        """List virtual files under an optional path prefix.

        Args:
            prefix: Path prefix used to restrict the listing; an empty prefix lists all files.

        Returns:
            Sorted canonical paths matching ``prefix``.
        """
        ...

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        """Replace *old* with *new* in *path*; return the replacement count.

        Raises :class:`FileNotFoundError` if *path* is missing and
        :class:`ValueError` if *old* is absent (or not unique when
        ``replace_all`` is ``False``).

        Args:
            path: Virtual path whose contents should be edited.
            old: Exact text to find.
            new: Text that replaces each matched occurrence.
            replace_all: Whether to replace every occurrence instead of requiring a unique match.

        Returns:
            Number of replacements made.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            ValueError: If ``old`` is absent, or occurs more than once while
                ``replace_all`` is false.
        """
        ...

    async def exists(self, path: str) -> bool:
        """Check whether a virtual file exists.

        Args:
            path: Virtual path to check.

        Returns:
            ``True`` when ``path`` exists; otherwise ``False``.
        """
        ...

    async def delete(self, path: str) -> None:
        """Remove a virtual file when present.

        Deleting an absent path is a no-op.

        Args:
            path: Virtual path to remove.
        """
        ...


class StateFileBackend:
    """Ephemeral in-memory :class:`FileBackend`.

    Holds files in a plain dict.  Cheap to create per session; nothing is
    persisted across sessions.  This is the default backend used for
    result-offload scratch space.

    Args:
        files: Optional initial mapping of paths to file contents. Paths are normalized
            before storage.
    """

    def __init__(self, files: dict[str, str] | None = None) -> None:
        """Create an in-memory backend with optional initial files.

        Args:
            files: Initial path-to-content mapping. Paths are normalized before
                they are stored.
        """
        self._files: dict[str, str] = {}
        if files:
            for path, content in files.items():
                self._files[normalize_path(path)] = content

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        """Read a file, optionally returning a line window.

        Args:
            path: Virtual path to read.
            offset: 1-indexed line to start at; ``0`` and ``1`` start at the first line.
            limit: Maximum number of lines; non-positive or ``None`` reads to the end.

        Returns:
            The selected file contents.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
        """
        path = normalize_path(path)
        if path not in self._files:
            raise FileNotFoundError(path)
        return _slice_lines(self._files[path], offset, limit)

    async def write(self, path: str, content: str) -> None:
        """Create or overwrite an in-memory file.

        Args:
            path: Virtual path to create or replace.
            content: Complete text to store.
        """
        self._files[normalize_path(path)] = content

    async def ls(self, prefix: str = "") -> list[str]:
        """List sorted in-memory file paths.

        Args:
            prefix: Optional path prefix; an empty prefix lists every file.

        Returns:
            Sorted normalized paths matching ``prefix``.
        """
        if not prefix:
            return sorted(self._files)
        pfx = normalize_path(prefix)
        # Match the prefix as a path segment boundary, not a raw substring.
        return sorted(p for p in self._files if p == pfx or p.startswith(pfx.rstrip("/") + "/"))

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        """Replace exact text in an in-memory file.

        Args:
            path: Virtual path whose contents should be edited.
            old: Exact text to find.
            new: Replacement text.
            replace_all: Whether to replace every occurrence instead of requiring a unique match.

        Returns:
            Number of replacements made.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            ValueError: If ``old`` is absent or is not unique when ``replace_all`` is false.
        """
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
        """Check whether an in-memory file exists.

        Args:
            path: Virtual path to check.

        Returns:
            ``True`` if the normalized path is stored; otherwise ``False``.
        """
        return normalize_path(path) in self._files

    async def delete(self, path: str) -> None:
        """Delete an in-memory file if present.

        Args:
            path: Virtual path to remove.
        """
        self._files.pop(normalize_path(path), None)


class ScopedFileBackend:
    """Expose only ``root`` of a shared backend, using scope-relative paths.

    Absolute virtual paths are rooted inside the scope. Traversal segments and
    null bytes are rejected before reaching the inner backend. The host owns
    the inner backend's lifetime; closing a run must not close a shared store.

    Args:
        inner: Backend that stores the scope's mapped paths.
        root: Virtual root under which all mapped paths are stored.
    """

    def __init__(self, inner: FileBackend, root: str) -> None:
        """Create a scoped view over an existing backend.

        Args:
            inner: Backend that stores the mapped paths.
            root: Virtual root under which this view is confined.
        """
        self._inner = inner
        self._root = self._normalize(root)

    @staticmethod
    def _normalize(path: str) -> str:
        if not isinstance(path, str) or "\x00" in path:
            raise ValueError("path must be a string without null bytes")
        parts = path.replace("\\", "/").split("/")
        if ".." in parts:
            raise ValueError("path may not contain '..'")
        return "/" + "/".join(part for part in parts if part and part != ".")

    def _map(self, path: str) -> str:
        return self._root.rstrip("/") + self._normalize(path)

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        """Read a scope-relative path from the inner backend.

        Args:
            path: Scope-relative virtual path.
            offset: 1-indexed line to start at; ``0`` and ``1`` start at the first line.
            limit: Maximum number of lines; non-positive or ``None`` reads to the end.

        Returns:
            The selected file contents.

        Raises:
            FileNotFoundError: If the mapped path does not exist.
            ValueError: If ``path`` contains traversal segments or a null byte.
        """
        return await self._inner.read(self._map(path), offset=offset, limit=limit)

    async def write(self, path: str, content: str) -> None:
        """Write a scope-relative path through the inner backend.

        Args:
            path: Scope-relative virtual path.
            content: Complete text to store.

        Raises:
            ValueError: If ``path`` contains traversal segments or a null byte.
        """
        await self._inner.write(self._map(path), content)

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        """Edit a scope-relative path through the inner backend.

        Args:
            path: Scope-relative virtual path whose contents should be edited.
            old: Exact text to find.
            new: Replacement text.
            replace_all: Whether to replace every occurrence instead of requiring a unique match.

        Returns:
            Number of replacements made.

        Raises:
            FileNotFoundError: If the mapped path does not exist.
            ValueError: If the path is unsafe or the requested text replacement is invalid.
        """
        return await self._inner.edit(self._map(path), old, new, replace_all=replace_all)

    async def exists(self, path: str) -> bool:
        """Check whether a scope-relative path exists.

        Args:
            path: Scope-relative virtual path to check.

        Returns:
            ``True`` when the mapped path exists; otherwise ``False``.

        Raises:
            ValueError: If ``path`` contains traversal segments or a null byte.
        """
        return await self._inner.exists(self._map(path))

    async def delete(self, path: str) -> None:
        """Delete a scope-relative path if present.

        Args:
            path: Scope-relative virtual path to remove.

        Raises:
            ValueError: If ``path`` contains traversal segments or a null byte.
        """
        await self._inner.delete(self._map(path))

    async def ls(self, prefix: str = "") -> list[str]:
        """List paths below a scope-relative prefix.

        Args:
            prefix: Scope-relative prefix; an empty prefix lists the whole scope.

        Returns:
            Sorted paths relative to this backend's root.

        Raises:
            ValueError: If ``prefix`` contains traversal segments or a null byte.
        """
        mapped = self._map(prefix).rstrip("/") or "/"
        paths: set[str] = set()
        root = self._root.rstrip("/")
        for path in await self._inner.ls(mapped):
            try:
                path = self._normalize(path)
            except ValueError:
                continue
            if root and path != root and not path.startswith(root + "/"):
                continue
            if path == mapped or path.startswith(mapped.rstrip("/") + "/"):
                paths.add(path[len(root) :] or "/")
        return sorted(paths)


class CompositeFileBackend:
    """Route paths to different backends by longest-matching prefix.

    Example::

        CompositeFileBackend(
            default=StateFileBackend(),
            routes={"/memories/": PostgresFileBackend("postgresql://user:pw@host/db")},
        )

    Files written under ``/memories/`` land in the persistent backend and
    survive across sessions; everything else stays in the ephemeral default.

    Args:
        default: Backend used when no configured route matches a path.
        routes: Optional mapping of path prefixes to backends. The longest matching
            normalized prefix wins.
    """

    def __init__(
        self, *, default: FileBackend, routes: dict[str, FileBackend] | None = None
    ) -> None:
        """Create a longest-prefix routing filesystem.

        Args:
            default: Backend used for paths that match no route.
            routes: Optional mapping of virtual path prefixes to backends. Prefixes
                are normalized once and the longest match wins.
        """
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
        """Read a path from its longest-matching backend.

        Args:
            path: Virtual path to read.
            offset: 1-indexed line to start at; ``0`` and ``1`` start at the first line.
            limit: Maximum number of lines; non-positive or ``None`` reads to the end.

        Returns:
            The selected file contents.

        Raises:
            FileNotFoundError: If the selected backend does not contain ``path``.
        """
        backend, p = self._route(path)
        return await backend.read(p, offset=offset, limit=limit)

    async def write(self, path: str, content: str) -> None:
        """Write a path to its longest-matching backend.

        Args:
            path: Virtual path to create or replace.
            content: Complete text to store.
        """
        backend, p = self._route(path)
        await backend.write(p, content)

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        """Edit a path in its longest-matching backend.

        Args:
            path: Virtual path whose contents should be edited.
            old: Exact text to find.
            new: Replacement text.
            replace_all: Whether to replace every occurrence instead of requiring a unique match.

        Returns:
            Number of replacements made by the selected backend.

        Raises:
            FileNotFoundError: If the selected backend does not contain ``path``.
            ValueError: If the selected backend rejects the replacement.
        """
        backend, p = self._route(path)
        return await backend.edit(p, old, new, replace_all=replace_all)

    async def exists(self, path: str) -> bool:
        """Check whether a path exists in its selected backend.

        Args:
            path: Virtual path to check.

        Returns:
            ``True`` when the selected backend contains ``path``; otherwise ``False``.
        """
        backend, p = self._route(path)
        return await backend.exists(p)

    async def delete(self, path: str) -> None:
        """Delete a path from its selected backend if present.

        Args:
            path: Virtual path to remove.
        """
        backend, p = self._route(path)
        await backend.delete(p)

    async def ls(self, prefix: str = "") -> list[str]:
        """List and merge paths from every configured backend.

        Args:
            prefix: Optional path prefix passed to each backend.

        Returns:
            Sorted, de-duplicated paths. A backend that raises while listing is
            omitted from the result.
        """
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
        """Asynchronously close each distinct child backend that supports closing.

        Child instances shared by multiple routes are closed once per invocation.
        Backends without ``aclose`` or ``close`` are left untouched.
        """
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
        """Synchronously close each distinct child backend with ``close``.

        Async-only closers are not awaited by this method. Child instances shared
        by multiple routes are closed once per invocation.
        """
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

    Args:
        value: Candidate backend, dependency mapping, or object exposing a
            ``filesystem`` attribute.

    Returns:
        A runtime-checkable :class:`FileBackend`, or ``None`` when no usable
        backend is present.
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
