"""Bounded byte retention, best-effort spill, and incremental output cursors."""

from __future__ import annotations

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

    @property
    def truncated(self) -> bool:
        return self._total > OUTPUT_CAP

    def append(self, chunk: bytes) -> None:
        previous = self._head + self._tail
        self._total += len(chunk)
        if self.truncated and self._total <= 64 * 1024 * 1024:
            try:
                if self._file is None and self._total - len(chunk) <= OUTPUT_CAP:
                    self._directory.mkdir(parents=True, exist_ok=True)
                    (self._directory / ".gitignore").write_text("*\n")
                    path = self._directory / f"{uuid.uuid4().hex}-{self._stream}.log"
                    self._file = os.fdopen(
                        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                        "wb",
                        buffering=0,
                    )
                    self.path = str(path)
                    if self._file.write(previous) != len(previous):
                        raise OSError("incomplete spill write")
                if self._file is not None and self._file.write(chunk) != len(chunk):
                    self._discard()
            except OSError:
                self._discard()
        elif self._total > 64 * 1024 * 1024:
            self._discard()
        needed = 4000 - len(self._head)
        self._head += chunk[:needed]
        self._tail = (self._tail + chunk[max(needed, len(chunk) - 26000) :])[-26000:]

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
        if self.truncated:
            omitted = self._total - len(self._head) - len(self._tail)
            return (
                self._head.decode("utf-8", errors="replace")
                + f"\n… ({omitted} bytes omitted)\n"
                + self._tail.decode("utf-8", errors="replace")
            )
        return (self._head + self._tail).decode("utf-8", errors="replace")

    def read(self) -> tuple[str, bool]:
        gap = max(len(self._head), self._total - len(self._tail))
        lossy = self._cursor < gap and self._total > OUTPUT_CAP
        data = self._head[self._cursor :] if self._cursor < len(self._head) else b""
        text = self._decoder.decode(data, final=lossy)
        if lossy:
            self._decoder.reset()
        data = self._tail[max(0, self._cursor - gap) :]
        self._cursor = self._total
        return text + self._decoder.decode(data, final=self._finished), lossy
