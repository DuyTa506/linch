"""Fork-mode subagents (ROADMAP Step 3 / Phase 1.1).

Normal subagents get a fresh, isolated context. A *forked* subagent instead
continues from the parent's context — same conversation prefix, system blocks,
and read-file tracker — so a caching provider can reuse the cached prefix
instead of re-paying for it. Trades isolation for cost; opt-in.

linch imports happen inside test functions because other tests pop ``linch*``
modules from ``sys.modules``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


class RecordingProvider:
    id = "fake"

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 1_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.requests.append(req)
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


async def _parent_with_context() -> tuple[Any, RecordingProvider, Any]:
    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.types import Message, TextBlock

    provider = RecordingProvider()
    agent = Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    parent = await agent.session()
    parent.provider_view.append(Message(role="user", content=[TextBlock(text="parent ask")]))
    parent.provider_view.append(
        Message(role="assistant", content=[TextBlock(text="parent answer")])
    )
    parent.file_read_tracker.add("/x.py")
    return agent, provider, parent


def _request_texts(req: Any) -> list[str]:
    return [
        block.text
        for message in req.messages
        for block in message.content
        if hasattr(block, "text")
    ]


async def test_fork_subagent_seeds_parent_context() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent, provider, parent = await _parent_with_context()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="child task",
            display_name="helper",
            subagent_run_id="sa_fork",
            fork=True,
            retain=True,
        )
    )

    texts = _request_texts(provider.requests[-1])
    # The child's request carries the parent conversation as a cache-friendly prefix.
    assert "parent ask" in texts
    assert "parent answer" in texts
    # The read-file tracker is cloned so the child doesn't re-read known files.
    child = agent._sessions[result.child_session_id]
    assert "/x.py" in child.file_read_tracker


async def test_normal_subagent_has_fresh_context() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent, provider, parent = await _parent_with_context()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="child task",
            display_name="helper",
            subagent_run_id="sa_normal",
            retain=True,
        )
    )

    texts = _request_texts(provider.requests[-1])
    # Default: isolated — no parent conversation leaks into the child.
    assert "parent ask" not in texts
    assert "parent answer" not in texts
    child = agent._sessions[result.child_session_id]
    assert "/x.py" not in child.file_read_tracker
