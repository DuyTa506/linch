from __future__ import annotations

from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


async def _parent_with_context() -> tuple[Any, ScriptedProvider, Any]:
    from linch.types import Message, TextBlock

    provider = ScriptedProvider([TextTurn("done"), TextTurn("done")])
    agent = make_agent(provider, model="gpt-5")
    parent = await agent.session()
    await parent.append(
        [
            Message(role="user", content=[TextBlock(text="parent ask")]),
            Message(role="assistant", content=[TextBlock(text="parent answer")]),
        ]
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


@pytest.mark.parametrize("fork,shares_context", [(True, True), (False, False)])
async def test_subagent_context_mode(fork: bool, shares_context: bool) -> None:
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
            subagent_run_id=f"sa_{fork}",
            fork=fork,
            retain=True,
        )
    )

    texts = _request_texts(provider.requests[-1])
    assert ("parent ask" in texts) is shares_context
    assert ("parent answer" in texts) is shares_context
    child = agent._sessions[result.child_session_id]
    assert ("/x.py" in child.file_read_tracker) is shares_context
