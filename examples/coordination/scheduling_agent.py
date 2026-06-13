"""Coordination — an agent that schedules its own recurring work.

Run:
    OPENAI_API_KEY=sk-...    python examples/coordination/scheduling_agent.py
    DEEPSEEK_API_KEY=sk-...  python examples/coordination/scheduling_agent.py

Scheduling is a *coordination* primitive (``linch.coordination.scheduling``) — it
advances the loop from a clock, not from a user turn. It is fully opt-in: pass
``Agent(schedule_store=...)`` and the
``CreateSchedule`` / ``ListSchedules`` / ``CancelSchedule`` tools are registered so
the agent can enqueue its own future work. With no store, nothing is added and the
loop is byte-identical.

The SDK ships only the mechanism — *what a fired schedule means is embedder policy*.
This example shows the full round trip:

  1. The agent calls ``CreateSchedule`` (a cron expression or an interval).
  2. The embedder drives a ``SchedulerLoop`` over the same store. On each ``tick()``
     a due schedule fires its ``payload`` into the session's ``pending_notifications``
     — the same channel background workers use.
  3. The next ``session.run()`` drains that payload as a ``UserEvent`` at the top of
     the turn, so the agent picks the work up exactly as if a user had asked.

``build_scheduling_agent`` is a factory so the smoke test in
``tests/test_example_coordination.py`` can drive it with a deterministic ``ScriptedProvider``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from linch import Agent, InMemoryScheduleStore, SchedulerLoop, empty_tools
from linch.config import FeatureFlags, SystemPromptConfig
from linch.sessions import InMemorySessionStore

SYSTEM = (
    "You are an ops assistant. When the user asks for recurring work, register it with "
    "CreateSchedule (a 5-field cron expression in UTC, or interval_s seconds). When a "
    "scheduled job fires, you receive it as a <scheduled-task> message — act on it."
)


def build_scheduling_agent(*, provider: Any = None, model: str | None = None) -> tuple[Agent, Any]:
    """Build an agent wired to an in-memory schedule store.

    Pass ``provider`` + ``model`` (e.g. a ``ScriptedProvider``) to drive it
    deterministically; otherwise the caller supplies a live provider via kwargs.
    Returns ``(agent, schedule_store)`` — the store is shared with the
    ``SchedulerLoop`` the embedder runs.
    """
    store = InMemoryScheduleStore()
    kwargs: dict[str, Any] = {}
    if provider is not None:
        kwargs["provider"] = provider

    agent = Agent(
        model=model or "scheduler-demo",
        # Only the schedule tools are needed; empty_tools() drops the default
        # Bash/Write/Edit/Read (no need for shell/file access on the real cwd).
        # schedule_store= then auto-registers CreateSchedule/ListSchedules/CancelSchedule.
        tools=empty_tools(),
        schedule_store=store,
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append=SYSTEM),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        **kwargs,
    )
    return agent, store


async def main() -> None:
    from linch.providers import OpenAIChatCompletionsProvider, OpenAIChatProviderOptions

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("Set OPENAI_API_KEY or DEEPSEEK_API_KEY to run this example.")
        return

    base_url = "https://api.deepseek.com" if os.environ.get("DEEPSEEK_API_KEY") else None
    provider = OpenAIChatCompletionsProvider(
        OpenAIChatProviderOptions(api_key=api_key, base_url=base_url)
    )
    agent, store = build_scheduling_agent(provider=provider, model="gpt-4o-mini")
    session = await agent.session()

    print("→ Asking the agent to schedule a recurring job...")
    async for event in session.run("Run the test suite every 2 seconds and report failures."):
        if event.type == "tool_call_end":
            print(f"  · {event.tool_name}: {event.result}")
        elif event.type == "result":
            print(f"  agent: {event.final_text}")

    # The embedder owns the clock. A short interval was requested, so a couple of
    # real ticks will fire it. (In production this loop runs for the process'
    # lifetime via loop.start(); here we tick by hand to keep the demo bounded.)
    loop = SchedulerLoop(store, session)
    print("\n→ Ticking the scheduler; waiting for the job to fire...")
    for _ in range(4):
        await asyncio.sleep(1)
        fired = await loop.tick()
        if fired:
            print(f"  fired {len(fired)} schedule(s) → next turn will pick it up")
            break

    print("\n→ Next turn drains the fired job as if the user had asked:")
    async for event in session.run("continue"):
        if event.type == "user":
            print(f"  (scheduled) {''.join(getattr(b, 'text', '') for b in event.message.content)}")
        elif event.type == "result":
            print(f"  agent: {event.final_text}")

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
