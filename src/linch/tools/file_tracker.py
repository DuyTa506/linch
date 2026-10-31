from __future__ import annotations


class FileReadTracker:
    """Track workspace paths read during a session or run.

    The tracker stores caller-supplied path strings and exposes sorted snapshots for
    deterministic policy checks.
    """

    def __init__(self) -> None:
        """Create an empty read tracker."""

        self._files: set[str] = set()

    def add(self, path: str) -> None:
        """Record a path as read.

        Args:
            path: Path to add to the read set.
        """

        self._files.add(path)

    def mark_read(self, path: str) -> None:
        """Record a path as read through the policy-facing name.

        Args:
            path: Path to add to the read set.
        """

        self.add(path)

    def files(self) -> list[str]:
        """Return all tracked paths in deterministic order.

        Returns:
            Sorted paths currently marked as read.
        """

        return sorted(self._files)

    def clear(self) -> None:
        """Remove every tracked path."""

        self._files.clear()

    def has_read(self, path: str) -> bool:
        """Return whether a path has been recorded as read.

        Args:
            path: Path to check.

        Returns:
            Whether ``path`` is in the read set.
        """

        return path in self._files

    def __len__(self) -> int:
        """Return the number of tracked paths.

        Returns:
            Count of paths currently marked as read.
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
