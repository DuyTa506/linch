"""Alignment & interrupt — redirect (or stop) a run that is already in flight.

Run:
    OPENAI_API_KEY=sk-...    python examples/core/aligning_agent.py
    DEEPSEEK_API_KEY=sk-...  python examples/core/aligning_agent.py

While a run is streaming you can inject new guidance without starting a new run:

  * ``await session.align(text)`` — queue a user message. It is applied at the
    next *turn boundary* (after the current tool batch finishes), so the model
    sees it before its next decision. Returns once the message is injected; pass
    ``timeout_s=`` so the caller never blocks forever if the run never resumes.
  * ``session.interrupt()`` — ask the loop to stop cleanly at the next boundary.
    The run ends with ``ResultEvent(subtype="interrupted")`` and any queued
    alignment messages are rejected.

Both are turn-boundary primitives: they take effect between turns, never tearing
a tool call in half (that's what ``session.abort()`` is for). This example uses a
pausable tool so the timing is deterministic — in a real app you'd call
``align``/``interrupt`` from your UI thread while the user watches the stream.

``build_aligning_agent`` is a factory so the smoke test in
``tests/test_example_interaction.py`` can drive it with a ScriptedProvider.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from linch import Agent, ToolResult
from linch.config import FeatureFlags, SystemPromptConfig
from linch.sessions import InMemorySessionStore
from linch.tools import ToolScope
from linch.tools.registry import empty_tools

SYSTEM = (
    "You are a coding assistant. Always call the `long_task` tool first to begin "
    "work, then continue. If the user sends extra guidance mid-task, honor it."
)


class LongTaskTool:
    """A tool that blocks until released — a deterministic pause point.

    ``started`` fires when the model invokes it; the call then waits on
    ``release`` so the harness (or a live caller) has a window to align/interrupt
    before the next turn begins.
    """

    name = "long_task"
    description = "Begin a long-running task. Call this first, then continue."
    input_schema = {"type": "object", "properties": {}}
    scope: ToolScope = "read"
    parallel = False

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {}

    def summarize(self, input: dict[str, Any]) -> str:
        return "long_task()"

    async def execute(self, input: dict[str, Any], ctx: Any) -> ToolResult:
        self.started.set()
        await self.release.wait()
        return ToolResult(content="task finished")


def build_aligning_agent(
    *, provider: Any = None, model: str | None = None
) -> tuple[Agent, LongTaskTool]:
    """Build an agent whose single tool is a pausable ``long_task``.

    Returns ``(agent, tool)``; the caller drives ``tool.started`` / ``tool.release``
    to open a deterministic window for ``session.align`` / ``session.interrupt``.
    """
    tool = LongTaskTool()
    kwargs: dict[str, Any] = {}
    if provider is not None:
        kwargs["provider"] = provider

    agent = Agent(
        model=model or "align-demo",
        tools=empty_tools(tool),
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append=SYSTEM),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        loop_guard=None,
        **kwargs,
    )
    return agent, tool


async def demo_align(agent: Agent, tool: LongTaskTool) -> None:
    print("\n── Alignment — inject guidance mid-run ──")
    session = await agent.session()
    events: list[Any] = []

    async def collect() -> None:
        async for event in session.run("Refactor the auth module."):
            events.append(event)

    run_task = asyncio.create_task(collect())
    # Wait until the model is parked inside long_task, then steer it. align()
    # resolves only once the message is drained at the next turn boundary, which
    # is after the tool is released — so fire it as a concurrent task (this is
    # exactly how a UI thread would call it while another task drives the stream).
    await asyncio.wait_for(tool.started.wait(), timeout=30)
    print("  · agent is mid-task; injecting alignment...")
    align_task = asyncio.create_task(
        session.align("Actually, prioritize correctness over speed.", timeout_s=30)
    )
    tool.release.set()  # let the tool finish so the loop reaches the next turn
    await run_task
    await align_task

    injected = [
        e for e in events if e.type == "user" and getattr(e, "subtype", None) == "alignment"
    ]
    print(f"  · alignment messages injected: {len(injected)}")
    final = next((e for e in reversed(events) if e.type == "result"), None)
    if final is not None:
        print(f"  agent: {final.final_text}")


async def demo_interrupt(agent: Agent, tool: LongTaskTool) -> None:
    print("\n── Interrupt — stop cleanly at the next boundary ──")
    session = await agent.session()
    events: list[Any] = []

    async def collect() -> None:
        async for event in session.run("Start a huge migration."):
            events.append(event)

    run_task = asyncio.create_task(collect())
    await asyncio.wait_for(tool.started.wait(), timeout=30)
    print("  · requesting interrupt...")
    session.interrupt()
    tool.release.set()
    await run_task

    final = next((e for e in reversed(events) if e.type == "result"), None)
    print(f"  · run ended with subtype={final.subtype if final else 'none'!r}")


async def main() -> None:
    from linch.providers import OpenAIChatCompletionsProvider, OpenAIChatProviderOptions

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("Set OPENAI_API_KEY or DEEPSEEK_API_KEY to run this example.")
        print("It shows session.align() injecting guidance mid-run and")
        print("session.interrupt() ending a run with subtype='interrupted'.")
        return

    base_url = "https://api.deepseek.com" if os.environ.get("DEEPSEEK_API_KEY") else None
    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=base_url)
    )
    # A fresh agent per demo so each gets its own pausable tool instance.
    agent_a, tool_a = build_aligning_agent(provider=provider, model="gpt-4o-mini")
    await demo_align(agent_a, tool_a)
    await agent_a.close()

    agent_b, tool_b = build_aligning_agent(provider=provider, model="gpt-4o-mini")
    await demo_interrupt(agent_b, tool_b)
    await agent_b.close()


if __name__ == "__main__":
    asyncio.run(main())
