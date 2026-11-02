"""Bounded byte retention, best-effort spill, and incremental output cursors."""

from __future__ import annotations

import asyncio
import codecs
import os
import uuid
from pathlib import Path
from typing import BinaryIO

from ._limits import OUTPUT_CAP


class BoundedOutput:
    def __init__(self, cwd: str, stream: str) -> None:
        self._directory = Path(cwd) / ".linch" / "output"
        self._stream = stream
        self._head = b""
        self._tail = b""
        self._total = self._cursor = 0
        self._file: BinaryIO | None = None
        self.path: str | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._finished = False
        self._dropped = False
        self._drop_reported = False

    @property
    def truncated(self) -> bool:
        return self._total > OUTPUT_CAP or self._dropped

    def _retain(self, chunk: bytes) -> tuple[bytes, int]:
        previous = self._head + self._tail
        before = self._total
        self._total += len(chunk)
        needed = max(0, 4000 - len(self._head))
        self._head += chunk[:needed]
        self._tail = (self._tail + chunk[max(needed, len(chunk) - 26000) :])[-26000:]
        return previous, before

    def append(self, chunk: bytes) -> None:
        previous, before = self._retain(chunk)
        if self._total > OUTPUT_CAP:
            self._spill(chunk, previous, before)

    async def append_async(self, chunk: bytes) -> None:
        previous, before = self._retain(chunk)
        if self._total > OUTPUT_CAP and (self._total <= 64 * 1024 * 1024 or self._file):
            await _thread(self._spill, chunk, previous, before)

    def _spill(self, chunk: bytes, previous: bytes, before: int) -> None:
        if self._total > 64 * 1024 * 1024:
            self._discard()
            return
        try:
            if self._file is None and before <= OUTPUT_CAP:
                parent = self._directory.parent
                if parent.is_symlink() or self._directory.is_symlink():
                    return
                self._directory.mkdir(parents=True, exist_ok=True)
                if parent.is_symlink() or self._directory.is_symlink():
                    return
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(self._directory / ".gitignore", flags, 0o600)
                except FileExistsError:
                    pass
                else:
                    try:
                        os.write(fd, b"*\n")
                    finally:
                        os.close(fd)
                path = self._directory / f"{uuid.uuid4().hex}-{self._stream}.log"
                self._file = os.fdopen(os.open(path, flags, 0o600), "wb", buffering=0)
                self.path = str(path)
                if self._file.write(previous) != len(previous):
                    raise OSError("incomplete spill write")
            if self._file is not None and self._file.write(chunk) != len(chunk):
                self._discard()
        except OSError:
            self._discard()

    async def finish_async(self) -> None:
        if self._file is not None:
            await _thread(self.finish)
        else:
            self.finish()

    async def discard_async(self) -> None:
        await _thread(self._discard)

    def drop(self) -> None:
        self._dropped = True

    def _discard(self) -> None:
        self.finish()
        self._finished = False
        if self.path is not None:
            try:
                Path(self.path).unlink(missing_ok=True)
            except OSError:
                pass
            self.path = None

    def finish(self) -> None:
        if self._file is not None:
            file, self._file = self._file, None
            try:
                file.close()
            except OSError:
                self._discard()
        self._finished = True

    def snapshot(self) -> str:
        if self._total > OUTPUT_CAP:
            omitted = self._total - len(self._head) - len(self._tail)
            return (
                self._head.decode("utf-8", errors="replace")
                + f"\n… ({omitted} bytes omitted)\n"
                + self._tail.decode("utf-8", errors="replace")
                + ("\n[output truncated]" if self._dropped else "")
            )
        text = (self._head + self._tail).decode("utf-8", errors="replace")
        return text + "\n[output truncated]" if self._dropped else text

    def read(self) -> tuple[str, bool]:
        gap = max(len(self._head), self._total - len(self._tail))
        lossy = self._cursor < gap and self._total > OUTPUT_CAP
        data = self._head[self._cursor :] if self._cursor < len(self._head) else b""
        text = self._decoder.decode(data, final=lossy)
        if lossy:
            self._decoder.reset()
        data = self._tail[max(0, self._cursor - gap) :]
        self._cursor = self._total
        text += self._decoder.decode(data, final=self._finished)
        if self._dropped and not self._drop_reported:
            self._drop_reported = True
            return text + "\n[output truncated]", True
        return text, lossy


async def _thread(func, *args):
    # A cancelled pump must not close its file while a worker is still writing.
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
