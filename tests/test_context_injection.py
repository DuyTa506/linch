"""Tests for per-turn context injection hooks.

NOTE: agent_kit imports inside test functions so tests are robust to
test_hardening.py's sys.modules reset.
"""

from __future__ import annotations

from typing import Any

import pytest

# ── Helper builders (all imports lazy) ──────────────────────────────────────


def _two_turn_provider():
    from agent_kit.providers.base import BaseProvider
    from agent_kit.types import TextBlock, Usage

    class _Provider(BaseProvider):
        id = "fake"
        calls: list = []

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            self.calls.append(
                {
                    "system": [b.text for b in req.system],
                    "messages": [
                        {
                            "role": m.role,
                            "content": [
                                b.text if isinstance(b, TextBlock) else str(b) for b in m.content
                            ],
                        }
                        for m in req.messages
                    ],
                }
            )
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "Answer"}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(),
                "provider_metadata": None,
            }

    return _Provider()


class _RecordingInjector:
    TAG = "[[test-inject]]"
    call_count = 0

    async def before_turn(self, ctx: Any) -> None:
        self.call_count += 1
        from agent_kit.types import Message, SystemBlock, TextBlock

        ctx.provider_view.append(
            Message(
                role="user",
                content=[TextBlock(text=f"{self.TAG} turn={ctx.turn_index}")],
            )
        )
        ctx.extra_system.append(SystemBlock(text="EXTRA_SYSTEM_BLOCK", cacheable=False))


class _PruningInjector:
    TAG = "[[pruning]]"

    async def before_turn(self, ctx: Any) -> None:
        from agent_kit.context_hooks import prune_tagged
        from agent_kit.types import Message, TextBlock

        prune_tagged(ctx.provider_view, self.TAG)
        ctx.provider_view.append(
            Message(
                role="user",
                content=[TextBlock(text=f"{self.TAG} fresh")],
            )
        )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_injector_fires_and_reaches_provider():
    from agent_kit import Agent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import empty_tools

    provider = _two_turn_provider()
    injector = _RecordingInjector()
    injector.call_count = 0

    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        context_injectors=[injector],
    )
    session = await agent.session()
    async for _ in session.run("hello"):
        pass

    assert injector.call_count >= 1

    first_call = provider.calls[0]
    all_msg_texts = [c for m in first_call["messages"] for c in m["content"]]
    assert any(_RecordingInjector.TAG in t for t in all_msg_texts)
    assert any("EXTRA_SYSTEM_BLOCK" in s for s in first_call["system"])


@pytest.mark.asyncio
async def test_pruning_prevents_accumulation():
    from agent_kit import Agent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import empty_tools
    from agent_kit.types import TextBlock

    provider = _two_turn_provider()
    injector = _PruningInjector()

    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        context_injectors=[injector],
    )
    session = await agent.session()
    async for _ in session.run("hello"):
        pass

    tagged = [
        m
        for m in session.provider_view
        if m.content
        and isinstance(m.content[0], TextBlock)
        and m.content[0].text.startswith(_PruningInjector.TAG)
    ]
    assert len(tagged) <= 1


# ── prune_tagged unit tests ────────────────────────────────────────────────────


def test_prune_tagged_removes_matching():
    from agent_kit.context_hooks import prune_tagged
    from agent_kit.types import Message, TextBlock

    TAG = "[[x]]"
    msgs = [
        Message(role="user", content=[TextBlock(text="keep")]),
        Message(role="user", content=[TextBlock(text=f"{TAG} remove")]),
        Message(role="user", content=[TextBlock(text=f"{TAG} also remove")]),
        Message(role="user", content=[TextBlock(text="keep2")]),
    ]
    prune_tagged(msgs, TAG)
    assert len(msgs) == 2
    assert all("keep" in m.content[0].text for m in msgs)


def test_prune_tagged_no_match_is_noop():
    from agent_kit.context_hooks import prune_tagged
    from agent_kit.types import Message, TextBlock

    msgs = [
        Message(role="user", content=[TextBlock(text="alpha")]),
        Message(role="user", content=[TextBlock(text="beta")]),
    ]
    before = [m for m in msgs]
    prune_tagged(msgs, "[[nope]]")
    assert len(msgs) == len(before)


def test_prune_tagged_empty_list():
    from agent_kit.context_hooks import prune_tagged

    msgs: list = []
    prune_tagged(msgs, "[[x]]")
    assert msgs == []


@pytest.mark.asyncio
async def test_no_injectors_zero_overhead():
    from agent_kit import Agent
    from agent_kit.sessions import InMemorySessionStore
    from agent_kit.tools.registry import empty_tools

    provider = _two_turn_provider()

    agent = Agent(
        model="gpt-5",
        provider=provider,
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    results = []
    async for event in session.run("hello"):
        if event.type == "result":
            results.append(event)

    assert results
    assert results[0].subtype == "success"
