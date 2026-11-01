"""Helpers for loading repository scripts and examples as isolated modules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_module(path: Path, name: str) -> ModuleType:
    """Load a Python file under an isolated module name.

    Args:
        path: File to import.
        name: Stable name used for the temporary module registration.

    Returns:
        The executed module object.

    Raises:
        AssertionError: If Python cannot build an import specification or loader.
    """

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
