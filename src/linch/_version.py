from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

_PACKAGE_NAME = "linch"
_UNKNOWN_VERSION = "unknown"


def get_version() -> str:
    """Return the installed package version, falling back to ``pyproject.toml``.

    Returns:
        Installed distribution version, the version declared in the local
        project file, or ``"unknown"`` when neither is available.
    """
    try:
        return metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return _version_from_pyproject()


def _version_from_pyproject() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return _UNKNOWN_VERSION
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    return match.group(1) if match else _UNKNOWN_VERSION
