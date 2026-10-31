"""Smoke tests for the coordination examples (examples/coordination/).

Drive both examples with a deterministic ScriptedProvider (no live key) to prove
they wire up:

  * team_mailbox — one peer addresses another via send_message; the message drains
    into the recipient's next turn; the Correlator resolves a request/response pair.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
    agent, box = await example.build_team(provider=provider, model="m")

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
