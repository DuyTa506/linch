"""Capability-owned file contributions with collision-safe path handling."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from .serializers import normalized_text


class ContributionError(ValueError):
    """Raised when a capability attempts an unsafe or colliding contribution."""


@dataclass(frozen=True, slots=True)
class FileContribution:
    path: str
    content: str
    capability_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", validate_contribution_path(self.path))
        object.__setattr__(self, "content", normalized_text(self.content))
        if not self.capability_id:
            raise ContributionError("capability_id must not be empty")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class ContributionSet:
    """Accumulate contributions while rejecting normalized path collisions."""

    def __init__(self) -> None:
        self._by_key: dict[str, FileContribution] = {}

    def add(self, contribution: FileContribution) -> None:
        key = collision_key(contribution.path)
        previous = self._by_key.get(key)
        if previous is not None:
            raise ContributionError(
                "generated path collision between "
                f"{previous.path!r} ({previous.capability_id}) and "
                f"{contribution.path!r} ({contribution.capability_id})"
            )
        self._by_key[key] = contribution

    def extend(self, contributions: list[FileContribution] | tuple[FileContribution, ...]) -> None:
        for contribution in contributions:
            self.add(contribution)

    def sorted(self) -> tuple[FileContribution, ...]:
        return tuple(sorted(self._by_key.values(), key=lambda item: item.path))


def validate_contribution_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ContributionError("generated path must be a non-empty string without NUL")
    if "\\" in raw:
        raise ContributionError(f"generated path must use POSIX separators: {raw!r}")
    normalized = unicodedata.normalize("NFC", raw)
    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise ContributionError(f"generated path must be relative: {raw!r}")
    if normalized != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContributionError(f"generated path is not normalized or is unsafe: {raw!r}")
    if path.parts and ":" in path.parts[0]:
        raise ContributionError(f"generated path may not use a drive prefix: {raw!r}")
    return path.as_posix()


def collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", validate_contribution_path(path)).casefold()


__all__ = [
    "ContributionError",
    "ContributionSet",
    "FileContribution",
    "collision_key",
    "validate_contribution_path",
]
