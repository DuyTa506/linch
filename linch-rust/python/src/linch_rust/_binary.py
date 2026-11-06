"""Locate the `linch-server` binary."""

from __future__ import annotations

import os
from importlib import metadata
from pathlib import Path

ENV_VAR = "LINCH_SERVER_BIN"
_NAME = "linch-server"
_DIST = "linch-rust"
# src/linch_rust/_binary.py -> linch-rust/, the cargo workspace.
WORKSPACE = Path(__file__).resolve().parents[3]


def find_server() -> Path:
    """Return the server binary path.

    `LINCH_SERVER_BIN` wins, so a build of your own replaces the bundled
    one; then the binary bundled in the installed `linch-rust` wheel; then,
    for development from a checkout, the newer of the debug and release
    builds in cargo's target dir (`CARGO_TARGET_DIR`, else the workspace's
    `target/`). `Server(binary=...)` skips this search.

    Raises:
        FileNotFoundError: The variable is unset, nothing is bundled and no
            build exists.
    """
    explicit = os.environ.get(ENV_VAR)
    if explicit:
        return Path(explicit)
    bundled = _bundled()
    if bundled is not None:
        return bundled
    target = Path(os.environ.get("CARGO_TARGET_DIR") or WORKSPACE / "target")
    builds = [target / profile / _NAME for profile in ("debug", "release")]
    found = [build for build in builds if build.is_file()]
    if not found:
        raise FileNotFoundError(
            f"no {_NAME} binary: install the {_DIST} wheel, set {ENV_VAR}, or run "
            f"`cargo build -p {_NAME}` in {WORKSPACE} (looked in {target})"
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
