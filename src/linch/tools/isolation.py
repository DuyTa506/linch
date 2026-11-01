"""Filesystem isolation seam for parallel agent branches.

Parallel subagents share one real ``cwd``; concurrent edits to the same relative
path collide. :class:`IsolationBackend` is the seam that gives each branch its
own working directory. The SDK ships only the protocol and a trivial scratch-dir
backend — **git-worktree is an embedder implementation of this protocol**, not
core, so the SDK never hardcodes ``git``.

Two tiers of conflict control, smallest first:

* :class:`~linch.tools.base.ResourceAccess` — cheap; serializes same-resource
  writes within one process (no isolation, shared cwd).
* :class:`IsolationBackend` — strong; each branch runs in its own cwd, so
  parallel branches can edit freely and merge later.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from .._blocking import run_blocking

_T = TypeVar("_T")


async def _drain_task(task: asyncio.Task[_T]) -> tuple[_T | None, BaseException | None]:
    """Wait for a task despite repeated cancellation of the current task."""

    while True:
        try:
            return await asyncio.shield(task), None
        except asyncio.CancelledError:
            if not task.done():
                continue
            try:
                return task.result(), None
            except BaseException as exc:  # noqa: BLE001 - report the drained task error
                return None, exc
        except BaseException as exc:  # noqa: BLE001 - report the drained task error
            return None, exc


@runtime_checkable
class IsolationBackend(Protocol):
    """Duck-typed seam for per-branch working directories.

    ``acquire`` returns a fresh cwd; ``release`` disposes of it (``keep=True``
    preserves it for inspection / later merge). Implementations must make
    distinct ``acquire`` calls return distinct, independently-writable paths.
    """

    async def acquire(self) -> str:
        """Allocate an independent working directory.

        Returns:
            Fresh path that can be written independently from other branches.
        """

        ...

    async def release(self, cwd: str, *, keep: bool = False) -> None:
        """Release an acquired working directory.

        Args:
            cwd: Path returned by :meth:`acquire`.
            keep: Whether to preserve the directory for inspection or merging.
        """

        ...


class TempDirIsolation:
    """Scratch-directory isolation: each ``acquire`` is a fresh temp dir.

    With ``source`` set, the scratch dir is seeded with a recursive copy of that
    directory (edits to the copy never touch the source). ``root`` chooses the
    parent directory for scratch dirs (default: the system temp dir). Blocking
    filesystem work runs in a thread so the loop never blocks.

    Args:
        source: Optional directory copied into each scratch directory.
        root: Optional parent directory for scratch directories.
    """

    def __init__(self, *, source: str | None = None, root: str | None = None) -> None:
        """Configure scratch-directory isolation.

        Args:
            source: Optional directory copied into each acquired directory.
            root: Optional parent directory for acquired directories.
        """

        source_path = Path(source).resolve() if source else None
        root_path = Path(root).resolve() if root else None
        self._source = str(source_path) if source_path else None
        self._root = str(root_path) if root_path else None

    async def acquire(self) -> str:
        """Create and optionally seed a scratch directory.

        Returns:
            Path to a fresh isolated directory.

        Raises:
            ValueError: If the temporary root is inside the source directory.
        """

        if self._source and self._root and Path(self._root).is_relative_to(Path(self._source)):
            raise ValueError("root must not be inside source")
        task = asyncio.create_task(run_blocking(self._acquire_sync))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            cwd, acquire_error = await _drain_task(task)
            release_error: BaseException | None = None
            if cwd is not None:
                release_task = asyncio.create_task(self.release(cwd))
                _, release_error = await _drain_task(release_task)
            if acquire_error is not None:
                raise cancellation from acquire_error
            if release_error is not None:
                raise cancellation from release_error
            raise cancellation

    def _acquire_sync(self) -> str:
        if self._root:
            Path(self._root).mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="linch-iso-", dir=self._root)).resolve()
        source_path = Path(self._source) if self._source else None
        if source_path is not None and path.is_relative_to(source_path):
            if path != source_path:
                shutil.rmtree(path, ignore_errors=True)
            raise ValueError("root must not be inside source")
        try:
            if self._source:
                shutil.copytree(self._source, path, dirs_exist_ok=True)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise
        return str(path)

    async def release(self, cwd: str, *, keep: bool = False) -> None:
        """Remove an acquired scratch directory unless retention is requested.

        Args:
            cwd: Directory returned by :meth:`acquire`.
            keep: Whether to leave the directory in place.
        """

        if keep:
            return
        await run_blocking(shutil.rmtree, cwd, ignore_errors=True)
