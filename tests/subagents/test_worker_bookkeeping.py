"""WorkerHandle bookkeeping for continued and stopped workers."""

from __future__ import annotations

from typing import Any

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


async def _retained_worker(agent: Any) -> tuple[Any, Any]:
    """Run one retained worker to completion and return its parent and handle."""
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.subagents.workers import WorkerHandle

    parent = await agent.session()
    first = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="first",
            display_name="w",
            subagent_run_id="sa_first",
            retain=True,
        )
    )
    handle = WorkerHandle(
        worker_id="agent-w",
        child_session_id=first.child_session_id,
        display_name="w",
        definition=DEFAULT_AGENT,
        status="completed",
    )
    parent.workers[handle.worker_id] = handle
    return parent, handle


async def test_continue_uses_the_supplied_subagent_run_id() -> None:
    from linch.events import SubagentEvent
    from linch.subagents.runner import ContinueSubagentArgs, continue_subagent

    agent = make_agent(ScriptedProvider([TextTurn("first"), TextTurn("second")]), model="m")
    try:
        parent, handle = await _retained_worker(agent)
        events: list[Any] = []
        result = await continue_subagent(
            ContinueSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                handle=handle,
                message="again",
                subagent_run_id="sa_cont_supplied",
                emit=events.append,
            )
        )
        assert not result.errored
        run_ids = {event.subagent_run_id for event in events if isinstance(event, SubagentEvent)}
        assert run_ids == {"sa_cont_supplied"}
    finally:
        await agent.close()


def _continue_args(parent: Any, agent: Any, handle: Any, run_id: str) -> Any:
    from linch.subagents.runner import ContinueSubagentArgs

    return ContinueSubagentArgs(
        parent_session=parent,
        parent_agent=agent,
        handle=handle,
        message="again",
        subagent_run_id=run_id,
    )


class _GatedProvider(ScriptedProvider):
    """Hold every call after the first until the gate opens."""

    def __init__(self, turns: list[Any]) -> None:
        import asyncio

        super().__init__(turns)
        self.gate = asyncio.Event()
        self.held = asyncio.Event()

    async def stream(self, req: Any) -> Any:
        if self.requests:
            self.held.set()
            await self.gate.wait()
        async for event in super().stream(req):
            yield event


async def test_continue_to_a_running_worker_is_refused_without_changing_status() -> None:
    import asyncio

    from linch.subagents.runner import continue_subagent

    provider = _GatedProvider([TextTurn("first"), TextTurn("second")])
    agent = make_agent(provider, model="m")
    try:
        parent, handle = await _retained_worker(agent)
        running = asyncio.create_task(
            continue_subagent(_continue_args(parent, agent, handle, "sa_running"))
        )
        await provider.held.wait()
        status_while_running = handle.status

        refused = await continue_subagent(_continue_args(parent, agent, handle, "sa_refused"))

        assert refused.errored
        assert refused.error is not None and refused.error["name"] == "WorkerBusy"
        assert handle.status == status_while_running
        provider.gate.set()
        finished = await running
        assert not finished.errored
        assert handle.status == "completed"
    finally:
        provider.gate.set()
        await agent.close()


async def test_two_continues_in_one_batch_run_one_and_refuse_the_other() -> None:
    import asyncio

    from linch.subagents.runner import continue_subagent

    class Hooks:
        resume_policy_id = "test.worker-bookkeeping"

        def __init__(self) -> None:
            self.started: list[str] = []

        async def on_subagent_start(self, ctx: Any) -> None:
            self.started.append(ctx.subagent_run_id)
            await asyncio.sleep(0)

    hooks = Hooks()
    provider = ScriptedProvider([TextTurn("first"), TextTurn("second"), TextTurn("third")])
    agent = make_agent(provider, model="m", hooks=[hooks])
    try:
        parent, handle = await _retained_worker(agent)
        hooks.started.clear()

        results = await asyncio.gather(
            continue_subagent(_continue_args(parent, agent, handle, "sa_a")),
            continue_subagent(_continue_args(parent, agent, handle, "sa_b")),
        )

        refused = [result for result in results if result.errored]
        assert len(refused) == 1
        assert refused[0].error is not None and refused[0].error["name"] == "WorkerBusy"
        assert len(hooks.started) == 1
        assert handle.status == "completed"
    finally:
        await agent.close()


async def _stop(session: Any, payload: dict[str, Any]) -> Any:
    from linch.tools.base import ToolContext
    from linch.tools.subagent_stop import TaskStopTool

    tool = TaskStopTool(get_session=lambda _session_id: session)
    ctx = ToolContext(cwd=".", session_id=session.id, run_id="r", session_store=session.store)
    return await tool.execute(tool.validate(payload), ctx)


async def test_task_stop_leaves_a_finished_worker_alone() -> None:
    agent = make_agent(ScriptedProvider([TextTurn("first")]), model="m")
    try:
        parent, handle = await _retained_worker(agent)

        result = await _stop(parent, {"task_id": handle.worker_id, "reason": "late"})

        assert not result.is_error
        assert "not running" in result.content
        assert handle.status == "completed"
        assert handle.stop_reason is None
    finally:
        await agent.close()


async def test_task_stop_records_the_reason_on_a_running_worker() -> None:
    import asyncio

    from linch.subagents.runner import continue_subagent

    provider = _GatedProvider([TextTurn("first"), TextTurn("second")])
    agent = make_agent(provider, model="m")
    try:
        parent, handle = await _retained_worker(agent)
        running = asyncio.create_task(
            continue_subagent(_continue_args(parent, agent, handle, "sa_running"))
        )
        await provider.held.wait()

        result = await _stop(parent, {"task_id": handle.worker_id, "reason": "off track"})
        provider.gate.set()
        await running

        assert not result.is_error
        assert handle.status == "killed"
        assert handle.stop_reason == "off track"
    finally:
        provider.gate.set()
        await agent.close()
