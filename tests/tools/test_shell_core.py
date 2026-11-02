from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path

import pytest


def output(tmp_path):
    from linch.tools._output import BoundedOutput

    return BoundedOutput(str(tmp_path), "stdout")


def test_exact_cap_and_oversized_chunk(tmp_path):
    stream = output(tmp_path)
    stream.append(b"h" * 4000 + b"t" * 26000)
    assert stream.snapshot() == "h" * 4000 + "t" * 26000
    assert not stream.truncated and stream.path is None
    stream.append(b"z" * 100000)
    stream.finish()
    assert stream.snapshot() == "h" * 4000 + "\n… (100000 bytes omitted)\n" + "z" * 26000
    assert stream.truncated
    path = Path(stream.path)
    assert path.read_bytes() == b"h" * 4000 + b"t" * 26000 + b"z" * 100000
    assert path.stat().st_mode & 0o777 == 0o600
    assert (path.parent / ".gitignore").read_text() == "*\n"


def test_incremental_utf8_and_cursor_loss(tmp_path):
    stream = output(tmp_path)
    stream.append(b"\xe2")
    assert stream.read() == ("", False)
    stream.append(b"\x82\xac")
    assert stream.read() == ("€", False)
    assert stream.read() == ("", False)
    stream.append(b"a" * 50000)
    text, lossy = stream.read()
    assert text == "a" * 29997 and lossy
    assert stream.read() == ("", False)
    stream.finish()


def test_spill_failure_is_best_effort(tmp_path):
    (tmp_path / ".linch").write_text("occupied")
    stream = output(tmp_path)
    stream.append(b"x" * 100000)
    stream.finish()
    assert (
        stream.path is None
        and stream.snapshot() == "x" * 4000 + "\n… (70000 bytes omitted)\n" + "x" * 26000
    )


def test_spill_discarded_after_64mib(tmp_path):
    stream = output(tmp_path)
    for _ in range(64):
        stream.append(b"x" * (1024 * 1024))
    path = Path(stream.path)
    assert path.stat().st_size <= 64 * 1024 * 1024
    stream.append(b"z")
    stream.finish()
    assert stream.path is None and not path.exists()
    assert stream.snapshot().endswith("z")


async def run(tmp_path, command, **kwargs):
    from linch.tools.execution import LocalBackend

    return await LocalBackend().run(
        command, cwd=str(tmp_path), timeout_s=kwargs.pop("timeout_s", 2), **kwargs
    )


async def test_cat_stdin_is_closed(tmp_path):
    result = await run(tmp_path, "cat", timeout_s=0.2)
    assert not result.timed_out and result.returncode == 0


async def test_background_child_does_not_hold_result(tmp_path):
    started = time.monotonic()
    result = await run(tmp_path, "sleep .7 & echo x")
    elapsed = time.monotonic() - started
    assert result.stdout == "x\n" and elapsed < 0.55
    await asyncio.sleep(0.75)  # Let the intentionally surviving short child exit.


async def test_timeout_preserves_output_and_kills_exited_shell_children(tmp_path):
    pidfile = tmp_path / "pid"
    result = await run(tmp_path, f"sleep .8 & echo $! > {pidfile}; echo earlier", timeout_s=0.1)
    assert result.timed_out and not result.aborted and "earlier" in result.stdout
    pid = int(pidfile.read_text())
    # A zombie is dead but may await the container's init reaper.
    status = Path(f"/proc/{pid}/stat")
    assert not status.exists() or status.read_text().split()[2] == "Z"


async def test_abort_returns_partial_error_result(tmp_path):
    from linch.abort import AbortContext

    abort = AbortContext()
    task = asyncio.create_task(run(tmp_path, "echo partial; sleep 5", signal=abort))
    await asyncio.sleep(0.08)
    abort.abort()
    result = await task
    assert result.aborted and not result.timed_out and "partial" in result.stdout


async def test_handle_read_wait_and_kill(tmp_path):
    from linch import ShellProcess, ShellRead, ShellStarter
    from linch.tools.execution import LocalBackend

    backend = LocalBackend()
    assert isinstance(backend, ShellStarter)
    handle = await backend.start("echo first; sleep 5", cwd=str(tmp_path))
    assert isinstance(handle, ShellProcess)
    assert await handle.wait(0.08) is None
    delta = handle.read()
    assert isinstance(delta, ShellRead) and delta.stdout == "first\n" and not delta.lossy
    assert handle.read().stdout == ""
    await handle.kill()
    result = await handle.wait(None)
    assert result.returncode != 0 and result.signal == signal.SIGTERM


