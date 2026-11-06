"""Approval: a `write` host tool in mode `default` suspends the run."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    Agent,
    Allow,
    Deny,
    ResultEvent,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
    tool,
)


@dataclass
class Suspended:
    agent: Agent
    run_id: str
    saved: list[str]


@pytest.fixture
async def suspended() -> AsyncIterator[Suspended]:
    """A run that asked to call `save` and suspended on it."""
    saved: list[str] = []

    @tool(scope="write")
    async def save(text: str) -> str:
        saved.append(text)
        return "saved"

    turns = [tool_turn("call_1", "save", {"text": "hello"}), text_turn("Done.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns, mode="default"), tools=[save])
        run = await agent.run("s1", "Save hello")
        stopped = await run.result()
        assert isinstance(stopped, SuspendedEvent)
        assert stopped.pending_request_ids == ["call_1"]
        assert saved == []
        yield Suspended(agent, run.id, saved)


async def test_approving_runs_the_call(suspended: Suspended) -> None:
    run = await suspended.agent.approve(suspended.run_id, {"call_1": Allow()})
    result = await run.result()

    assert run.id == suspended.run_id
    assert suspended.saved == ["hello"]
    assert isinstance(result, ResultEvent) and result.final_text == "Done."


async def test_denying_gives_the_model_the_reason(suspended: Suspended) -> None:
    run = await suspended.agent.approve(suspended.run_id, {"call_1": Deny("not now")})
    envelopes = [e async for e in run]

    assert suspended.saved == []
    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.is_error and "not now" in str(end.result)
    assert isinstance(envelopes[-1].event, ResultEvent)


async def test_resuming_a_suspended_run_suspends_it_again(suspended: Suspended) -> None:
    run = await suspended.agent.resume(suspended.run_id)
    again = await run.result()

    assert isinstance(again, SuspendedEvent) and again.pending_request_ids == ["call_1"]
    assert suspended.saved == []
