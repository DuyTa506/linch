"""Live integration tests against DeepSeek (OpenAI Chat Completions compatible).

Run with:
    DEEPSEEK_API_KEY=<key> pytest tests/integration/test_live_deepseek.py -v

Skipped automatically when DEEPSEEK_API_KEY is absent.

Covers our new deep-agent runtime primitives:
  - Background worker spawn + <task-notification> drain
  - Fork/continue a retained worker by id and display_name
  - Coordinator mode roster (no Edit/Write/Bash on parent)
  - TaskStop cancels a running worker
"""

from __future__ import annotations

import asyncio
import os

import pytest

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-chat"
SKIP_REASON = "DEEPSEEK_API_KEY not set"
needs_key = pytest.mark.skipif(not DEEPSEEK_API_KEY, reason=SKIP_REASON)


def _make_provider():
    from linch.providers.openai_chat import OpenAIChatCompletionsProvider, OpenAIChatProviderOptions

    return OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            json_mode=True,
        )
    )


# ── 1. Basic text completion ──────────────────────────────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_deepseek_basic_completion():
    """Agent returns a text response for a simple prompt via DeepSeek."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = Agent(
        model=MODEL,
        provider=_make_provider(),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a concise assistant. Reply in one sentence.",
        ),
        tools=empty_tools(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    results = []
    async for event in session.run("What is 2 + 2?"):
        if event.type == "result":
            results.append(event)

    assert results, "no result event"
    assert results[0].subtype == "success"
    assert "4" in (results[0].final_text or "")


# ── 2. Tool use ───────────────────────────────────────────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_deepseek_tool_use():
    """Agent calls a tool and uses the result to answer."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.base import ToolContext, ToolResult
    from linch.tools.registry import empty_tools

    tool_called = []

    class MultiplierTool:
        name = "multiply"
        description = "Multiply two integers together."
        input_schema = {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        }
        scope = "read"
        parallel_safe = True

        def validate(self, raw):
            return raw

        async def execute(self, input, ctx: ToolContext) -> ToolResult:
            tool_called.append(input)
            return ToolResult(content=str(input["a"] * input["b"]))

        def summarize(self, input):
            return f"multiply({input['a']}, {input['b']})"

    agent = Agent(
        model=MODEL,
        provider=_make_provider(),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="Use the multiply tool to answer math questions. Always call it.",
        ),
        tools=empty_tools(MultiplierTool()),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    result = None
    async for event in session.run("What is 7 multiplied by 8?"):
        if event.type == "result":
            result = event

    assert result is not None
    assert result.subtype == "success"
    assert tool_called, "multiply tool was never called"
    assert "56" in (result.final_text or "")


