"""IsolationBackend protocol + TempDirIsolation (ROADMAP Phase 2.2).

Parallel subagents share one cwd today, so real-disk edits to the same relative
path collide. The isolation seam gives each branch its own working directory.
The SDK ships only the protocol + a trivial scratch-dir backend; git-worktree is
an embedder implementation of the same protocol.
"""

from __future__ import annotations

from pathlib import Path

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


def test_tempdir_isolation_satisfies_protocol() -> None:
    assert isinstance(TempDirIsolation(), IsolationBackend)
