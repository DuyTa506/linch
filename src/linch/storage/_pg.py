"""asyncpg import guard.

Used by all Postgres-backed store implementations.  Follows the repo's
established optional-dependency pattern: import guard at call-time, clear
error message pointing at the ``[postgres]`` extra.

Install::

    pip install 'linch[postgres]'
"""

from __future__ import annotations

from typing import Any


def _import_asyncpg() -> Any:
    """Return the ``asyncpg`` module or raise with an install hint."""
    try:
        import asyncpg  # type: ignore[import-untyped]

        return asyncpg
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Postgres storage requires the optional 'asyncpg' dependency. "
            "Install with: pip install 'linch[postgres]'"
        ) from exc
