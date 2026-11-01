"""Micro-compaction must sever provider response chaining in its projection."""

from __future__ import annotations

from collections.abc import Sequence

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider


def _assistant_tool_message(response_id: str, tool_id: str):
    from linch.types import Message, ToolUseBlock

    return Message(
        role="assistant",
        content=[ToolUseBlock(id=tool_id, name="search", input={"query": tool_id})],
        provider_metadata={"openai_responses": {"response_id": response_id}},
    )


def _tool_result_message(tool_id: str, content: str = "result payload"):
    from linch.types import Message, ToolResultBlock

    return Message(
        role="user",
        content=[ToolResultBlock(tool_use_id=tool_id, content=content)],
    )


def _response_ids(messages: Sequence[object]) -> list[str]:
    return [
        metadata["openai_responses"]["response_id"]
        for message in messages
        if (metadata := getattr(message, "provider_metadata", None))
        and "response_id" in metadata.get("openai_responses", {})
    ]


async def test_micro_compaction_elides_old_results_and_strips_projection_ids() -> None:
    from linch.compaction import apply_micro_compaction

    agent = make_agent(ScriptedProvider([]), run_store=None)
    session = await agent.session()
    messages = [
        _assistant_tool_message("old-response", "old-call"),
        _tool_result_message("old-call", "old result that is long enough to be elided"),
        _assistant_tool_message("recent-response", "recent-call"),
        _tool_result_message("recent-call", "recent result must remain"),
    ]
    await session.append(messages)

    changed = apply_micro_compaction(
        session,
        agent,
        keep_recent_turns=1,
    )

    assert changed is True
    assert _response_ids(session.provider_view) == []
    assert _response_ids(session.full_history) == ["old-response", "recent-response"]
    assert session.provider_view[1].content[0].content == "[tool result elided to save context]"
    assert session.provider_view[3].content[0].content == "recent result must remain"


async def test_micro_compaction_noop_preserves_response_chaining_ids() -> None:
    from linch.compaction import apply_micro_compaction

    agent = make_agent(ScriptedProvider([]), run_store=None)
    session = await agent.session()
    await session.append(
        [
            _assistant_tool_message("only-response", "only-call"),
            _tool_result_message("only-call", "recent result"),
        ]
    )

    changed = apply_micro_compaction(
        session,
        agent,
        keep_recent_turns=1,
    )

    assert changed is False
    assert _response_ids(session.provider_view) == ["only-response"]
    assert _response_ids(session.full_history) == ["only-response"]
