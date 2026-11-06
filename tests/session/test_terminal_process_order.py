"""Terminal status remains nonterminal until managed processes are cleaned up."""

import asyncio
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn


@pytest.mark.parametrize("outcome", ["completed", "aborted", "failed"])
@pytest.mark.parametrize("cleanup_fails", [False, True, "once"])
async def test_terminal_status_follows_process_cleanup(outcome, cleanup_fails):
    from linch import InMemoryRunStore, empty_tools, tool
    from linch.tools.execution import ExecResult
    from linch.tools.processes import _Process

    entered, release = asyncio.Event(), asyncio.Event()
    order = []
    failure = cleanup_fails
    session: Any = None

    class Store(InMemoryRunStore):
        async def save_checkpoint_with_events(self, *args, **kwargs):
            if kwargs.get("status") in {"completed", "aborted", "failed"}:
                order.append(kwargs["status"])
            return await super().save_checkpoint_with_events(*args, **kwargs)

        async def save_checkpoint(self, *args, **kwargs):
            if kwargs.get("status") in {"completed", "aborted", "failed"}:
                order.append(kwargs["status"])
            return await super().save_checkpoint(*args, **kwargs)

        async def mark_completed(self, *args, **kwargs):
            order.append("completed")
            return await super().mark_completed(*args, **kwargs)

        async def mark_failed(self, *args, **kwargs):
            order.append("failed")
            return await super().mark_failed(*args, **kwargs)

    class Handle:
        async def kill(self):
            nonlocal failure
            entered.set()
            await release.wait()
            if failure:
                if failure == "once":
                    failure = False
                raise OSError("kill failed")
            order.append("kill")

        async def wait(self, timeout):
            return ExecResult("", "", 0, False)

    @tool(scope="exec")  # pyright: ignore[reportCallIssue]
    async def register_handle() -> str:
        session._processes.entries.append(_Process(Handle()))
        if outcome == "aborted":
            session.abort()
        return "registered"

    turns: list[TextTurn | ToolUseTurn] = [ToolUseTurn("register_handle", {})]
    if outcome == "completed":
        turns.append(TextTurn("done"))
    store = Store()
    agent = make_agent(ScriptedProvider(turns), run_store=store, tools=empty_tools(register_handle))  # pyright: ignore[reportArgumentType]
    session = await agent.session()

    async def consume():
        return [event async for event in session.run("go")]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert session._active
        run_id = session.active_run_id
        record = await store.load_run(run_id)
        assert record is not None and record.status == "running"
        assert not any(item in {"completed", "aborted", "failed"} for item in order)
        release.set()
        if cleanup_fails:
            with pytest.raises(ExceptionGroup, match="Process cleanup failed") as caught:
                await task
            seen, cause = set(), caught.value
            while cause is not None:
                assert id(cause) not in seen, "exception cause chain has a cycle"
                seen.add(id(cause))
                cause = cause.__cause__
            assert session._active is (cleanup_fails is True)
            record = await store.load_run(run_id)
            assert record is not None and record.status == "running"
            assert not any(item in {"completed", "aborted", "failed"} for item in order)
        else:
            await task
            record = await store.load_run(run_id)
            assert record is not None and record.status == outcome
            assert order.index("kill") < order.index(outcome)
            assert not session._active
    finally:
        failure = False
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await agent.close()


async def test_abort_awaits_background_start_before_closing_registry():
    from linch import InMemoryRunStore, empty_tools, tool
    from linch.tools.execution import ExecResult
    from linch.tools.processes import _Process

    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order: list[str] = []
    session: Any = None

    class Store(InMemoryRunStore):
        async def save_checkpoint_with_events(self, *args, **kwargs):
            if kwargs.get("status") == "aborted":
                order.append("aborted")
            return await super().save_checkpoint_with_events(*args, **kwargs)

    class Handle:
        async def kill(self):
            order.append("kill")

        async def wait(self, timeout):
            return ExecResult("", "", 0, False)

    async def late_start():
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            session._processes.entries.append(_Process(Handle()))

    @tool(scope="exec")  # pyright: ignore[reportCallIssue]
    async def start_and_abort() -> str:
        task = asyncio.create_task(late_start())
        session.background_tasks.append(task)
        await started.wait()
        session._abort_controller.abort()
        return "started"

    store = Store()
    agent = make_agent(
        ScriptedProvider([ToolUseTurn("start_and_abort", {})]),
        run_store=store,
        tools=empty_tools(start_and_abort),  # pyright: ignore[reportArgumentType]
    )
    session = await agent.session()

    async def consume():
        return [event async for event in session.run("go")]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(cancelled.wait(), 2)
        assert not session._processes.cleaned
        assert not order
        release.set()
        await task
        assert order == ["kill", "aborted"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await agent.close()
