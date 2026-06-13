"""IsolationBackend protocol + TempDirIsolation (ROADMAP Phase 2.2).

Parallel subagents share one cwd today, so real-disk edits to the same relative
path collide. The isolation seam gives each branch its own working directory.
The SDK ships only the protocol + a trivial scratch-dir backend; git-worktree is
an embedder implementation of the same protocol.
"""

from __future__ import annotations

from pathlib import Path

from linch.tools.isolation import IsolationBackend, TempDirIsolation


async def test_acquire_returns_fresh_writable_dir() -> None:
    iso = TempDirIsolation()
    cwd = await iso.acquire()
    try:
        p = Path(cwd)
        assert p.is_dir()
        (p / "f.txt").write_text("hi")
        assert (p / "f.txt").read_text() == "hi"
    finally:
        await iso.release(cwd)


async def test_two_acquires_dont_collide_on_same_relative_path() -> None:
    iso = TempDirIsolation()
    a = await iso.acquire()
    b = await iso.acquire()
    try:
        assert a != b
        (Path(a) / "shared.txt").write_text("from-a")
        (Path(b) / "shared.txt").write_text("from-b")
        # Same relative path, isolated content — no collision.
        assert (Path(a) / "shared.txt").read_text() == "from-a"
        assert (Path(b) / "shared.txt").read_text() == "from-b"
    finally:
        await iso.release(a)
        await iso.release(b)


async def test_release_removes_dir_by_default() -> None:
    iso = TempDirIsolation()
    cwd = await iso.acquire()
    assert Path(cwd).exists()
    await iso.release(cwd)
    assert not Path(cwd).exists()


async def test_release_keep_preserves_dir() -> None:
    iso = TempDirIsolation()
    cwd = await iso.acquire()
    try:
        await iso.release(cwd, keep=True)
        assert Path(cwd).exists()
    finally:
        await iso.release(cwd)  # actual cleanup


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


def test_tempdir_isolation_satisfies_protocol() -> None:
    assert isinstance(TempDirIsolation(), IsolationBackend)
