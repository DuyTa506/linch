"""Real-disk :class:`~agent_kit.filesystem.backend.FileBackend`.

For when you *want* offloaded results as inspectable files on disk (to grep,
open in an editor, or keep as an audit trail) rather than in opaque state.
Unlike the built-in ``Read``/``Write`` tools — which operate on the user's
``cwd`` and would pollute the repo — this backend confines everything to a
dedicated *root* directory (default ``<cwd>/.agent_kit/offload``), so it stays
out of the way and is trivial to clean up.

All I/O runs via ``asyncio.to_thread`` so the event loop is never blocked.

Use it like any other backend::

    Agent(
        filesystem=DiskFileBackend(root=".agent_kit/offload"),
        result_offload=OffloadConfig(),
    )

To isolate per session, give each session its own subdir, e.g.
``DiskFileBackend(root=f".agent_kit/offload/{session_id}")`` via a factory, or
route a subtree with :class:`CompositeFileBackend`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .backend import _slice_lines, normalize_path


class DiskFileBackend:
    """A virtual filesystem mapped onto a real directory subtree."""

    def __init__(self, root: str | Path = ".agent_kit/offload") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _real_path(self, path: str) -> Path:
        # normalize_path guarantees a single leading slash and no '..' segments
        # (split on '/' drops empty parts; '..' is preserved, so guard it).
        norm = normalize_path(path)
        if ".." in norm.split("/"):
            raise ValueError(f"path may not contain '..': {path}")
        target = (self.root / norm.lstrip("/")).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes filesystem root: {path}") from exc
        return target

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        target = self._real_path(path)

        def _read() -> str:
            if not target.is_file():
                raise FileNotFoundError(path)
            return target.read_text(encoding="utf-8")

        text = await asyncio.to_thread(_read)
        return _slice_lines(text, offset, limit)

    async def write(self, path: str, content: str) -> None:
        target = self._real_path(path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

    async def ls(self, prefix: str = "") -> list[str]:
        def _ls() -> list[str]:
            out: list[str] = []
            for p in self.root.rglob("*"):
                if p.is_file():
                    out.append("/" + str(p.relative_to(self.root)).replace("\\", "/"))
            return out

        paths = await asyncio.to_thread(_ls)
        if not prefix:
            return sorted(paths)
        pfx = normalize_path(prefix)
        return sorted(p for p in paths if p == pfx or p.startswith(pfx.rstrip("/") + "/"))

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        target = self._real_path(path)

        def _edit() -> int:
            if not target.is_file():
                raise FileNotFoundError(path)
            text = target.read_text(encoding="utf-8")
            count = text.count(old)
            if count == 0:
                raise ValueError(f"old string not found in {path}")
            if count > 1 and not replace_all:
                raise ValueError(
                    f"old string is not unique in {path} ({count} matches); "
                    "pass replace_all=true or include more context"
                )
            updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            target.write_text(updated, encoding="utf-8")
            return count if replace_all else 1

        return await asyncio.to_thread(_edit)

    async def exists(self, path: str) -> bool:
        target = self._real_path(path)
        return await asyncio.to_thread(target.is_file)

    async def delete(self, path: str) -> None:
        target = self._real_path(path)

        def _delete() -> None:
            if target.is_file():
                target.unlink()

        await asyncio.to_thread(_delete)
