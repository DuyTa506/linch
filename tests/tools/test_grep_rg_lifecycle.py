from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from linch.tools import builtin

_FAKE_RG = """#!/bin/sh
sleep 60 &
echo $! > child.pid
echo $$ > rg.pid
wait
"""


def _alive(pid: int) -> bool:
    """Whether `pid` runs. A zombie is dead: it is only waiting to be reaped.

    `os.kill(pid, 0)` succeeds on a zombie, and an orphan stays one when the
    container's init does not reap. Where `/proc` exists, its state tells.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except FileNotFoundError:
        return not Path("/proc/self/stat").exists()  # gone, or there is no /proc
    return state != "Z"


async def _gone(pid: int) -> bool:
    """Whether `pid` is dead within 5 s: SIGKILL takes effect a moment later."""
    for _ in range(250):
        if not _alive(pid):
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
def fake_rg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake-rg"
    script.write_text(_FAKE_RG)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(builtin, "_RG_PATH", str(script))
    return tmp_path


async def _pids(root: Path) -> tuple[int, int]:
    for _ in range(100):
        if (root / "rg.pid").exists() and (root / "child.pid").exists():
            return int((root / "rg.pid").read_text()), int((root / "child.pid").read_text())
        await asyncio.sleep(0.02)
    raise AssertionError("fake rg did not start")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
async def test_grep_rg_killed_and_reaped_on_timeout(fake_rg: Path) -> None:
    with pytest.raises(TimeoutError):
        await builtin._run_grep_via_rg_async([], str(fake_rg), timeout=0.5)
    rg_pid, child_pid = await _pids(fake_rg)
    assert await _gone(rg_pid)
    assert await _gone(child_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
async def test_grep_rg_killed_and_reaped_on_cancel(fake_rg: Path) -> None:
    task = asyncio.ensure_future(builtin._run_grep_via_rg_async([], str(fake_rg), timeout=30))
    rg_pid, child_pid = await _pids(fake_rg)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _gone(rg_pid)
    assert await _gone(child_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX processes")
async def test_alive_counts_a_zombie_as_dead() -> None:
    # A child that exited and was not waited for stays a zombie; `os.kill(pid, 0)`
    # still succeeds on it, as it does for an orphan whose init does not reap.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        assert await _gone(child.pid)
    finally:
        child.wait()
