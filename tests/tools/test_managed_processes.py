"""Managed shell processes retain output and terminate with their owning run."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from types import SimpleNamespace

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


def python(code):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}"


async def context(tmp_path):
    from linch.tools.base import ToolContext
    from linch.tools.processes import ProcessRegistry

    registry = ProcessRegistry("r")
    session = SimpleNamespace(_processes=registry)
    ctx = ToolContext(str(tmp_path), "s", "r", None, _session=session)
    return ctx, registry


async def launch(ctx, command, **options):
    from linch import BashTool

    tool = BashTool(**options)
    return await tool.execute(tool.validate({"command": command, "timeout_ms": 25}), ctx)


async def process(ctx, action="wait", id=None, wait_ms=1000):
    from linch import BashProcessTool

    if id is None:
        id = next(iter(ctx._session._processes.ids), "stale-process-id")
    tool = BashProcessTool()
    return await tool.execute(tool.validate({"id": id, "action": action, "wait_ms": wait_ms}), ctx)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_background_flag_returns_immediately_and_kill_works(tmp_path):
    from linch import BashTool

    ctx, registry = await context(tmp_path)
    try:
        tool = BashTool()
        result = await tool.execute(
            tool.validate({"command": "sleep 30", "run_in_background": True}), ctx
        )
        process_id = result.metadata["process_id"]
        killed = await process(ctx, "kill", id=process_id)
        assert not killed.is_error and "signal" in killed.content
        assert "Exited" in (await process(ctx, "status", id=process_id)).content
    finally:
        await registry.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_quiet_waits_suggest_stopping_and_clamp_timeout(tmp_path):
    ctx, registry = await context(tmp_path)
    try:
        process_id = (await launch(ctx, "sleep 30")).metadata["process_id"]
        ctx.tool_timeout_s = 0.1
        for _ in range(3):
            result = await asyncio.wait_for(process(ctx, id=process_id, wait_ms=999999), 0.2)
        assert "stop" in result.content.lower()
    finally:
        await registry.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_missing_and_previous_run_ids_are_gone(tmp_path):
    ctx, registry = await context(tmp_path)
    try:
        assert (await process(ctx)).is_error
        await launch(ctx, "sleep 30")
        ctx.run_id = "next-run"
        assert (await process(ctx)).is_error
    finally:
        await registry.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_run_end_cleans_spills_before_result_and_reuse(tmp_path):
    from linch import BashProcessTool, BashTool, ResultEvent, empty_tools

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                "Bash",
                {
                    "command": python("print('x'*40000); import time; time.sleep(30)"),
                    "timeout_ms": 100,
                },
            ),
            TextTurn("done"),
            TextTurn("again"),
        ]
    )
    agent = make_agent(
        provider, cwd=str(tmp_path), tools=empty_tools(BashTool(), BashProcessTool())
    )
    session = await agent.session()
    try:
        async for event in session.run("go"):
            if isinstance(event, ResultEvent):
                assert not list((tmp_path / ".linch/output").glob("*.log"))
                assert not session._active
        assert [e async for e in session.run("again")][-1].type == "result"
    finally:
        await agent.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_closed_registry_rejects_old_detached_start(tmp_path):
    ctx, registry = await context(tmp_path)
    await registry.close()
    result = await launch(ctx, "touch should-not-exist")
    assert result.is_error
    assert not (tmp_path / "should-not-exist").exists()


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "skip-dangerous"])
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_process_control_is_allowed_without_prompt_and_not_cached(
    tmp_path, mode, monkeypatch
):
    import linch.tools.processes as processes_module
    from linch import BashProcessTool, BashTool, ToolCacheConfig, empty_tools

    monkeypatch.setattr(
        processes_module,
        "uuid4",
        lambda: SimpleNamespace(hex="00000001000000000000000000000000"),
    )

    provider = ScriptedProvider(
        [
            ToolUseTurn("Bash", {"command": "sleep .15", "timeout_ms": 20}, tool_id="b"),
            ToolUseTurn(
                "BashProcess", {"id": "p00000001", "action": "wait", "wait_ms": 0}, tool_id="w1"
            ),
            ToolUseTurn(
                "BashProcess",
                {"id": "p00000001", "action": "wait", "wait_ms": 1000},
                tool_id="w2",
            ),
            TextTurn("done"),
        ]
    )
    approvals = []

    async def approve(request):
        approvals.append(request.tool_name)
        return {"behavior": "allow"}

    agent = make_agent(
        provider,
        cwd=str(tmp_path),
        run_store=None,
        tools=empty_tools(BashTool(), BashProcessTool()),
        permissions={"mode": mode, "can_use_tool": approve},
        tool_cache=ToolCacheConfig(allow={"BashProcess"}),
    )
    try:
        events = [e async for e in (await agent.session()).run("go")]
        results = [e for e in events if e.type == "tool_call_end"]
        assert "still running" in results[1].result
        assert "Exited 0" in results[2].result
        assert "BashProcess" not in approvals
    finally:
        await agent.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_cancelled_cleanup_finishes_killing_before_releasing_admission(tmp_path):
    from linch import BashTool, empty_tools

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                "Bash", {"command": "trap '' TERM; echo ready; sleep 30", "timeout_ms": 30}
            ),
            TextTurn("done"),
        ]
    )
    agent = make_agent(provider, cwd=str(tmp_path), tools=empty_tools(BashTool()))
    session = await agent.session()
    task = asyncio.create_task(_consume(session))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.sleep(0.05)
    assert session._active
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session._active
    assert all([await e.handle.wait(0) is not None for e in session._processes.entries])
    await agent.close()


async def _consume(session):
    return [e async for e in session.run("go")]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_child_run_has_its_own_process_ids_and_cap(tmp_path):
    from linch import BashProcessTool, BashTool, empty_tools
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools.base import ToolContext
    from linch.tools.processes import ProcessRegistry

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                "BashProcess", {"action": "status", "id": "stale-process-id"}, tool_id="status"
            ),
            ToolUseTurn("Bash", {"command": "sleep 30", "timeout_ms": 25}, tool_id="start"),
            ToolUseTurn(
                "BashProcess", {"action": "kill", "id": "stale-process-id"}, tool_id="kill"
            ),
            TextTurn("done"),
        ]
    )
    agent = make_agent(
        provider, cwd=str(tmp_path), tools=empty_tools(BashTool(max_processes=1), BashProcessTool())
    )
    parent = await agent.session()
    parent._processes = ProcessRegistry("r")
    ctx = ToolContext(str(tmp_path), parent.id, "r", parent.store, _session=parent)
    await launch(ctx, "sleep 30", max_processes=1)
    try:
        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="test",
                display_name="child",
                subagent_run_id="child-r",
                retain=True,
            )
        )
        child = agent._sessions[result.child_session_id]
        assert child._processes is not parent._processes
        child_process_id = next(iter(child._processes.ids))
        assert child_process_id.startswith("p") and len(child_process_id) == 9
        assert not child._processes.ids[child_process_id].timed_out
        parent_process_id = next(iter(parent._processes.ids))
        assert await parent._processes.ids[parent_process_id].handle.wait(0) is None
        tool_results = [
            b.content for m in child.provider_view for b in m.content if b.type == "tool_result"
        ]
        assert any("gone" in str(value) for value in tool_results)
    finally:
        await agent.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_late_start_after_cleanup_is_killed_and_spill_removed(tmp_path):
    from linch import BashTool
    from linch.tools.execution import LocalBackend

    ctx, registry = await context(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class Delayed:
        async def start(self, command, *, cwd):
            entered.set()
            await release.wait()
            handle = await LocalBackend().start(command, cwd=cwd)
            await asyncio.sleep(0.05)
            return handle

    tool = BashTool(execution=SimpleNamespace(shell=Delayed()))
    task = asyncio.create_task(
        tool.execute(
            tool.validate({"command": python("print('x'*40000); import time; time.sleep(30)")}), ctx
        )
    )
    await entered.wait()
    await registry.close()
    release.set()
    assert (await task).is_error
    assert not list((tmp_path / ".linch/output").glob("*.log"))
    assert await registry.entries[0].handle.wait(0) is not None


@pytest.mark.parametrize("tool_name", ["Bash", "BashProcess"])
async def test_managed_process_schema_changes_block_resume(tmp_path, tool_name):
    import copy

    from linch import BashProcessTool, BashTool, ConfigError, empty_tools
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    run_store, store = InMemoryRunStore(), InMemorySessionStore()
    tool = BashTool() if tool_name == "Bash" else BashProcessTool()
    tool.input_schema = copy.deepcopy(tool.input_schema)
    del tool.input_schema["properties"]["run_in_background" if tool_name == "Bash" else "wait_ms"]
    old = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        tools=empty_tools(tool),
        run_store=run_store,
        session_store=store,
    )
    session = await old.session(id="schema")
    stream = session.run("go")
    first = await anext(stream)
    await stream.aclose()
    current_tool = BashTool() if tool_name == "Bash" else BashProcessTool()
    current = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        tools=empty_tools(current_tool),
        run_store=run_store,
        session_store=store,
    )
    try:
        with pytest.raises(ConfigError, match="input_schema"):
            _ = [e async for e in (await current.session(id="schema")).resume(first.run_id)]
    finally:
        await old.close()
        await current.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell or process groups")
async def test_bash_uses_managed_background_with_generic_background_enabled(tmp_path, monkeypatch):
    import linch.tools.processes as processes_module
    from linch import BashProcessTool, BashTool, empty_tools

    monkeypatch.setattr(
        processes_module,
        "uuid4",
        lambda: SimpleNamespace(hex="00000001000000000000000000000000"),
    )

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                "Bash", {"command": "sleep 30", "run_in_background": True}, tool_id="start"
            ),
            ToolUseTurn("BashProcess", {"id": "p00000001", "action": "kill"}, tool_id="kill"),
            TextTurn("done"),
        ]
    )
    agent = make_agent(
        provider,
        cwd=str(tmp_path),
        run_store=None,
        tools=empty_tools(BashTool(), BashProcessTool()),
        enable_background_tools=True,
    )
    try:
        events = [e async for e in (await agent.session()).run("go")]
        results = [e for e in events if e.type == "tool_call_end"]
        process_id = results[0].tool_result.metadata["process_id"]
        assert f"Process {process_id} still running" in results[0].result
        assert "gone" not in results[1].result
    finally:
        await agent.close()
