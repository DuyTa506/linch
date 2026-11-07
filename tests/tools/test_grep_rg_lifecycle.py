from __future__ import annotations

import asyncio
import os
import stat
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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
    assert not _alive(rg_pid)
    assert not _alive(child_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
async def test_grep_rg_killed_and_reaped_on_cancel(fake_rg: Path) -> None:
    task = asyncio.ensure_future(builtin._run_grep_via_rg_async([], str(fake_rg), timeout=30))
    rg_pid, child_pid = await _pids(fake_rg)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(rg_pid)
    assert not _alive(child_pid)
