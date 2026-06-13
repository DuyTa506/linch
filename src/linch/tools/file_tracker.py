from __future__ import annotations


class FileReadTracker:
    def __init__(self) -> None:
        self._files: set[str] = set()

    def add(self, path: str) -> None:
        self._files.add(path)

    def mark_read(self, path: str) -> None:
        self.add(path)

    def files(self) -> list[str]:
        return sorted(self._files)

    def clear(self) -> None:
        self._files.clear()

    def has_read(self, path: str) -> bool:
        return path in self._files

    def __len__(self) -> int:
        return len(self._files)

    def __contains__(self, path: str) -> bool:
        return path in self._files
