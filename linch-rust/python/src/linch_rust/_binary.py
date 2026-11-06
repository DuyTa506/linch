"""Locate the `linch-server` binary."""

from __future__ import annotations

import os
from importlib import metadata
from pathlib import Path

ENV_VAR = "LINCH_SERVER_BIN"
_NAME = "linch-server"
_DIST = "linch-rust"
# src/linch_rust/_binary.py -> linch-rust/, the cargo workspace.
_WORKSPACE = Path(__file__).resolve().parents[3]


def find_server() -> Path:
    """Return the server binary path.

    The binary bundled in the installed `linch-rust` wheel wins; then
    `LINCH_SERVER_BIN`; then, for development from a checkout, the newer of
    the debug and release builds in cargo's target dir (`CARGO_TARGET_DIR`,
    else the workspace's `target/`). `Server(binary=...)` skips this search.

    Raises:
        FileNotFoundError: Nothing is bundled, the variable is unset and no
            build exists.
    """
    bundled = _bundled()
    if bundled is not None:
        return bundled
    explicit = os.environ.get(ENV_VAR)
    if explicit:
        return Path(explicit)
    target = Path(os.environ.get("CARGO_TARGET_DIR") or _WORKSPACE / "target")
    builds = [target / profile / _NAME for profile in ("debug", "release")]
    found = [build for build in builds if build.is_file()]
    if not found:
        raise FileNotFoundError(
            f"no {_NAME} binary: install the {_DIST} wheel, set {ENV_VAR}, or run "
            f"`cargo build -p {_NAME}` in {_WORKSPACE} (looked in {target})"
        )
    return max(found, key=lambda build: build.stat().st_mtime)


def _bundled() -> Path | None:
    # The wheel installs the binary as a script; its RECORD says where, for
    # any install scheme (a venv, --user, --prefix or --target).
    try:
        files = metadata.files(_DIST) or []
    except metadata.PackageNotFoundError:
        return None
    for file in files:
        if file.name == _NAME:
            path = Path(file.locate()).resolve()
            if path.is_file():
                return path
    return None
