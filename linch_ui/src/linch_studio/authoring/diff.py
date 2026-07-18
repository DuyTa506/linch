"""Deterministic semantic blueprint diffs."""

from __future__ import annotations

from typing import Any

from linch_studio.spec import Blueprint, canonical_data, json_path

from .models import SemanticDiffEntry


def semantic_diff(before: Blueprint, after: Blueprint) -> tuple[SemanticDiffEntry, ...]:
    """Compare normalized semantic data, excluding layout by construction.

    Collections retain declaration-order semantics, so list positions are
    intentionally compared by index. The result is descriptive rather than an
    executable JSON Patch; accepting a proposal always replaces the blueprint
    atomically after a digest check.
    """

    changes: list[SemanticDiffEntry] = []
    _compare(canonical_data(before), canonical_data(after), (), changes)
    return tuple(sorted(changes, key=lambda item: (item.path, item.operation)))


def _compare(
    before: Any,
    after: Any,
    path: tuple[str | int, ...],
    changes: list[SemanticDiffEntry],
) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys - after_keys):
            changes.append(
                SemanticDiffEntry(
                    operation="remove",
                    path=json_path(*path, key),
                    before=before[key],
                )
            )
        for key in sorted(before_keys & after_keys):
            _compare(before[key], after[key], (*path, key), changes)
        for key in sorted(after_keys - before_keys):
            changes.append(
                SemanticDiffEntry(
                    operation="add",
                    path=json_path(*path, key),
                    after=after[key],
                )
            )
        return

    if isinstance(before, list) and isinstance(after, list):
        shared = min(len(before), len(after))
        for index in range(shared):
            _compare(before[index], after[index], (*path, index), changes)
        for index in range(len(before) - 1, shared - 1, -1):
            changes.append(
                SemanticDiffEntry(
                    operation="remove",
                    path=json_path(*path, index),
                    before=before[index],
                )
            )
        for index in range(shared, len(after)):
            changes.append(
                SemanticDiffEntry(
                    operation="add",
                    path=json_path(*path, index),
                    after=after[index],
                )
            )
        return

    if before != after:
        changes.append(
            SemanticDiffEntry(
                operation="replace",
                path=json_path(*path),
                before=before,
                after=after,
            )
        )


__all__ = ["semantic_diff"]
