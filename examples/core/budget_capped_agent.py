"""Cap a run's spending with RunBudget.

A RunBudget caps total tokens (and/or USD) for a run *and* every subagent it
spawns — all runs in the tree charge the same budget object.  The loop emits a
``budget`` event once at 90% (warning) and stops gracefully with an error
result when the cap is crossed.

Offline demo: a ScriptedProvider replays fixed turns with fixed token usage,
so no API key is needed.
"""

import asyncio

from linch import Agent, RunBudget
from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
from linch.session import RunOptions
from linch.sessions import InMemorySessionStore
from linch.types import Usage


async def main() -> None:
    # Each scripted turn reports 4 000 input tokens; the budget allows 10 000.
    # Turn 1: 4 000 (ok) → turn 2: 8 000 (ok) → turn 3: 12 000 (exceeded) →
    # the loop stops before a fourth provider call.
    provider = ScriptedProvider(
        turns=[
            ToolUseTurn(
                tool_name="Glob",
                tool_input={"pattern": "*.md"},
                usage=Usage(input_tokens=4_000),
            ),
            ToolUseTurn(
                tool_name="Glob",
                tool_input={"pattern": "*.py"},
                usage=Usage(input_tokens=4_000),
            ),
            ToolUseTurn(
                tool_name="Glob",
                tool_input={"pattern": "*.txt"},
                usage=Usage(input_tokens=4_000),
            ),
            TextTurn(text="never reached"),
        ]
    )

    agent = Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()

    budget = RunBudget(max_tokens=10_000)

    async for event in session.run("survey the repo", RunOptions(budget=budget)):
        if event.type == "budget":
            print(
                f"[budget:{event.kind}] spent={event.spent_tokens} of max={event.max_tokens} tokens"
            )
        elif event.type == "error":
            print(f"[error] {event.error['name']}: {event.error['message']}")
        elif event.type == "result":
            print(f"[result] subtype={event.subtype}")

    print(
        f"after run: spent_tokens={budget.spent_tokens}, "
        f"remaining_tokens={budget.remaining_tokens}, exceeded={budget.exceeded}"
    )
    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
