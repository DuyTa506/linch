"""`find_server` order: `LINCH_SERVER_BIN`, then the wheel's bundled binary, then cargo."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from linch_rust import _binary, find_server


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def installed_wheel(prefix: Path, *, with_binary: bool = True) -> Path:
    """Lay out a `linch-rust` install: a dist-info whose RECORD lists the script."""
    site = prefix / "site"
    info = site / "linch_rust-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: linch-rust\nVersion: 0.1.0\n")
    (info / "RECORD").write_text(
        "../bin/linch-server,,\nlinch_rust-0.1.0.dist-info/METADATA,,\n"
        "linch_rust-0.1.0.dist-info/RECORD,,\n"
    )
    binary = prefix / "bin" / "linch-server"
    if with_binary:
        executable(binary)
    return site


@pytest.fixture
def cargo_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A debug build in a `CARGO_TARGET_DIR` of its own."""
    target = tmp_path / "target"
    monkeypatch.setenv("CARGO_TARGET_DIR", str(target))
    return executable(target / "debug" / "linch-server")


@pytest.fixture
def nothing_bundled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the binary of an installed wheel, as in a checkout."""
    monkeypatch.setattr(_binary, "_bundled", lambda: None)


def test_the_variable_wins_over_the_bundled_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cargo_build: Path
) -> None:
    site = installed_wheel(tmp_path / "env")
    monkeypatch.syspath_prepend(str(site))
    explicit = executable(tmp_path / "other" / "linch-server")
    monkeypatch.setenv(_binary.ENV_VAR, str(explicit))

    assert find_server() == explicit


def test_the_bundled_binary_wins_over_cargo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cargo_build: Path
) -> None:
    monkeypatch.syspath_prepend(str(installed_wheel(tmp_path / "env")))
    monkeypatch.delenv(_binary.ENV_VAR, raising=False)

    assert find_server().samefile(tmp_path / "env" / "bin" / "linch-server")


def test_a_listed_binary_missing_on_disk_is_not_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cargo_build: Path
) -> None:
    monkeypatch.syspath_prepend(str(installed_wheel(tmp_path / "env", with_binary=False)))
    monkeypatch.delenv(_binary.ENV_VAR, raising=False)

    assert find_server() == cargo_build


@pytest.mark.usefixtures("nothing_bundled")
def test_the_variable_wins_over_cargo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cargo_build: Path
) -> None:
    explicit = tmp_path / "other" / "linch-server"
    monkeypatch.setenv(_binary.ENV_VAR, str(explicit))

    assert find_server() == explicit


@pytest.mark.usefixtures("nothing_bundled")
def test_cargo_takes_the_newer_build_last(
    monkeypatch: pytest.MonkeyPatch, cargo_build: Path
) -> None:
    monkeypatch.delenv(_binary.ENV_VAR, raising=False)
    release = executable(cargo_build.parents[1] / "release" / "linch-server")
    os.utime(cargo_build, (1_000, 1_000))

    assert find_server() == release


@pytest.mark.usefixtures("nothing_bundled")
def test_no_binary_names_every_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_binary.ENV_VAR, raising=False)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "empty"))

    with pytest.raises(FileNotFoundError, match="linch-rust wheel.*LINCH_SERVER_BIN.*cargo build"):
        find_server()