async def test_environment_allowlist_and_explicit_values(tmp_path, monkeypatch):
    from linch.tools.execution import LocalBackend

    monkeypatch.setenv("UNLISTED_SECRET", "hidden")
    monkeypatch.setenv("FORWARD_ME", "forwarded")
    code = "import os,json; print(json.dumps(dict(os.environ)))"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    backend = LocalBackend(env={"EXPLICIT": "set"}, forward_env=("FORWARD_ME",))
    result = await backend.run(command, cwd=str(tmp_path), timeout_s=2)
    env = json.loads(result.stdout)
    assert "UNLISTED_SECRET" not in env and env["FORWARD_ME"] == "forwarded"
    assert env["EXPLICIT"] == "set" and env["PATH"] == os.environ["PATH"]


def test_environment_fingerprint(monkeypatch):
    from linch.tools.execution import LocalBackend

    assert LocalBackend().resume_policy_version == "2"
    assert LocalBackend().resume_policy_config
    with pytest.raises(ValueError, match="resume_fingerprint_key"):
        _ = LocalBackend(env={"TOKEN": "secret"}).resume_policy_config
    monkeypatch.setenv("FORWARD_ME", "first")
    backend = LocalBackend(
        env={"TOKEN": "secret"}, forward_env=("FORWARD_ME",), resume_fingerprint_key=b"k" * 16
    )
    first = backend.resume_policy_config
    assert "secret" not in json.dumps(first)
    monkeypatch.setenv("FORWARD_ME", "second")
    assert backend.resume_policy_config != first


async def test_scheduler_timeout_keeps_bash_partial_output(tmp_path):
    from linch import BashTool, ToolCallEndEvent, ToolRegistry, ToolUseBlock
    from linch.scheduler import execute_tool_calls
    from tests.support.scheduler import scheduler_agent, scheduler_session

    registry = ToolRegistry()
    registry.register(BashTool())
    agent = scheduler_agent(registry, cwd=str(tmp_path), tool_timeout_ms=1200)
    events = [
        e
        async for e in execute_tool_calls(
            [ToolUseBlock(id="shell", name="Bash", input={"command": "echo partial; sleep 5"})],
            agent,
            scheduler_session(),
            __import__("linch.abort", fromlist=["AbortContext"]).AbortContext(),
        )
    ]
    result = next(e.tool_result for e in events if isinstance(e, ToolCallEndEvent))
    assert result.is_error and "partial" in result.content and "timed out" in result.content


async def test_bash_rendering_and_spilled_output_not_reoffloaded(tmp_path):
    from linch import BashTool, ToolContext
    from linch.filesystem import OffloadConfig, StateFileBackend
    from linch.filesystem.offload import maybe_offload

    result = await BashTool().execute(
        {"command": "yes | head -c 40000"},
        ToolContext(cwd=str(tmp_path), session_id="s", run_id="r", session_store=None),
    )
    assert result.truncated and "truncated" in result.content
    assert result.metadata["stdout_path"] in result.content
    assert "Exited 0" in result.content
    content = result.content
    await maybe_offload(
        result,
        tool_name="Bash",
        call_id="c",
        backend=StateFileBackend(),
        config=OffloadConfig(threshold_tokens=1),
    )
    assert result.content == content


def test_300m_output_rss_growth_under_50mb(tmp_path):
    # Isolated interpreter gives a meaningful high-water RSS comparison.
    import subprocess

    code = f"""
import asyncio, resource
from linch.tools.execution import LocalBackend
async def main():
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = await LocalBackend().run("yes | head -c 300M", cwd={str(tmp_path)!r}, timeout_s=15)
    growth = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    assert growth < 50 * 1024, growth
    assert len(result.stdout.encode()) < 30100 and result.truncated
    assert result.stdout_path is None
asyncio.run(main())
"""
    process = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=25
    )
    assert process.returncode == 0, process.stderr


