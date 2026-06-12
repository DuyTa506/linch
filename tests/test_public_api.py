"""Public-surface contract guard (ROADMAP Phase 5.1).

`linch.__all__` is the supported, semver-governed public API. This test pins the
contract so an accidental rename/removal or an undeclared public leak is caught
in CI rather than in an embedder's pinned import:

- every name in `__all__` resolves on the package,
- `__all__` has no duplicate entries,
- every public (non-underscore) *attribute* of the package — submodules aside —
  is declared in `__all__`, so nothing leaks into the surface undeclared.

Submodule names (`linch.agent`, `linch.tools`, …) are import artifacts, not part
of the contract: embedders import from the top-level `linch` namespace only.
"""

from __future__ import annotations

import types
from collections import Counter

import linch


def test_all_names_resolve() -> None:
    unresolved = [name for name in linch.__all__ if not hasattr(linch, name)]
    assert unresolved == [], f"__all__ names with no attribute: {unresolved}"


def test_all_has_no_duplicates() -> None:
    dupes = [name for name, count in Counter(linch.__all__).items() if count > 1]
    assert dupes == [], f"duplicate __all__ entries: {dupes}"


def test_no_undeclared_public_attributes() -> None:
    declared = set(linch.__all__)
    leaked = [
        name
        for name in dir(linch)
        if not name.startswith("_")
        and name not in declared
        and not isinstance(getattr(linch, name), types.ModuleType)
    ]
    assert leaked == [], f"public attributes missing from __all__: {leaked}"
