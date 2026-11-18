"""AskUser: a call suspends the run with an `input_request`, `Agent.answer` continues it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    Agent,
    Allow,
    Answer,
    InputRequestEvent,
    InvalidAnswer,
    NotSuspended,
    ResultEvent,
    RunTerminal,
    Server,
    SuspendedEvent,
    ToolCallEndEvent,
)
from linch_rust.errors import error_from

QUESTION = {
    "questions": [
        {
            "id": "db",
            "header": "Database",
            "question": "Which database?",
            "options": [
                {"label": "Postgres", "description": "durable"},
                {"label": "Memory", "description": "for tests"},
            ],
        }
    ]
}


@dataclass
class Asked:
    agent: Agent
    run_id: str
    request: InputRequestEvent


@pytest.fixture
async def asked() -> AsyncIterator[Asked]:
    """A run that called AskUser and suspended for the answer."""
    turns = [tool_turn("ask_1", "AskUser", QUESTION), text_turn("Postgres it is.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns, ask_user=True))
        run = await agent.run("s1", "Set up storage")
        envelopes = [e async for e in run]
        stopped = envelopes[-1].event
        assert isinstance(stopped, SuspendedEvent)
        assert stopped.status == "awaiting_input"
        assert stopped.pending_request_ids == ["ask_1"]
        request = envelopes[-2].event
        assert isinstance(request, InputRequestEvent)
        yield Asked(agent, run.id, request)


async def test_the_request_carries_the_questions(asked: Asked) -> None:
    assert asked.request.request_id == "ask_1"
    assert asked.request.kind == "ask_user"
    assert asked.request.prompt == "Which database?"
    [question] = asked.request.questions
    assert [option["label"] for option in question["options"]] == ["Postgres", "Memory"]


async def test_an_answer_becomes_the_result_and_the_run_finishes(asked: Asked) -> None:
    run = await asked.agent.answer(asked.run_id, {"ask_1": Answer({"db": "Postgres"})})
    envelopes = [e async for e in run]

    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    assert end.result["content"][0]["text"] == '{"answers":{"db":"Postgres"}}'
    result = envelopes[-1].event
    assert isinstance(result, ResultEvent) and result.final_text == "Postgres it is."
    again = await asked.agent.answer(asked.run_id, {"ask_1": Answer({"db": "Memory"})})
    with pytest.raises(RunTerminal):
        await again.result()


async def test_a_declined_answer_passes_the_note_on(asked: Asked) -> None:
    run = await asked.agent.answer(asked.run_id, {"ask_1": Answer.declined("ask me later")})
    envelopes = [e async for e in run]

    [end] = [e.event for e in envelopes if isinstance(e.event, ToolCallEndEvent)]
    text = end.result["content"][0]["text"]
    assert text.startswith("The user declined to answer.") and "ask me later" in text


async def test_an_answer_that_does_not_fit_leaves_the_run_waiting(asked: Asked) -> None:
    for answers in (
        {},
        {"ask_1": Answer({"db": ["Postgres"]})},
        {"ask_2": Answer({"db": "Postgres"})},
    ):
        run = await asked.agent.answer(asked.run_id, answers)
        with pytest.raises(InvalidAnswer) as refused:
            await run.result()
        assert refused.value.reason

    approve = await asked.agent.approve(asked.run_id, {"ask_1": Allow()})
    with pytest.raises(NotSuspended):
        await approve.result()
    run = await asked.agent.answer(asked.run_id, {"ask_1": Answer({"db": "Memory"})})
    assert isinstance(await run.result(), ResultEvent)


def test_invalid_answer_has_its_code_and_reason() -> None:
    error = error_from({"code": -32032, "message": "no", "data": {"reason": "q1 has no answer"}})

    assert isinstance(error, InvalidAnswer) and error.reason == "q1 has no answer"