def test_utf8_does_not_bridge_dropped_gap(tmp_path):
    stream = output(tmp_path)
    stream.append(b"a" * 3999 + b"\xc2" + b"x" * 1000 + b"\xa2" + b"z" * 25999)
    assert "¢" not in stream.snapshot()
    text, lossy = stream.read()
    assert lossy and "¢" not in text
    stream.finish()


def test_spill_write_and_close_failures_are_best_effort(tmp_path, monkeypatch):
    from linch.tools import _output

    class BrokenFile:
        def write(self, data):
            raise OSError("disk failed")

        def close(self):
            raise OSError("close failed")

    real_fdopen = _output.os.fdopen

    def broken_fdopen(fd, *args, **kwargs):
        real_fdopen(fd, *args, **kwargs).close()
        return BrokenFile()

    monkeypatch.setattr(_output.os, "fdopen", broken_fdopen)
    stream = output(tmp_path)
    stream.append(b"a" * 50000)
    stream.finish()
    assert (
        stream.path is None
        and stream.snapshot() == "a" * 4000 + "\n… (20000 bytes omitted)\n" + "a" * 26000
    )


@pytest.mark.parametrize("first_cause", ["timeout", "abort"])
async def test_first_cause_survives_outer_deadline_and_later_abort(tmp_path, first_cause):
    from linch.abort import AbortContext

    abort = AbortContext()
    started = time.monotonic()
    task = asyncio.create_task(
        run(tmp_path, "trap '' TERM; echo partial; exec sleep 5", timeout_s=0.12, signal=abort)
    )
    await asyncio.sleep(0.05 if first_cause == "abort" else 0.18)
    abort.abort()
    result = await asyncio.wait_for(task, timeout=0.05)
    assert result.timed_out is (first_cause == "timeout")
    assert result.aborted is (first_cause == "abort")
    assert "partial" in result.stdout and result.signal == signal.SIGKILL
    assert 3 <= time.monotonic() - started < 4.5


async def test_external_cancellation_without_selected_cause_propagates(tmp_path):
    task = asyncio.create_task(run(tmp_path, "echo partial; sleep 5", timeout_s=5))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("selected_timeout", [True, False])
async def test_repeated_cancellation_finishes_owned_group_cleanup(tmp_path, selected_timeout):
    from linch.tools.execution import LocalBackend

    class CapturingBackend(LocalBackend):
        async def start(self, command, *, cwd):
            self.handle = await super().start(command, cwd=cwd)
            return self.handle

    backend = CapturingBackend()
    task = asyncio.create_task(
        backend.run(
            "trap '' TERM; echo ready; exec sleep 5",
            cwd=str(tmp_path),
            timeout_s=0.1 if selected_timeout else 10,
        )
    )
    try:
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        if selected_timeout:
            result = await task
            assert result.timed_out and not result.aborted and result.stdout == "ready\n"
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert backend.handle.proc.returncode == -signal.SIGKILL
        assert backend.handle._done.done()
    finally:
        try:
            os.killpg(backend.handle.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await backend.handle.wait(None)


@pytest.mark.parametrize("short_on_write", [1, 2])
def test_short_spill_write_is_discarded(tmp_path, monkeypatch, short_on_write):
    from linch.tools import _output

    real_fdopen = _output.os.fdopen

    class ShortFile:
        def __init__(self, actual):
            self.actual = actual
            self.writes = 0

        def write(self, data):
            self.writes += 1
            return self.actual.write(
                data[: len(data) // 2] if self.writes == short_on_write else data
            )

        def close(self):
            self.actual.close()

    monkeypatch.setattr(
        _output.os, "fdopen", lambda *args, **kwargs: ShortFile(real_fdopen(*args, **kwargs))
    )
    stream = output(tmp_path)
    stream.append(b"h" * 30000)
    stream.append(b"t" * 10000)
    stream.finish()
    assert stream.path is None
    assert not list((tmp_path / ".linch" / "output").glob("*.log"))
    assert stream.snapshot() == (
        "h" * 4000 + "\n… (10000 bytes omitted)\n" + "h" * 16000 + "t" * 10000
    )


def test_shell_module_imports_without_posix_sigkill():
    import subprocess

    checked = subprocess.run(
        [
            sys.executable,
            "-c",
            "import signal; del signal.SIGKILL; from linch.tools.execution import LocalBackend",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
