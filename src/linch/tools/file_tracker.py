from __future__ import annotations


class FileReadTracker:
    """Track workspace paths read during a session or run.

    The tracker stores caller-supplied paths and line ranges. Sorted snapshots
    export only whole-file reads so partial evidence cannot grant full access.
    """

    def __init__(self) -> None:
        """Create an empty read tracker."""

        self._files: set[str] = set()
        self._ranges: dict[str, list[tuple[int, int]]] = {}
        self._stamps: dict[str, tuple[int, int]] = {}

    def add(self, path: str) -> None:
        """Record a path as read.

        Args:
            path: Path to add to the read set.
        """

        self._files.add(path)

    def mark_read(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> None:
        """Record a path as read through the policy-facing name.

        Args:
            path: Path to add to the read set.
            start_line: First line read, or ``None`` for the whole file.
            end_line: Last line read, or ``None`` for the whole file.
        """

        if start_line is None or end_line is None:
            self.add(path)
            return
        start = max(1, start_line)
        end = max(start, end_line)
        ranges = self._ranges.setdefault(path, [])
        ranges.append((start, end))
        ranges.sort()
        merged: list[tuple[int, int]] = []
        for current_start, current_end in ranges:
            if merged and current_start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], current_end))
            else:
                merged.append((current_start, current_end))
        self._ranges[path] = merged

    def files(self) -> list[str]:
        """Return fully read paths in deterministic order.

        Returns:
            Sorted paths whose whole contents were marked as read.
        """

        return sorted(self._files)

    def clear(self) -> None:
        """Remove every tracked path."""

        self._files.clear()
        self._ranges.clear()
        self._stamps.clear()

    def has_read(self, path: str) -> bool:
        """Return whether a whole file has been recorded as read.

        Args:
            path: Path to check.

        Returns:
            Whether ``path`` has whole-file read evidence.
        """

        return path in self._files

    def has_read_range(self, path: str, start_line: int, end_line: int) -> bool:
        """Return whether every line in a requested range has been read.

        Args:
            path: Path to check.
            start_line: First line required by the operation.
            end_line: Last line required by the operation.

        Returns:
            Whether the full range is covered by a whole-file or windowed read.
        """

        if path in self._files:
            return True
        start = max(1, start_line)
        end = max(start, end_line)
        return any(
            read_start <= start and read_end >= end
            for read_start, read_end in self._ranges.get(path, [])
        )

    def invalidate_ranges(self, path: str) -> None:
        """Drop windowed-read evidence for a path after its contents change.

        Args:
            path: Path whose partial-read ranges are stale.
        """

        self._ranges.pop(path, None)

    def __len__(self) -> int:
        """Return the number of fully read paths.

        Returns:
            Count of paths with whole-file read evidence.
        """

        return len(self._files)

    def __contains__(self, path: str) -> bool:
        """Return whether a path is tracked using ``path in tracker`` syntax.

        Args:
            path: Path to check.

        Returns:
            Whether ``path`` is currently tracked.
        """

        return path in self._files

    def is_current(self, path: str, stamp: tuple[int, int]) -> bool:
        return self._stamps.get(path) == stamp

    def set_stamp(self, path: str, stamp: tuple[int, int], *, reset: bool = False) -> None:
        if reset and not self.is_current(path, stamp):
            self._files.discard(path)
            self.invalidate_ranges(path)
        self._stamps[path] = stamp

    def snapshot(self) -> dict[str, object]:
        return {"files": self.files(), "stamps": {p: list(s) for p, s in self._stamps.items()}}

    def restore(self, snapshot: object) -> None:
        self.clear()
        if not isinstance(snapshot, dict):
            return
        files, stamps = snapshot.get("files"), snapshot.get("stamps")
        if isinstance(files, list):
            self._files.update(path for path in files if isinstance(path, str))
        if isinstance(stamps, dict):
            for path, stamp in stamps.items():
                if (
                    isinstance(path, str)
                    and isinstance(stamp, (list, tuple))
                    and len(stamp) == 2
                    and all(type(value) is int for value in stamp)
                    and stamp[1] >= 0
                ):
                    self._stamps[path] = (stamp[0], stamp[1])
