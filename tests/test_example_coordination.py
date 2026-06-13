"""Smoke tests for the coordination examples (examples/coordination/).

Drive both examples with a deterministic ScriptedProvider (no live key) to prove
they wire up:

  * scheduling_agent — the agent registers a schedule, the embedder's SchedulerLoop
    fires it, and the payload is drained into the next turn as a <scheduled-task>.
  * team_mailbox — one peer addresses another via send_message; the message drains
    into the recipient's next turn; the Correlator resolves a request/response pair.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from linch import SchedulerLoop
from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "coordination"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"coordination_example_{name}", _EXAMPLES / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_scheduling_agent_fires_into_next_turn() -> None:
    example = _load("scheduling_agent")

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="CreateSchedule",
                tool_input={"payload": "Run the test suite", "interval_s": 60},
            ),
            TextTurn(text="Scheduled the job."),
            TextTurn(text="Running the scheduled tests now."),
        ]
    )
    agent, store = example.build_scheduling_agent(provider=provider, model="m")
    session = await agent.session()

    # Turn 1: the agent registers a schedule via the auto-wired tool.
    async for _ in session.run("Run the test suite every minute."):
        pass
    assert len(await store.list()) == 1

    # The embedder owns the clock; jump well past next_run so the schedule is due.
    loop = SchedulerLoop(store, session, clock=lambda: time.time() + 10_000)
    fired = await loop.tick()
    assert len(fired) == 1
    assert session.pending_notifications, "fired schedule should be queued for the next turn"

    # Turn 2: the fired payload is drained as a <scheduled-task> UserEvent.
    user_texts: list[str] = []
    async for event in session.run("continue"):
        if event.type == "user":
            user_texts.append("".join(getattr(b, "text", "") for b in event.message.content))
    joined = " ".join(user_texts)
    assert "<scheduled-task>" in joined
    assert "Run the test suite" in joined


async def test_team_mailbox_peer_messaging_and_correlator() -> None:
    example = _load("team_mailbox")

    provider = ScriptedProvider(
        [
            # Lead delegates to alice via the send_message tool.
            ToolUseTurn(
                tool_name="send_message",
                tool_input={
                    "to": "alice",
                    "content": "Refactor the auth module",
                    "type": "assignment",
                },
            ),
            TextTurn(text="Assigned to alice."),
            # Alice's turn after draining her inbox.
            TextTurn(text="On it."),
        ]
    )
    agent, box = example.build_team(provider=provider, model="m")

    lead = await agent.session()
    lead.mailbox_address = "lead"
    worker = await agent.session()
    worker.mailbox_address = "alice"

    async for _ in lead.run("Ask alice to refactor auth."):
        pass

    drained_texts: list[str] = []
    async for event in worker.run("Check your inbox."):
        if event.type == "user":
            drained_texts.append("".join(getattr(b, "text", "") for b in event.message.content))
    assert any("Refactor the auth module" in t for t in drained_texts)

    # The request/response protocol primitive resolves the handshake over the
    # real mailbox (await box.send), not a mock.
    from linch import Correlator

    transcript = await example.plan_approval_handshake(box, Correlator())
    assert transcript["matched"] is True
    assert transcript["resolved"] is True
    assert transcript["decision"] == "Approved — proceed."
    # Both protocol messages were actually delivered to the mailbox.
    assert len(await box.drain("lead")) == 1
    assert len(await box.drain("alice")) == 1
