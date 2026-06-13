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
from typing import Protocol, runtime_checkable


@runtime_checkable
class IsolationBackend(Protocol):
    """Duck-typed seam for per-branch working directories.

    ``acquire`` returns a fresh cwd; ``release`` disposes of it (``keep=True``
    preserves it for inspection / later merge). Implementations must make
    distinct ``acquire`` calls return distinct, independently-writable paths.
    """

    async def acquire(self) -> str: ...

    async def release(self, cwd: str, *, keep: bool = False) -> None: ...


class TempDirIsolation:
    """Scratch-directory isolation: each ``acquire`` is a fresh temp dir.

    With ``source`` set, the scratch dir is seeded with a recursive copy of that
    directory (edits to the copy never touch the source). ``root`` chooses the
    parent directory for scratch dirs (default: the system temp dir). Blocking
    filesystem work runs in a thread so the loop never blocks.
    """

    def __init__(self, *, source: str | None = None, root: str | None = None) -> None:
        self._source = str(Path(source).resolve()) if source else None
        self._root = str(Path(root).resolve()) if root else None

    async def acquire(self) -> str:
        return await asyncio.to_thread(self._acquire_sync)

    def _acquire_sync(self) -> str:
        if self._root:
            Path(self._root).mkdir(parents=True, exist_ok=True)
        path = tempfile.mkdtemp(prefix="linch-iso-", dir=self._root)
        if self._source:
            shutil.copytree(self._source, path, dirs_exist_ok=True)
        return path

    async def release(self, cwd: str, *, keep: bool = False) -> None:
        if keep:
            return
        await asyncio.to_thread(shutil.rmtree, cwd, ignore_errors=True)
