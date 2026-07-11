"""Regression: subagent runs must not leak any_signal watcher tasks.

linch imports happen inside test functions because tests/loop/test_hardening.py
pops all ``linch*`` modules from ``sys.modules``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest


class TextProvider:
    id = "fake"

    def context_window(self, model: str) -> int:
        return 1_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


async def _pending_background_tasks() -> list[asyncio.Task[Any]]:
    # Two ticks let just-cancelled watcher tasks finish unwinding.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if not t.done() and t is not current]


async def test_run_subagent_does_not_leak_watcher_tasks() -> None:
    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent = Agent(
        model="gpt-5",
        provider=TextProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
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
    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    started = asyncio.Event()

    class BlockingProvider:
        id = "blocking"

        def context_window(self, model: str) -> int:
            return 1_000_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            yield {"type": "message_start", "model": req.model}
            started.set()
            await asyncio.Event().wait()

    agent = Agent(
        model="gpt-5",
        provider=BlockingProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
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
