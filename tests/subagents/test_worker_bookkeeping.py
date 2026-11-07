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
