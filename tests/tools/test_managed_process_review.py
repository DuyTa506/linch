"""Regression coverage for E4 review failures at actual ownership boundaries."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.tools.test_managed_processes import context, launch, process


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_backend_rebind_preserves_bash_limits_through_agent(tmp_path, worker):
    from linch import BashTool, LocalExecutionBackend, empty_tools

    supplied = BashTool(max_processes=1, max_runtime_ms=70)
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        tools=empty_tools(supplied),
        execution_backend=LocalExecutionBackend(cwd=tmp_path),
    )
    try:
        if worker:
            agent._set_subagent_tool_registry(empty_tools(supplied))
        registry = agent._subagent_tool_registry if worker else agent.tools
        effective = registry.get("Bash")
        assert effective.max_processes == 1
        assert effective.max_runtime_ms == 70
        ctx, processes = await context(tmp_path)
        try:
            yielded = await effective.execute(
                effective.validate({"command": "sleep 30", "timeout_ms": 10}), ctx
            )
            process_id = yielded.metadata["process_id"]
            result = await processes.ids[process_id].handle.wait(0.5)
            assert result is not None and processes.ids[process_id].timed_out
        finally:
            await processes.close()
    finally:
        await agent.close()


@pytest.mark.parametrize("failure", ["kill", "unlink"])
async def test_cleanup_attempts_every_entry_and_can_retry(tmp_path, monkeypatch, failure):
    from linch.tools.execution import ExecResult
    from linch.tools.processes import ProcessRegistry, _Process

    failed = True
    spill = tmp_path / "spill.log"
    spill.write_text("output")

    class Handle:
        def __init__(self, first=False):
            self.first, self.kills, self.running = first, 0, True

        async def kill(self):
            self.kills += 1
            if self.first and failed and failure == "kill":
                raise OSError("kill failed")
            self.running = False

        async def wait(self, timeout):
            return ExecResult("", "", 0, False, stdout_path=str(spill) if self.first else None)

    original = Path.unlink

    def unlink(path, **kwargs):
        if path == spill and failed and failure == "unlink":
            raise OSError("unlink failed")
        return original(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    first, second = Handle(True), Handle()
    registry = ProcessRegistry("r", entries=[_Process(first), _Process(second)])
    with pytest.raises((OSError, ExceptionGroup)):
        await registry.close()
    assert second.kills == 1 and not second.running
    failed = False
    await registry.close()
    assert not first.running and not spill.exists()


@pytest.mark.parametrize("failure", ["kill", "unlink"])
@pytest.mark.parametrize("overlapping", [False, True])
async def test_failed_cleanup_retains_admission_until_forced_retry(
    tmp_path, monkeypatch, failure, overlapping
):
    from linch import ConfigError, empty_tools, tool
    from linch.tools.execution import ExecResult
    from linch.tools.processes import _Process
    from tests.support.providers import ToolUseTurn

    failed = True
    spill = tmp_path / "spill.log"
    spill.write_text("data")
    owner = SimpleNamespace(session=None, running=True, later_running=True)
    entered, release = asyncio.Event(), asyncio.Event()
    if not overlapping:
        release.set()
    closing = None

    class Handle:
        def __init__(self, first):
            self.first = first

        async def kill(self):
            if self.first and failed:
                entered.set()
                await release.wait()
            if self.first and failed and failure == "kill":
                raise OSError("kill failed")
            if self.first:
                owner.running = False
            else:
                owner.later_running = False

        async def wait(self, timeout):
            return ExecResult("", "", 0, False, stdout_path=str(spill) if self.first else None)

    original = Path.unlink

    def unlink(path, **kwargs):
        if path == spill and failed and failure == "unlink":
            raise OSError("unlink failed")
        return original(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    @tool(scope="exec")
    async def register_handles() -> str:
        owner.session._processes.entries.extend([_Process(Handle(True)), _Process(Handle(False))])
        return "registered"

    provider = ScriptedProvider([ToolUseTurn("register_handles", {}), TextTurn("done")])
    agent = make_agent(provider, cwd=str(tmp_path), tools=empty_tools(register_handles))
    session = owner.session = await agent.session()

    async def consume():
        return [e async for e in session.run("go")]

    try:
        running = asyncio.create_task(consume())
        if overlapping:
            await entered.wait()
            closing = asyncio.create_task(session.aclose(force=True))
            await asyncio.sleep(0.01)
            release.set()
        with pytest.raises((OSError, ExceptionGroup)):
            await running
        if closing is not None:
            with pytest.raises(ExceptionGroup):
                await asyncio.wait_for(closing, 0.2)
        assert session._active and not session._idle.is_set()
        assert not owner.later_running
        old_registry = session._processes
        with pytest.raises(ConfigError):
            session.run("must not replace registry")
        assert session._processes is old_registry
        failed = False
        await asyncio.wait_for(session.aclose(force=True), 1)
        assert not session._active and session._idle.is_set()
        assert not owner.running and not spill.exists()
    finally:
        failed = False
        if session._close_task is not None and not session._close_task.done():
            session._close_task.cancel()
            await asyncio.gather(session._close_task, return_exceptions=True)
        await agent.close()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux /proc")
async def test_foreground_live_group_consumes_process_cap(tmp_path):
    ctx, registry = await context(tmp_path)
    try:
        foreground = await launch(
            ctx, "sleep 30 & echo $! > child.pid; echo ready", max_processes=1
        )
        assert not foreground.is_error and "ready" in foreground.content
        pid = int((tmp_path / "child.pid").read_text())
        assert Path(f"/proc/{pid}").exists()
        await asyncio.sleep(0.3)
        overflow = await launch(ctx, "sleep 30", max_processes=1)
        assert overflow.is_error and "timed out" in overflow.content
    finally:
        await registry.close()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux /proc")
async def test_completed_foreground_overflow_group_is_killed(tmp_path):
    ctx, registry = await context(tmp_path)
    try:
        await launch(ctx, "sleep 30", max_processes=1)
        from linch import BashTool

        tool = BashTool(max_processes=1)
        overflow = await tool.execute(
            tool.validate(
                {
                    "command": "sleep 30 & echo $! > overflow.pid; echo ready",
                    "timeout_ms": 1000,
                }
            ),
            ctx,
        )
        pid = int((tmp_path / "overflow.pid").read_text())
        stat = Path(f"/proc/{pid}/stat")
        assert overflow.is_error
        assert not stat.exists() or stat.read_text().split()[2] == "Z"
    finally:
        await registry.close()


async def test_late_failed_start_cleanup_can_be_retried_after_prior_success(tmp_path):
    from linch import BashTool
    from linch.tools.execution import ExecResult

    ctx, registry = await context(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    failed = True

    class Handle:
        running = True

        async def kill(self):
            if failed:
                raise OSError("late kill failed")
            self.running = False

        async def wait(self, timeout):
            return ExecResult("", "", 0, False)

    handle = Handle()

    class Delayed:
        async def start(self, command, *, cwd):
            entered.set()
            await release.wait()
            return handle

    tool = BashTool(execution=SimpleNamespace(shell=Delayed()))
    task = asyncio.create_task(tool.execute(tool.validate({"command": "unused"}), ctx))
    await entered.wait()
    await registry.close()
    release.set()
    with pytest.raises(ExceptionGroup):
        await task
    failed = False
    await registry.close()
    assert not handle.running


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_start_cap_fallback_supports_handles_without_private_group_probe(tmp_path):
    from linch import BashTool
    from linch.tools.execution import LocalBackend

    class Handle:
        def __init__(self, inner):
            self.inner = inner

        def read(self):
            return self.inner.read()

        async def wait(self, timeout):
            return await self.inner.wait(timeout)

        async def kill(self):
            await self.inner.kill()

    class Backend:
        async def start(self, command, *, cwd):
            return Handle(await LocalBackend().start(command, cwd=cwd))

    ctx, registry = await context(tmp_path)
    tool = BashTool(execution=SimpleNamespace(shell=Backend()), max_processes=1)
    try:
        first = await tool.execute(tool.validate({"command": "sleep 30", "timeout_ms": 20}), ctx)
        first_id = first.metadata["process_id"]
        assert first_id in registry.ids
        overflow = await tool.execute(tool.validate({"command": "sleep 30", "timeout_ms": 20}), ctx)
        assert overflow.is_error and "timed out" in overflow.content
    finally:
        await registry.close()


async def test_inherited_execution_world_preserves_supplied_process_limits(tmp_path):
    from linch import BashTool, Context, LocalExecutionBackend, empty_tools

    host = Context(label="host")
    host.register("execution", LocalExecutionBackend(cwd=tmp_path))
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        context=host,
        tools=empty_tools(BashTool(max_processes=1, max_runtime_ms=70)),
    )
    try:
        assert agent.tools.get("Bash").max_processes == 1
        assert agent.tools.get("Bash").max_runtime_ms == 70
    finally:
        await agent.close()
        await host.dispose()


@pytest.mark.parametrize("failure", ["kill", "unlink"])
async def test_delayed_start_failure_reaches_overlapping_force_close(
    tmp_path, monkeypatch, failure
):
    from linch import BashTool, ConfigError, empty_tools
    from linch.tools.execution import ExecResult
    from linch.tools.processes import ProcessRegistry
    from tests.support.providers import ToolUseTurn

    entered, release = asyncio.Event(), asyncio.Event()
    early_closed = asyncio.Event()
    original_close = ProcessRegistry.close

    async def observe_close(registry):
        await original_close(registry)
        early_closed.set()

    monkeypatch.setattr(ProcessRegistry, "close", observe_close)
    failed = True
    spill = tmp_path / "late-spill.log"
    spill.write_text("output")

    class Handle:
        running = True

        async def kill(self):
            if failed and failure == "kill":
                raise OSError("late kill failed")
            self.running = False

        async def wait(self, timeout):
            return ExecResult("", "", 0, False, stdout_path=str(spill))

    handle = Handle()

    class Backend:
        async def start(self, command, *, cwd):
            entered.set()
            await release.wait()
            return handle

    original = Path.unlink

    def unlink(path, **kwargs):
        if path == spill and failed and failure == "unlink":
            raise OSError("late unlink failed")
        return original(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    provider = ScriptedProvider([ToolUseTurn("Bash", {"command": "delayed"}), TextTurn("done")])
    agent = make_agent(
        provider,
        cwd=str(tmp_path),
        run_store=None,
        tools=empty_tools(BashTool(execution=SimpleNamespace(shell=Backend()))),
    )
    session = await agent.session()

    async def consume():
        return [e async for e in session.run("go")]

    running = asyncio.create_task(consume())
    await entered.wait()
    closing = asyncio.create_task(session.aclose(force=True))
    try:
        # Wait for the early empty close to finish; it must still await the iterator.
        await asyncio.wait_for(early_closed.wait(), 1)
        assert session._processes.cleaned
        assert not closing.done()
        release.set()
        with pytest.raises(ExceptionGroup):
            await running
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(closing, 0.3)
        assert session._active and not session._idle.is_set()
        with pytest.raises(ConfigError):
            session.run("must not overwrite failed registry")
        failed = False
        await asyncio.wait_for(session.aclose(force=True), 1)
        assert not session._active and session._idle.is_set()
        assert not handle.running and not spill.exists()
    finally:
        failed = False
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        if session._close_task is not None and not session._close_task.done():
            session._close_task.cancel()
            await asyncio.gather(session._close_task, return_exceptions=True)
        await agent.close()


async def test_limit_does_not_kill_reaped_handle():
    from linch.tools.execution import ExecResult
    from linch.tools.processes import _Process

    class Handle:
        killed = False

        async def wait(self, timeout):
            return ExecResult("", "", 0, False)

        async def kill(self):
            self.killed = True

    handle = Handle()
    await _Process(handle).limit(0, None)
    assert not handle.killed


async def test_finished_history_is_bounded_and_spills_removed(tmp_path):
    from linch.tools.base import ToolContext
    from linch.tools.execution import ExecResult, ShellRead
    from linch.tools.processes import ProcessRegistry

    class Handle:
        _group_alive = False

        def __init__(self, index):
            self.path = tmp_path / str(index)
            self.path.write_text("spill")

        async def wait(self, timeout):
            return ExecResult("", "", 0, False, stdout_path=str(self.path))

        async def kill(self):
            pass

        def read(self):
            return ShellRead()

    class Backend:
        count = 0

        async def start(self, command, *, cwd):
            self.count += 1
            return Handle(self.count)

    class Tool:
        _backend = Backend()
        max_processes = 1
        max_runtime_ms = 10000

    registry = ProcessRegistry("r")
    ctx = ToolContext(str(tmp_path), "s", "r", None)
    for _ in range(300):
        await registry.run(Tool(), {"command": "x"}, ctx, 0)
    assert len(registry.entries) <= 128
    await registry.close()
    assert not list(tmp_path.iterdir())


async def test_recent_finished_ids_are_retained_and_old_ids_evicted(tmp_path):
    from linch.tools.base import ToolContext
    from linch.tools.execution import ExecResult, ShellRead
    from linch.tools.processes import ProcessRegistry

    class Handle:
        _group_alive = False
        calls = 0

        async def wait(self, timeout):
            self.calls += 1
            return None if self.calls <= 2 else ExecResult("", "", 0, False)

        async def kill(self):
            pass

        def read(self):
            return ShellRead()

    class Backend:
        async def start(self, command, *, cwd):
            return Handle()

    class Tool:
        _backend = Backend()
        max_processes = 1000
        max_runtime_ms = 10000

    registry = ProcessRegistry("r")
    ctx = ToolContext(str(tmp_path), "s", "r", None)
    ids = []
    for _ in range(140):
        result = await registry.run(Tool(), {"command": "x", "run_in_background": True}, ctx, 0)
        ids.append(result.metadata["process_id"])
    await registry._retire(registry.entries[-1], await registry.entries[-1].handle.wait(0))
    registry._trim_history()
    assert ids[0] not in registry.ids and ids[-1] in registry.ids
    assert len(registry.ids) <= 128
    await registry.close()


@pytest.mark.parametrize("trigger", ["timeout", "abort"])
async def test_limit_does_not_kill_exited_local_leader_with_pending_readers(trigger):
    from linch.tools.execution import ExecResult, ShellRead, _LocalProcess
    from linch.tools.processes import _Process, _render

    class Local(_LocalProcess):
        def __init__(self):
            self.proc = SimpleNamespace(returncode=0)
            self.killed = False
            self.readers_done = False

        async def wait(self, timeout):
            return ExecResult("", "", 0, False) if self.readers_done else None

        async def kill(self):
            self.killed = True

        def read(self):
            return ShellRead()

    handle = Local()
    entry = _Process(handle)
    signal = asyncio.Event()
    signal.set()
    await entry.limit(0 if trigger == "timeout" else 1, None if trigger == "timeout" else signal)
    assert not handle.killed and not entry.stopped
    assert not entry.timed_out and not entry.aborted
    handle.readers_done = True
    assert not (await _render(entry, "p1234abcd", 0)).is_error


async def test_capacity_kills_retire_stopped_groups_despite_stale_probe(tmp_path):
    from linch.tools.base import ToolContext
    from linch.tools.execution import ExecResult, ShellRead
    from linch.tools.processes import ProcessRegistry

    class Handle:
        _group_alive = True
        stopped = False

        async def wait(self, timeout):
            return ExecResult("", "", 0, False) if self.stopped else None

        async def kill(self):
            self.stopped = True

        def read(self):
            return ShellRead()

    class Backend:
        async def start(self, command, *, cwd):
            return Handle()

    class Tool:
        _backend = Backend()
        max_processes = 1
        max_runtime_ms = 10000

        @staticmethod
        def _render(result):
            from linch.tools.builtin import BashTool

            return BashTool._render(result)

    registry = ProcessRegistry("r")
    ctx = ToolContext(str(tmp_path), "s", "r", None)
    try:
        await registry.run(Tool(), {"command": "x"}, ctx, 0)
        for _ in range(150):
            await registry.run(Tool(), {"command": "x"}, ctx, 0)
        assert len(registry.entries) == 1
    finally:
        await registry.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
def test_group_signal_permission_error_is_not_reported_as_success(monkeypatch):
    from linch.tools.execution import _kill_process_group

    def denied(*args):
        raise PermissionError("denied")

    monkeypatch.setattr("linch.tools.execution.os.killpg", denied)
    with pytest.raises(PermissionError):
        _kill_process_group(SimpleNamespace(pid=123, returncode=0))


async def test_live_group_with_exited_leader_remains_owned(tmp_path):
    from linch.tools.execution import ExecResult
    from linch.tools.processes import ProcessRegistry, _Process

    class Handle:
        _group_alive = True

        async def wait(self, timeout):
            return ExecResult("", "", 0, False)

        async def kill(self):
            pass

    entry = _Process(Handle())
    registry = ProcessRegistry("r", entries=[entry])
    assert not await registry._retire(entry, await entry.handle.wait(0))
    assert registry.entries == [entry]
    await registry.close()


async def test_failed_kill_keeps_entry_for_retry(tmp_path):
    from linch.tools.execution import ExecResult
    from linch.tools.processes import ProcessRegistry, _Process

    class Handle:
        _group_alive = True
        fail = True

        async def wait(self, timeout):
            return ExecResult("", "", 0, False)

        async def kill(self):
            if self.fail:
                raise PermissionError("denied")

    handle = Handle()
    entry = _Process(handle)
    registry = ProcessRegistry("r", entries=[entry])
    with pytest.raises(PermissionError):
        await entry.stop()
    assert not entry.stopped
    assert not await registry._retire(entry, await handle.wait(0))
    with pytest.raises(ExceptionGroup):
        await registry.close()
    handle.fail = False
    await registry.close()
    assert registry.cleaned


@pytest.mark.parametrize("method", ["timer", "user"])
async def test_successful_stop_retires_stale_group_for_timer_and_user(tmp_path, method):
    from linch.tools.base import ToolContext
    from linch.tools.execution import ExecResult, ShellRead
    from linch.tools.processes import BashProcessTool, ProcessRegistry, _Process

    class Handle:
        _group_alive = True
        stopped = False

        async def wait(self, timeout):
            return ExecResult("", "", 0, False) if self.stopped else None

        async def kill(self):
            self.stopped = True

        def read(self):
            return ShellRead()

    entry = _Process(Handle())
    registry = ProcessRegistry("r", entries=[entry], ids={"p1234abcd": entry})
    if method == "timer":
        await entry.limit(0, None)
        await registry._retire(entry, await entry.handle.wait(0))
    else:
        ctx = ToolContext(
            str(tmp_path), "s", "r", None, _session=SimpleNamespace(_processes=registry)
        )
        await BashProcessTool().execute({"action": "kill", "id": "p1234abcd"}, ctx)
    assert not registry.entries and registry.ids["p1234abcd"] is entry
    await registry.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_kill_keeps_the_failure_of_an_already_finished_process(tmp_path):
    ctx, registry = await context(tmp_path)
    try:
        started = await launch(ctx, "sleep .2; exit 3")
        id = started.metadata["process_id"]
        await registry.ids[id].handle.wait(None)
        result = await process(ctx, "kill", id=id)
        assert result.is_error
    finally:
        await registry.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_kill_of_a_running_process_is_not_an_error(tmp_path):
    ctx, registry = await context(tmp_path)
    try:
        id = (await launch(ctx, "sleep 30")).metadata["process_id"]
        assert not (await process(ctx, "kill", id=id)).is_error
    finally:
        await registry.close()


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux /proc")
async def test_kill_still_stops_a_group_outliving_its_leader(tmp_path):
    ctx, registry = await context(tmp_path)
    try:
        started = await launch(ctx, "sleep 30 & echo $! > child.pid; sleep .2")
        id = started.metadata["process_id"]
        await registry.ids[id].handle.wait(None)
        pid = int((tmp_path / "child.pid").read_text())
        result = await process(ctx, "kill", id=id)
        assert not result.is_error
        for _ in range(100):
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("group child survived kill")
    finally:
        await registry.close()


async def test_background_at_capacity_is_refused_before_spawning(tmp_path):
    from linch.tools.base import ToolContext
    from linch.tools.execution import ExecResult, ShellRead
    from linch.tools.processes import ProcessRegistry

    class Handle:
        async def wait(self, timeout):
            return None if timeout == 0 else ExecResult("", "", 0, False)

        def read(self):
            return ShellRead("", "", False, None, None)

        async def kill(self):
            pass

    class Backend:
        starts = 0

        async def start(self, command, *, cwd):
            Backend.starts += 1
            return Handle()

    class Tool:
        _backend = Backend()
        max_processes = 1
        max_runtime_ms = 10000

    registry = ProcessRegistry("r")
    ctx = ToolContext(str(tmp_path), "s", "r", None)
    command = {"command": "x", "run_in_background": True}
    try:
        await registry.run(Tool(), command, ctx, 0)
        refused = await registry.run(Tool(), command, ctx, 0)
        assert refused.is_error and "BashProcess" in refused.content
        assert Backend.starts == 1
    finally:
        await registry.close()
