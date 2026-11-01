from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


async def _pending_background_tasks() -> list[asyncio.Task[Any]]:
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if not t.done() and t is not current]


async def test_run_subagent_does_not_leak_watcher_tasks() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent = make_agent(ScriptedProvider([TextTurn("done")]), model="gpt-5")
    parent = await agent.session()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="do the thing",
            display_name="helper",
            subagent_run_id="sa_leak_test",
        )
    )

    assert not result.errored
    assert await _pending_background_tasks() == []


async def test_cancelled_unretained_subagent_releases_child_session() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    started = asyncio.Event()

    class BlockingProvider(ScriptedProvider):
        async def stream(self, req):
            self.requests.append(req)
            yield {"type": "message_start", "model": req.model}
            started.set()
            await asyncio.Event().wait()

    agent = make_agent(BlockingProvider([]), model="gpt-5")
    parent = await agent.session()
    child_ids: list[str] = []
    task = asyncio.create_task(
        run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="do the thing",
                display_name="helper",
                subagent_run_id="sa_cancel_cleanup",
                on_child_registered=child_ids.append,
            )
        )
    )

    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(child_ids) == 1
    assert child_ids[0] not in agent._sessions
