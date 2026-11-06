"""Locate the `linch-server` binary."""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "LINCH_SERVER_BIN"
_NAME = "linch-server"
# src/linch_rust/_binary.py -> linch-rust/, the cargo workspace.
_WORKSPACE = Path(__file__).resolve().parents[3]


def find_server() -> Path:
    """Return the server binary path.

    `LINCH_SERVER_BIN` wins; otherwise the newer of the debug and release
    builds in cargo's target dir (`CARGO_TARGET_DIR`, else the workspace's
    `target/`), for development from a checkout.

    Raises:
        FileNotFoundError: No variable is set and no build exists.
    """
    explicit = os.environ.get(ENV_VAR)
    if explicit:
        return Path(explicit)
    target = Path(os.environ.get("CARGO_TARGET_DIR") or _WORKSPACE / "target")
    builds = [target / profile / _NAME for profile in ("debug", "release")]
    found = [build for build in builds if build.is_file()]
    if not found:
        raise FileNotFoundError(
            f"no {_NAME} binary: set {ENV_VAR}, or run `cargo build -p {_NAME}` "
            f"in {_WORKSPACE} (looked in {target})"
        )
    return max(found, key=lambda build: build.stat().st_mtime)
