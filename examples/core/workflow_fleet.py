"""Workflow engine — a deterministic "fleet loop" over subagents.

``agent.run_workflow(fn)`` runs a plain async function that orchestrates
subagents deterministically:

- ``await wf.agent(prompt)``      — run a subagent, get its final text
- ``await wf.parallel(thunks)``   — fan out concurrently (semaphore-capped)
- ``await wf.pipeline(items, *stages)`` — per-item stage chaining, no barrier
- ``await wf.phase(title)``       — progress grouping for observers
- ``wf.budget``                   — the shared RunBudget across all children

With ``Agent(run_store=...)`` and a ``run_id``, every ``wf.agent`` result is
journaled: re-running the same workflow after a crash replays the unchanged
prefix from the journal instead of re-running subagents.

Fake-provider demo (set LINCH_POSTGRES_DSN for Postgres durability) — two parts:
1. a research fan-out with phases, parallel agents, and a shared budget;
2. a crash + resume showing journal replay (no provider calls for the prefix).
"""

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from linch import Agent, PostgresRunStore, RunBudget, WorkflowError
from linch.sessions import InMemorySessionStore
from linch.types import Usage


class TextProvider:
    """Answers every subagent call with 'result-N'; can fail on one call."""

    id = "fake"

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    def context_window(self, model: str) -> int:
        return 1_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("simulated provider crash")
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": f"result-{self.calls}"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(input_tokens=1_000),
        }


def make_agent(provider: TextProvider, run_store: PostgresRunStore | None = None) -> Agent:
    return Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        run_store=run_store,
    )


async def fan_out_demo() -> None:
    print("── fan-out with shared budget ──")
    agent = make_agent(TextProvider())
    budget = RunBudget(max_tokens=50_000)

    async def research(wf: Any) -> dict[str, Any]:
        await wf.phase("Survey")
        findings = await wf.parallel(
            [
                lambda: wf.agent("survey the auth module", label="auth"),
                lambda: wf.agent("survey the storage module", label="storage"),
                lambda: wf.agent("survey the API module", label="api"),
            ]
        )
        await wf.phase("Deepen")
        deepened = await wf.pipeline(
            findings,
            lambda f: wf.agent(f"deep dive: {f}"),
        )
        return {"findings": deepened, "tokens_spent": wf.budget.spent_tokens}

    try:
        result = await agent.run_workflow(
            research,
            budget=budget,
            on_event=lambda e: e.type == "workflow" and print(f"  [{e.kind}] {e.title}"),
        )
        print(f"  result: {result}")
    finally:
        await agent.close()


async def resume_demo(dsn: str) -> None:
    print("── crash + journal resume ──")

    async def flow(wf: Any) -> list[str]:
        one = await wf.agent("step one")
        two = await wf.agent("step two")
        three = await wf.agent("step three")
        return [one, two, three]

    run_id = f"wf-demo-{uuid4().hex}"
    crashing = TextProvider(fail_on_call=3)
    agent1 = make_agent(crashing, run_store=PostgresRunStore(dsn))
    try:
        await agent1.run_workflow(flow, run_id=run_id)
    except WorkflowError as exc:
        print(f"  first attempt failed on call 3: {exc}")
    finally:
        await agent1.close()

    healthy = TextProvider()
    agent2 = make_agent(healthy, run_store=PostgresRunStore(dsn))
    try:
        result = await agent2.run_workflow(
            flow,
            run_id=run_id,
            on_event=lambda e: (
                e.type == "workflow"
                and e.kind == "agent_replayed"
                and print(f"  replayed from journal: occurrence {e.occurrence}")
            ),
        )
        print(f"  resumed result: {result} (provider calls on resume: {healthy.calls})")
    finally:
        await agent2.close()


async def main() -> None:
    await fan_out_demo()
    await resume_demo(os.environ["LINCH_POSTGRES_DSN"])


if __name__ == "__main__":
    asyncio.run(main())
