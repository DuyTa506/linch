"""System prompt sections and the context budget, end to end against the real server."""

from __future__ import annotations

from typing import Any

import pytest
from scripted import MODEL, config, text_turn

from linch_rust import (
    AgentConfig,
    ContextBudget,
    ContextBuildEvent,
    DroppedContext,
    OpenError,
    Server,
    System,
    callback,
)
from linch_rust.memory import Chunk, retrieval


class Store:
    """Four chunks of 10 estimated tokens each (a 13 character body and the 27 character header)."""

    def __init__(self) -> None:
        self.chunks = [Chunk(name, f"notes/{name}.md", name * 13) for name in "abcd"]

    async def retrieve(self, query: str, *, k: int) -> list[Chunk]:
        return self.chunks[:k]


def with_spec(cfg: AgentConfig, **fields: Any) -> AgentConfig:
    cfg["spec"] = {**cfg["spec"], **fields}
    return cfg


async def sent(cfg: AgentConfig, *callbacks: Any) -> tuple[dict[str, Any], list[ContextBuildEvent]]:
    """The first request the provider is sent, and the run's `context_build` events."""
    seen: list[dict[str, Any]] = []

    @callback("before_provider_call", "spy", 1)
    async def spy(request: dict[str, Any]) -> dict[str, Any]:
        seen.append(request["request"])
        return {"action": "continue"}

    async with Server() as server:
        agent = await server.open_agent(cfg, callbacks=[*callbacks, spy])
        envelopes = [e async for e in await agent.run("s1", "what is alpha?")]
    builds = [e.event for e in envelopes if isinstance(e.event, ContextBuildEvent)]
    return seen[0], builds


async def test_sections_go_in_order_with_their_cache_flags_around_the_prompt() -> None:
    system: System = {
        "sections": [
            {"name": "tone", "text": "Tone.", "order": 2},
            {"name": "policy", "text": "Policy.", "order": 1},
        ],
        "append": "Last word.",
    }
    cfg = with_spec(config([text_turn("ok")]), system_prompt="Be brief.", system=system)
    request, [build] = await sent(cfg)
    blocks = [(b["text"], b.get("cacheable", False)) for b in request["system"]]
    assert blocks == [
        ("Policy.", True),
        ("Tone.", True),
        ("Be brief.", False),
        ("Last word.", False),
    ]
    assert build.budget == {}, "no budget, nothing reported"


async def test_a_section_that_is_not_cacheable_before_a_cacheable_block_does_not_open() -> None:
    system: System = {
        "sections": [
            {"name": "today", "text": "Today.", "cacheable": False},
            {"name": "policy", "text": "Policy.", "order": 1},
        ]
    }
    async with Server() as server:
        with pytest.raises(OpenError):
            await server.open_agent(with_spec(config([text_turn("ok")]), system=system))


async def test_over_budget_drops_the_unlisted_callback_first_and_the_event_says_which() -> None:
    @callback("context_build", "notes", 1)
    async def notes(request: dict[str, Any]) -> dict[str, Any]:
        return {"blocks": [{"text": "n" * 40}]}

    budget: ContextBudget = {"max_tokens": 25}
    cfg = with_spec(config([text_turn("ok")]), context_budget=budget)
    request, [build] = await sent(cfg, retrieval(Store(), k=3), notes)
    last = request["messages"][-1]
    texts = [b["text"] for b in last["content"]]
    # 3 chunks of 10 and a note of 10: the later callback (the note) goes
    # first, then the last chunk, to fit 25.
    assert len(texts) == 1 + 2
    assert build.budget["trimmed"] is True
    assert build.dropped == [DroppedContext("memory", 2, 10), DroppedContext("notes", 0, 10)]
    assert build.metadata["memory"]["ids"] == ["a", "b", "c"], "the metadata still lists all three"
    assert build.budget["used_tokens"] <= 25


async def test_a_pinned_callback_stays_and_the_declared_order_decides_the_rest() -> None:
    @callback("context_build", "policy", 1)
    async def policy(request: dict[str, Any]) -> dict[str, Any]:
        return {"blocks": [{"text": "p" * 40}]}

    budget: ContextBudget = {"max_tokens": 20, "pinned": ["policy"], "drop_order": ["memory"]}
    cfg = with_spec(config([text_turn("ok")]), context_budget=budget)
    request, [build] = await sent(cfg, policy, retrieval(Store(), k=3))
    texts = [b["text"] for b in request["messages"][-1]["content"]]
    assert texts[1] == "p" * 40
    assert len(texts) == 3, "the question, the pinned note and the best chunk"
    assert [d.source for d in build.dropped] == ["memory", "memory"]


async def test_a_budget_with_nothing_to_drop_reports_and_changes_nothing() -> None:
    cfg = with_spec(config([text_turn("ok")]), context_budget={"max_tokens": 1000})
    request, [build] = await sent(cfg, retrieval(Store(), k=2))
    assert len(request["messages"][-1]["content"]) == 3
    assert build.budget["trimmed"] is False
    assert build.dropped == []


async def test_a_budget_that_cannot_apply_does_not_open() -> None:
    async with Server() as server:
        with pytest.raises(OpenError):
            await server.open_agent(
                with_spec(config([text_turn("ok")]), context_budget={"max_tokens": 0})
            )


def test_the_spec_types_name_what_the_server_reads() -> None:
    assert MODEL
    assert ContextBudget.__required_keys__ == {"max_tokens"}
    assert System.__optional_keys__ == {"sections", "append", "replace_defaults"}
