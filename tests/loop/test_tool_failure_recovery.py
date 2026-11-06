"""Tests for Feature E — richer tool-failure recovery (RED until implemented).

When all tools in a batch fail, the loop injects a recovery hint message
before the next provider call so the model can self-correct.
"""

from __future__ import annotations

import pytest

from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry

# ---------------------------------------------------------------------------
# Unit: ToolResult.recovery_hint field exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "recovery_hint", "expected"),
    [("ok", None, ""), ("failed", "Check your JSON syntax.", "Check your JSON syntax.")],
)
def test_tool_result_recovery_hint_field(content, recovery_hint, expected):
    """ToolResult exposes a default and configurable recovery hint."""
    from linch.tools.base import ToolResult

    kwargs = {} if recovery_hint is None else {"recovery_hint": recovery_hint, "is_error": True}
    result = ToolResult(content=content, **kwargs)
    assert hasattr(result, "recovery_hint")
    assert result.recovery_hint == expected


# ---------------------------------------------------------------------------
# Integration: loop injects hint when all tools fail
# ---------------------------------------------------------------------------
# NOTE: All linch imports are lazy (inside test bodies or local class definitions)
# so these tests survive test_hardening.py's sys.modules reset correctly.


def _make_fail_once_provider() -> ScriptedProvider:
    return ScriptedProvider(
        [
            ToolUseTurn("FakeTool", {}),
            TextTurn("I'll fix my approach."),
        ]
    )


def _make_error_with_hint_tool() -> StubTool:
    from linch.tools.base import ToolResult

    class _FailureTool(StubTool):
        async def execute(self, input, ctx):
            return ToolResult(
                content="Input validation failed: 'path' is required.",
                is_error=True,
                recovery_hint="Provide a non-empty 'path' argument.",
            )

    return _FailureTool("FakeTool", scope="read", parallel=True)


@pytest.mark.asyncio
async def test_all_failed_with_hint_injects_recovery_message():
    """When all tools fail and any has a recovery_hint, a hint message is injected."""
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    provider = _make_fail_once_provider()
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=tool_registry(_make_error_with_hint_tool()),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    session = await agent.session()
    [e async for e in session.run("use the tool")]

    assert len(provider.requests) == 2
    # Second call's messages should include the injected recovery hint message.
    # Messages are Message objects with TextBlock/ToolResultBlock content.
    second_call_messages = provider.requests[1].messages
    hint_texts = []
    for msg in second_call_messages:
        role = msg.role if hasattr(msg, "role") else msg.get("role")
        if role != "user":
            continue
        content = msg.content if hasattr(msg, "content") else msg.get("content", [])
        for block in content:
            block_type = block.type if hasattr(block, "type") else block.get("type")
            if block_type == "text":
                text = block.text if hasattr(block, "text") else block.get("text", "")
                hint_texts.append(text)
    full_text = " ".join(hint_texts)
    assert "Provide" in full_text or "path" in full_text.lower() or "recovery" in full_text.lower()

    assistant_index = next(
        index
        for index, message in enumerate(second_call_messages)
        if getattr(message, "role", None) == "assistant"
    )
    result_index = next(
        index
        for index, message in enumerate(second_call_messages)
        if getattr(message, "role", None) == "user"
        and any(getattr(block, "type", None) == "tool_result" for block in message.content)
    )
    hint_index = next(
        index
        for index, message in enumerate(second_call_messages)
        if getattr(message, "role", None) == "user"
        and any(
            getattr(block, "type", None) == "text" and "All tool calls failed" in block.text
            for block in message.content
        )
    )
    assert assistant_index < result_index < hint_index
