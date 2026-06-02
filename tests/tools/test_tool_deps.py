"""Tests for ToolContext.deps threading from Agent/RunOptions into tool execution.

NOTE: All linch imports inside test functions (not module-level) so tests
are robust to test_hardening.py's sys.modules reset.
"""

from __future__ import annotations

import pytest

_SENTINEL = object()
_OTHER = object()

_received_deps: list = []


@pytest.fixture(autouse=True)
def clear_received():
    _received_deps.clear()
    yield
    _received_deps.clear()


def _make_deps_captor_tool():
    from linch.tools.base import ToolContext, ToolResult

    class DepsCaptorTool:
        name = "CaptureDeps"
        description = "Captures ctx.deps for test inspection."
        input_schema: dict = {"type": "object", "properties": {}}
        scope = "read"
        parallel_safe = False

        def validate(self, raw: dict) -> dict:
            return raw

        async def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
            _received_deps.append(ctx.deps)
            return ToolResult(content="captured", summary="CaptureDeps")

        def summarize(self, input: dict) -> str:
            return "CaptureDeps"

    return DepsCaptorTool()


def _fake_provider(tool_name: str = "CaptureDeps"):
    from linch.providers.base import BaseProvider
    from linch.types import Usage

    class FakeProvider(BaseProvider):
        id = "fake"

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            if not hasattr(self, "_call_count"):
                self._call_count = 0
            self._call_count += 1
            yield {"type": "message_start", "model": req.model}
            if self._call_count == 1:
                yield {"type": "tool_use_start", "id": "t1", "name": tool_name}
                yield {"type": "tool_use_input_delta", "id": "t1", "json_delta": "{}"}
                yield {"type": "tool_use_end", "id": "t1"}
                yield {
                    "type": "message_end",
                    "stop_reason": "tool_use",
                    "usage": Usage(),
                    "provider_metadata": None,
                }
            else:
                yield {"type": "text_delta", "text": "Done"}
                yield {
                    "type": "message_end",
                    "stop_reason": "end_turn",
                    "usage": Usage(),
                    "provider_metadata": None,
                }

    return FakeProvider()


@pytest.mark.asyncio
async def test_agent_level_deps_threaded():
    """Agent.deps is available in ctx.deps."""
    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model="gpt-5",
        provider=_fake_provider(),
        tools=empty_tools(_make_deps_captor_tool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        deps=_SENTINEL,
    )
    session = await agent.session()
    async for _ in session.run("go"):
        pass

    assert len(_received_deps) == 1
    assert _received_deps[0] is _SENTINEL


@pytest.mark.asyncio
async def test_run_options_deps_overrides_agent():
    """RunOptions.deps overrides Agent.deps for the duration of that run."""
    from linch import Agent, RunOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model="gpt-5",
        provider=_fake_provider(),
        tools=empty_tools(_make_deps_captor_tool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        deps=_SENTINEL,
    )
    session = await agent.session()
    async for _ in session.run("go", RunOptions(deps=_OTHER)):
        pass

    assert len(_received_deps) == 1
    assert _received_deps[0] is _OTHER


@pytest.mark.asyncio
async def test_no_deps_is_none():
    """When no deps are set, ctx.deps is None."""
    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model="gpt-5",
        provider=_fake_provider(),
        tools=empty_tools(_make_deps_captor_tool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    async for _ in session.run("go"):
        pass

    assert len(_received_deps) == 1
    assert _received_deps[0] is None
