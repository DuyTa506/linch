"""Tests for Feature E — richer tool-failure recovery (RED until implemented).

When all tools in a batch fail, the loop injects a recovery hint message
before the next provider call so the model can self-correct.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Unit: ToolResult.recovery_hint field exists
# ---------------------------------------------------------------------------


def test_tool_result_has_recovery_hint_field():
    """ToolResult exposes a recovery_hint field that defaults to empty string."""
    from linch.tools.base import ToolResult

    result = ToolResult(content="ok")
    assert hasattr(result, "recovery_hint")
    assert result.recovery_hint == ""


def test_tool_result_recovery_hint_is_settable():
    """recovery_hint can be set to a non-empty hint string."""
    from linch.tools.base import ToolResult

    result = ToolResult(content="failed", is_error=True, recovery_hint="Check your JSON syntax.")
    assert result.recovery_hint == "Check your JSON syntax."


# ---------------------------------------------------------------------------
# Integration: loop injects hint when all tools fail
# ---------------------------------------------------------------------------
# NOTE: All linch imports are lazy (inside test bodies or local class definitions)
# so these tests survive test_hardening.py's sys.modules reset correctly.


def _make_fail_once_provider():
    from linch.providers.base import BaseProvider
    from linch.types import Usage

    class _P(BaseProvider):
        id = "fail-once"

        def __init__(self):
            self.calls = 0
            self.received_messages: list = []

        def context_window(self, model):
            return 100_000

        async def stream(self, req):
            self.calls += 1
            self.received_messages.append(req.messages)
            yield {"type": "message_start", "model": req.model}
            if self.calls == 1:
                yield {"type": "tool_use_start", "id": "call_1", "name": "FakeTool"}
                yield {"type": "tool_use_input_delta", "id": "call_1", "json_delta": "{}"}
                yield {"type": "tool_use_end", "id": "call_1"}
                yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            else:
                yield {"type": "text_delta", "text": "I'll fix my approach."}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    return _P()


def _make_error_with_hint_tool():
    from linch.tools.base import ToolResult

    class _T:
        name = "FakeTool"
        description = "A tool that always errors."
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx):
            return ToolResult(
                content="Input validation failed: 'path' is required.",
                is_error=True,
                recovery_hint="Provide a non-empty 'path' argument.",
            )

        def summarize(self, input):
            return "FakeTool()"

    return _T()


def _make_error_no_hint_tool():
    from linch.tools.base import ToolResult

    class _T:
        name = "FakeTool"
        description = "A tool that errors without a hint."
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx):
            return ToolResult(content="Something went wrong.", is_error=True)

        def summarize(self, input):
            return "FakeTool()"

    return _T()


def _make_registry(tool):
    from linch.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(tool)
    return reg


@pytest.mark.asyncio
async def test_all_failed_with_hint_injects_recovery_message():
    """When all tools fail and any has a recovery_hint, a hint message is injected."""
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    provider = _make_fail_once_provider()
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=_make_registry(_make_error_with_hint_tool()),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    session = await agent.session()
    [e async for e in session.run("use the tool")]

    assert provider.calls == 2
    # Second call's messages should include the injected recovery hint message.
    # Messages are Message objects with TextBlock/ToolResultBlock content.
    second_call_messages = provider.received_messages[1]
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


@pytest.mark.asyncio
async def test_all_failed_without_hint_no_injection():
    """When all tools fail but no recovery_hint is set, no extra message is injected."""
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    provider = _make_fail_once_provider()
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=_make_registry(_make_error_no_hint_tool()),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    session = await agent.session()
    events = [e async for e in session.run("use the tool")]

    assert provider.calls == 2
    # With no recovery_hint, no injection message — verify run completes successfully.
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    # Confirm no "All tool calls failed" injection in the second call's messages
    second_call_messages = provider.received_messages[1]
    for msg in second_call_messages:
        role = msg.role if hasattr(msg, "role") else msg.get("role")
        if role != "user":
            continue
        content = msg.content if hasattr(msg, "content") else msg.get("content", [])
        for block in content:
            block_type = block.type if hasattr(block, "type") else block.get("type")
            if block_type == "text":
                text = block.text if hasattr(block, "text") else block.get("text", "")
                assert "All tool calls failed" not in text


@pytest.mark.asyncio
async def test_partial_failure_no_injection():
    """When only *some* tools fail (not all), no recovery hint is injected."""
    from linch import Agent
    from linch.providers.base import BaseProvider
    from linch.sessions import InMemorySessionStore
    from linch.tools.base import ToolResult
    from linch.types import Usage

    class _MixedProvider(BaseProvider):
        id = "mixed"

        def __init__(self):
            self.calls = 0
            self.received_messages = []

        def context_window(self, model):
            return 100_000

        async def stream(self, req):
            self.calls += 1
            self.received_messages.append(req.messages)
            yield {"type": "message_start", "model": req.model}
            if self.calls == 1:
                for call_id in ["call_1", "call_2"]:
                    yield {"type": "tool_use_start", "id": call_id, "name": "FakeTool"}
                    yield {"type": "tool_use_input_delta", "id": call_id, "json_delta": "{}"}
                    yield {"type": "tool_use_end", "id": call_id}
                yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            else:
                yield {"type": "text_delta", "text": "done"}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    _alt_count = [0]

    class _AlternatingTool:
        name = "FakeTool"
        description = "Fails on odd calls, succeeds on even."
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx):
            _alt_count[0] += 1
            if _alt_count[0] % 2 == 1:
                return ToolResult(content="error", is_error=True, recovery_hint="Fix your input.")
            return ToolResult(content="ok")

        def summarize(self, input):
            return "FakeTool()"

    provider = _MixedProvider()
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=_make_registry(_AlternatingTool()),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    session = await agent.session()
    [e async for e in session.run("use two tools")]

    assert provider.calls == 2
    # Partial failure should NOT inject a recovery message
    second_call_messages = provider.received_messages[1]
    for msg in second_call_messages:
        role = msg.role if hasattr(msg, "role") else msg.get("role")
        if role != "user":
            continue
        content = msg.content if hasattr(msg, "content") else msg.get("content", [])
        for block in content:
            block_type = block.type if hasattr(block, "type") else block.get("type")
            if block_type == "text":
                text = block.text if hasattr(block, "text") else block.get("text", "")
                assert "All tool calls failed" not in text, (
                    f"Unexpected recovery injection for partial failure: {text!r}"
                )
