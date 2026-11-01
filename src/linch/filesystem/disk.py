"""Real-disk :class:`~linch.filesystem.backend.FileBackend`.

For when you *want* offloaded results as inspectable files on disk (to grep,
open in an editor, or keep as an audit trail) rather than in opaque state.
Unlike the built-in ``Read``/``Write`` tools — which operate on the user's
``cwd`` and would pollute the repo — this backend confines everything to a
dedicated *root* directory (default ``<cwd>/.linch/offload``), so it stays
out of the way and is trivial to clean up.

All I/O runs on a bounded daemon thread (``run_blocking``) so the event loop is
never blocked, and is confined to the configured root.

Use it like any other backend::

    Agent(
        filesystem=DiskFileBackend(root=".linch/offload"),
        result_offload=OffloadConfig(),
    )

To isolate per session, give each session its own subdir, e.g.
``DiskFileBackend(root=f".linch/offload/{session_id}")`` via a factory, or
route a subtree with :class:`CompositeFileBackend`.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from .._blocking import run_blocking
from .backend import _slice_lines, normalize_path


def _atomic_write(target: Path, content: str) -> None:
    """Write UTF-8 content through a sibling temporary file."""
    encoded = content.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    temporary: Path | None = None
    file = None
    try:
        for _ in range(10):
            candidate = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o666,
                )
            except FileExistsError:
                continue
            temporary = candidate
            file = os.fdopen(descriptor, "wb")
            break
        if file is None or temporary is None:
            raise FileExistsError(f"could not create a temporary file beside {target}")
        with file:
            if mode is not None:
                os.fchmod(file.fileno(), mode)
            file.write(encoded)
            file.flush()
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class DiskFileBackend:
    """Map the virtual filesystem onto a real directory subtree.

    All file operations run through the repository's bounded blocking-I/O
    helper, and every resolved path must remain below :attr:`root`.

    Args:
        root: Directory used as the backend's confinement root. It is resolved
            and created during construction.

    Attributes:
        root: Absolute path of the directory that stores virtual files.
    """

    resume_policy_id = "linch.filesystem.disk"
    resume_policy_version = "1"

    def __init__(self, root: str | Path = ".linch/offload") -> None:
        """Create a disk-backed virtual filesystem.

        Args:
            root: Directory used as the backend's confinement root. It is created
                if it does not already exist.
        """
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def resume_policy_config(self) -> dict[str, str]:
        """Return the durable identity of this backend's root.

        Returns:
            A mapping containing the resolved root path used in resume-policy
            fingerprints.
        """
        return {"root": str(self.root)}

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
        """Read a confined file, optionally returning a line window.

        Args:
            path: Slash-rooted virtual path below :attr:`root`.
            offset: 1-indexed line to start at; ``0`` and ``1`` start at the first line.
            limit: Maximum number of lines; non-positive or ``None`` reads to the end.

        Returns:
            The selected file contents.

        Raises:
            FileNotFoundError: If ``path`` is not a regular file.
            ValueError: If ``path`` contains traversal segments or resolves outside
                :attr:`root`.
        """
        target = self._real_path(path)

        def _read() -> str:
            if not target.is_file():
                raise FileNotFoundError(path)
            with target.open("r", encoding="utf-8", newline="") as file:
                return file.read()

        text = await run_blocking(_read)
        return _slice_lines(text, offset, limit)

    async def write(self, path: str, content: str) -> None:
        """Create or overwrite a confined file.

        Args:
            path: Slash-rooted virtual path below :attr:`root`.
            content: Complete text to write.

        Raises:
            ValueError: If ``path`` contains traversal segments or resolves outside
                :attr:`root`.
        """
        target = self._real_path(path)

        def _write() -> None:
            _atomic_write(target, content)

        await run_blocking(_write)

    async def ls(self, prefix: str = "") -> list[str]:
        """List regular files below the root, optionally by prefix.

        Args:
            prefix: Virtual path prefix used to filter the recursive listing.

        Returns:
            Sorted slash-rooted virtual paths for regular files matching ``prefix``.

        Raises:
            ValueError: If ``prefix`` is not a valid virtual path.
        """

        def _ls() -> list[str]:
            out: list[str] = []
            for p in self.root.rglob("*"):
                if p.is_file():
                    out.append("/" + str(p.relative_to(self.root)).replace("\\", "/"))
            return out

        paths = await run_blocking(_ls)
        if not prefix:
            return sorted(paths)
        pfx = normalize_path(prefix)
        return sorted(p for p in paths if p == pfx or p.startswith(pfx.rstrip("/") + "/"))

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        """Replace exact text in a confined file.

        Args:
            path: Slash-rooted virtual path below :attr:`root`.
            old: Exact text to find.
            new: Replacement text.
            replace_all: Whether to replace every occurrence instead of requiring a unique match.

        Returns:
            Number of replacements made.

        Raises:
            FileNotFoundError: If ``path`` is not a regular file.
            ValueError: If ``path`` is unsafe, ``old`` is absent, or ``old`` is
                not unique while ``replace_all`` is false.
        """
        target = self._real_path(path)

        def _edit() -> int:
            if not target.is_file():
                raise FileNotFoundError(path)
            with target.open("r", encoding="utf-8", newline="") as file:
                text = file.read()
            count = text.count(old)
            if count == 0:
                raise ValueError(f"old string not found in {path}")
            if count > 1 and not replace_all:
                raise ValueError(
                    f"old string is not unique in {path} ({count} matches); "
                    "pass replace_all=true or include more context"
                )
            updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            _atomic_write(target, updated)
            return count if replace_all else 1

        return await run_blocking(_edit)

    async def exists(self, path: str) -> bool:
        """Check whether a confined regular file exists.

        Args:
            path: Slash-rooted virtual path below :attr:`root`.

        Returns:
            ``True`` when the resolved path is a regular file; otherwise ``False``.

        Raises:
            ValueError: If ``path`` contains traversal segments or resolves outside
                :attr:`root`.
        """
        target = self._real_path(path)
        return await run_blocking(target.is_file)

    async def delete(self, path: str) -> None:
        """Delete a confined regular file if present.

        Args:
            path: Slash-rooted virtual path below :attr:`root`.

        Raises:
            ValueError: If ``path`` contains traversal segments or resolves outside
                :attr:`root`.
        """
        target = self._real_path(path)

        def _delete() -> None:
            if target.is_file():
                target.unlink()

        await run_blocking(_delete)