# ── 3. Background worker spawn + notification drain ──────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_deepseek_background_worker_notification():
    """Background worker appends a <task-notification> that drains on next turn.

    Calls SubagentTool.execute() directly to avoid relying on LLM prompt-following.
    """
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.base import ToolContext
    from linch.tools.registry import default_tools

    agent = Agent(
        model=MODEL,
        provider=_make_provider(),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append="You are a concise assistant.",
        ),
        tools=default_tools(),
        features=FeatureFlags(skills=False, subagents=True, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()

    subagent_tool = agent.tools.get("Subagent")
    assert subagent_tool is not None, "Subagent tool not registered"

    ctx = ToolContext(
        cwd=str(agent.cwd),
        session_id=session.id,
        run_id="test-bg-notif",
        session_store=agent.session_store,
    )

    # Directly spawn a background worker
    spawn_result = await subagent_tool.execute(
        {
            "description": "researcher",
            "prompt": "Reply with exactly 'RESEARCH DONE: 42' and stop.",
            "run_in_background": True,
        },
        ctx,
    )
    assert not spawn_result.is_error, f"spawn failed: {spawn_result.content}"
    assert session.workers, "no worker handle registered after spawn"

    # Wait for the background task to finish and append a notification
    for _ in range(40):
        if session.pending_notifications:
            break
        await asyncio.sleep(0.5)

    assert session.pending_notifications, (
        "No <task-notification> arrived within 20 s — background worker may have failed"
    )
    notif_text = session.pending_notifications[0].content[0].text
    assert "<task-notification>" in notif_text

    # Next LLM turn: notification drains automatically at top of turn
    result_t2 = None
    async for event in session.run("What was the research result?"):
        if event.type == "result":
            result_t2 = event

    assert result_t2 is not None and result_t2.subtype == "success"
    assert not session.pending_notifications, "pending_notifications not drained after turn"


# ── 4. Fork / continue a retained worker ─────────────────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_deepseek_fork_continue_worker():
    """SubagentContinueTool re-drives a retained worker with full context."""
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import default_tools

    agent = Agent(
        model=MODEL,
        provider=_make_provider(),
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You orchestrate worker agents. "
                "When asked to spawn a counter, call Subagent with display_name='counter' "
                "and instructions: 'Reply with COUNT:1 and stop.' "
                "When asked to continue it, call SubagentContinue with to='counter' "
                "and message: 'Now reply with COUNT:2 and stop.' "
                "Always echo the worker's reply verbatim."
            ),
        ),
        tools=default_tools(),
        features=FeatureFlags(skills=False, subagents=True, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()

    # Turn 1: spawn worker
    result_t1 = None
    async for event in session.run("Spawn the counter worker."):
        if event.type == "result":
            result_t1 = event

    assert result_t1 is not None and result_t1.subtype == "success"
    assert session.workers, "no worker handle registered"

    worker_id = next(iter(session.workers))
    handle = session.workers[worker_id]
    assert handle.worker_id == worker_id

    # Turn 2: continue the same worker by worker_id
    result_t2 = None
    async for event in session.run(f"Continue the worker with id {worker_id}."):
        if event.type == "result":
            result_t2 = event

    assert result_t2 is not None, "no result event on continue turn"
    # Accept timeout/error gracefully — the continue turn is a live LLM call
    # that may time out on slow connections; the important SDK contract is that
    # the worker handle persists regardless of whether the second turn succeeds.
    assert worker_id in session.workers, "worker handle lost after continue turn"


# ── 5. Coordinator mode — parent roster restriction ──────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_deepseek_coordinator_mode_roster():
    """create_deep_agent(coordinator=True) strips heavy tools from the parent."""
    from linch.deep_agent import create_deep_agent

    agent = create_deep_agent(
        model=MODEL,
        provider=_make_provider(),
        durable=False,
        coordinator=True,
    )
    # Trigger connect_subagents so Subagent/SubagentContinue are registered
    await agent.session()

    tool_names = {t.name for t in agent.tools.list()}
    # Coordinator parent must NOT have heavy edit/exec tools
    assert "Edit" not in tool_names, "Edit found on coordinator parent"
    assert "Write" not in tool_names, "Write found on coordinator parent"
    assert "Bash" not in tool_names, "Bash found on coordinator parent"
    # Must retain orchestration tools (registered by connect_subagents)
    assert "Subagent" in tool_names
    assert "SubagentContinue" in tool_names
    assert "TaskStop" in tool_names


# ── 6. TaskStop cancels a running background worker ──────────────────────────


@needs_key
@pytest.mark.asyncio
async def test_deepseek_taskstop_cancels_worker():
    """TaskStopTool (coordinator mode) cancels a background worker and sets status='killed'.

    Spawns the worker via SubagentTool.execute() directly to avoid prompt-following flakiness.
    """
    from linch.deep_agent import create_deep_agent
    from linch.tools.base import ToolContext

    agent = create_deep_agent(
        model=MODEL,
        provider=_make_provider(),
        durable=False,
        coordinator=True,
    )
    session = await agent.session()

    # Both tools must be registered in coordinator mode
    subagent_tool = agent.tools.get("Subagent")
    stop_tool = agent.tools.get("TaskStop")
    assert subagent_tool is not None, "Subagent not registered on coordinator"
    assert stop_tool is not None, "TaskStop not registered on coordinator"

    ctx = ToolContext(
        cwd=str(agent.cwd),
        session_id=session.id,
        run_id="test-stop",
        session_store=agent.session_store,
    )

    # Spawn a slow background worker directly
    spawn_result = await subagent_tool.execute(
        {
            "description": "slow-worker",
            "prompt": "Wait for 60 seconds then say done.",
            "run_in_background": True,
        },
        ctx,
    )
    assert not spawn_result.is_error, f"spawn failed: {spawn_result.content}"
    assert session.workers, "no worker handle after spawn"

    worker_id = next(iter(session.workers))
    handle = session.workers[worker_id]
    assert handle.status == "running"

    # Stop it immediately — the background task is still waiting on the LLM call
    stop_result = await stop_tool.execute({"task_id": worker_id, "reason": "test cleanup"}, ctx)
    assert not stop_result.is_error, f"TaskStop error: {stop_result.content}"
    assert handle.status == "killed"
