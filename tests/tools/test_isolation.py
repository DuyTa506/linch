"""IsolationBackend protocol + TempDirIsolation.

Parallel subagents share one cwd today, so real-disk edits to the same relative
path collide. The isolation seam gives each branch its own working directory.
The SDK ships only the protocol + a trivial scratch-dir backend; git-worktree is
an embedder implementation of the same protocol.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

import linch.tools.isolation as isolation_module
from linch.tools.isolation import IsolationBackend, TempDirIsolation


async def test_source_seeds_scratch_dir(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "seed.txt").write_text("seeded")

    iso = TempDirIsolation(source=str(source))
    cwd = await iso.acquire()
    try:
        assert (Path(cwd) / "seed.txt").read_text() == "seeded"
        # Editing the scratch copy must not touch the source.
        (Path(cwd) / "seed.txt").write_text("changed")
        assert (source / "seed.txt").read_text() == "seeded"
    finally:
        await iso.release(cwd)


async def test_release_missing_dir_is_safe() -> None:
    iso = TempDirIsolation()
    cwd = await iso.acquire()
    await iso.release(cwd)
    # Double release must not raise.
    await iso.release(cwd)


async def test_acquires_under_custom_root(tmp_path: Path) -> None:
    root = tmp_path / "scratch-root"
    iso = TempDirIsolation(root=str(root))
    cwd = await iso.acquire()
    try:
        assert Path(cwd).parent == root
    finally:
        await iso.release(cwd)


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancel_during_copy_releases_finished_workspace(
    tmp_path: Path, cancel_count: int
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "seed.txt").write_text("seeded")
    root = tmp_path / "scratch-root"
    started = threading.Event()
    continue_copy = threading.Event()
    finished = threading.Event()

    class SlowCopy(TempDirIsolation):
        def _acquire_sync(self) -> str:
            started.set()
            assert continue_copy.wait(timeout=2)
            try:
                return super()._acquire_sync()
            finally:
                finished.set()

    iso = SlowCopy(source=str(source), root=str(root))
    task = asyncio.create_task(iso.acquire())
    assert await asyncio.to_thread(started.wait, 2)
    for index in range(cancel_count):
        task.cancel()
        if index + 1 < cancel_count:
            await asyncio.sleep(0)
    continue_copy.set()
    assert await asyncio.to_thread(finished.wait, 2)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not root.exists() or not any(root.iterdir())


async def test_partial_copy_failure_removes_workspace(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "scratch-root"

    def fail_copy(_source: str, destination: str, *, dirs_exist_ok: bool, symlinks: bool) -> None:
        assert dirs_exist_ok is True
        assert symlinks is True
        (Path(destination) / "partial.txt").write_text("partial")
        raise OSError("copy failed")

    monkeypatch.setattr(isolation_module.shutil, "copytree", fail_copy)
    iso = TempDirIsolation(source=str(source), root=str(root))

    with pytest.raises(OSError, match="copy failed"):
        await iso.acquire()

    assert root.exists()
    assert not any(root.iterdir())


async def test_cancelled_copy_failure_preserves_cancellation_and_cleans_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / "scratch-root"
    started = threading.Event()
    continue_copy = threading.Event()

    def fail_copy(_source: str, destination: str, *, dirs_exist_ok: bool, symlinks: bool) -> None:
        assert dirs_exist_ok is True
        assert symlinks is True
        (Path(destination) / "partial.txt").write_text("partial")
        raise OSError("copy failed")

    monkeypatch.setattr(isolation_module.shutil, "copytree", fail_copy)

    class FailingCopy(TempDirIsolation):
        def _acquire_sync(self) -> str:
            started.set()
            assert continue_copy.wait(timeout=2)
            return super()._acquire_sync()

    iso = FailingCopy(source=str(source), root=str(root))
    task = asyncio.create_task(iso.acquire())
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    continue_copy.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert root.exists()
    assert not any(root.iterdir())


@pytest.mark.parametrize("root_name", ["source", "source/.linch/iso"])
async def test_root_inside_source_fails_closed(tmp_path: Path, root_name: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    root = tmp_path / root_name

    iso = TempDirIsolation(source=str(source), root=str(root))
    with pytest.raises(ValueError, match="root.*source"):
        await iso.acquire()

    if root == source:
        assert not any(root.iterdir())
    else:
        assert not root.exists()


async def test_default_root_inside_source_fails_before_copytree(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    acquired = source / "linch-iso-forced"

    def fake_mkdtemp(*, prefix: str, dir: str | None) -> str:
        assert prefix == "linch-iso-"
        assert dir is None
        acquired.mkdir()
        return str(acquired)

    def unexpected_copy(*_args, **_kwargs) -> None:
        raise AssertionError("copytree must not run inside its source")

    monkeypatch.setattr(isolation_module.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(isolation_module.shutil, "copytree", unexpected_copy)

    iso = TempDirIsolation(source=str(source))
    with pytest.raises(ValueError, match="root.*source"):
        await iso.acquire()

    assert not acquired.exists()


def test_tempdir_isolation_satisfies_protocol() -> None:
    assert isinstance(TempDirIsolation(), IsolationBackend)


async def test_copy_rejects_symlinks_outside_isolated_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("outside-secret")
    (source / "link.txt").symlink_to(outside)
    scratch = tmp_path / "scratch"
    isolation = TempDirIsolation(source=str(source), root=str(scratch))
    with pytest.raises(ValueError, match="symlink"):
        await isolation.acquire()
    assert not any(scratch.iterdir())
