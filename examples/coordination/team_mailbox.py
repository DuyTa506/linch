"""Coordination — a two-agent team that coordinates over a mailbox.

Run:
    OPENAI_API_KEY=sk-...    python examples/coordination/team_mailbox.py
    DEEPSEEK_API_KEY=sk-...  python examples/coordination/team_mailbox.py

The core SDK gives you parent→child subagents (a worker runs once and returns a
conclusion). The *coordination* mailbox (``linch.coordination.mailbox``) adds the
other half: peers that address each other directly and exchange messages across turns —
the substrate under any multi-agent *team*. Opt in with ``Agent(mailbox=...)``; with
no mailbox the ``send_message`` tool is absent and no inbox drain runs (byte-identical).

Two mechanisms, shown end to end:

  1. **Message bus (s15).** A session with a ``mailbox_address`` drains its inbox at
     the top of each turn (exactly like ``pending_notifications``). The ``send_message``
     tool lets one agent address a peer; the peer picks it up on its next run.
  2. **Request/response protocol (s16).** A ``Correlator`` is a tiny pending→resolved
     state machine: a requester ``open()``s a ``request_id``, the responder echoes it in
     ``in_reply_to``, and ``resolve()`` matches them. It is non-blocking by design — a
     turn-based agent opens a request, continues, and checks ``is_resolved`` later.

``build_team`` is a factory so the smoke test in ``tests/test_example_coordination.py`` can
drive both agents with a deterministic ``ScriptedProvider``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from linch import Agent, Correlator, InMemoryMailbox, MailboxMessage, empty_tools
from linch.config import FeatureFlags, SystemPromptConfig
from linch.sessions import InMemorySessionStore

LEAD_SYSTEM = "You are the team lead. Delegate work to teammates with the send_message tool."
WORKER_SYSTEM = "You are a teammate. You receive assignments as <peer-message> and act on them."


def build_team(*, provider: Any = None, model: str | None = None) -> tuple[Agent, InMemoryMailbox]:
    """Build one agent backed by a shared in-process mailbox.

    Lead and worker are two *sessions* of the same agent, each given a distinct
    ``mailbox_address`` so peers can address them. Pass ``provider`` + ``model``
    (e.g. a ``ScriptedProvider``) to drive it deterministically.
    Returns ``(agent, mailbox)``.
    """
    box = InMemoryMailbox()
    kwargs: dict[str, Any] = {}
    if provider is not None:
        kwargs["provider"] = provider

    agent = Agent(
        model=model or "team-demo",
        # Only send_message is needed; empty_tools() drops the default
        # Bash/Write/Edit/Read (no shell/file access on the real cwd).
        # mailbox= then auto-registers the send_message tool.
        tools=empty_tools(),
        mailbox=box,
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append=LEAD_SYSTEM),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        **kwargs,
    )
    return agent, box


async def plan_approval_handshake(box: InMemoryMailbox, correlator: Correlator) -> dict[str, Any]:
    """Illustrate the request/response protocol primitive over the real mailbox.

    Takes the actual :class:`Mailbox` and ``await``s ``box.send`` — so this is the
    pattern an embedder uses verbatim, not a mock. No LLM involved; this is the
    embedder-level handshake a teammate would use to get a plan approved.
    Returns the transcript.
    """
    # Worker 'alice' asks lead to approve a risky plan.
    request = MailboxMessage(
        sender="alice",
        recipient="lead",
        content="Plan: rewrite auth to use JWT. Approve?",
        type="plan_approval_request",
        request_id="req-001",
    )
    correlator.open(request.request_id or "")
    await box.send(request)

    # Lead approves; the response echoes the request_id via in_reply_to.
    response = MailboxMessage(
        sender="lead",
        recipient="alice",
        content="Approved — proceed.",
        type="plan_approval_response",
        in_reply_to="req-001",
    )
    await box.send(response)
    matched = correlator.resolve(response)
    resolved = correlator.response("req-001")
    return {
        "matched": matched,
        "resolved": correlator.is_resolved("req-001"),
        "decision": resolved.content if resolved is not None else None,
    }


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
    agent, box = build_team(provider=provider, model="gpt-4o-mini")

    lead = await agent.session()
    lead.mailbox_address = "lead"
    worker = await agent.session()
    worker.mailbox_address = "alice"

    print("→ Lead delegates to teammate 'alice' via send_message...")
    async for event in lead.run("Ask alice to refactor the auth module."):
        if event.type == "tool_call_end":
            print(f"  · {event.tool_name}: {event.result}")

    print("\n→ Teammate 'alice' drains her inbox on her next turn:")
    async for event in worker.run("Check your inbox and get to work."):
        if event.type == "user":
            print(f"  (peer) {''.join(getattr(b, 'text', '') for b in event.message.content)}")
        elif event.type == "result":
            print(f"  alice: {event.final_text}")

    print("\n→ Plan-approval protocol (Correlator handshake):")
    transcript = await plan_approval_handshake(box, Correlator())
    print(f"  {transcript}")

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
