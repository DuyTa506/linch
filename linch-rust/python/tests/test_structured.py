"""Structured output: the agent's or a run's schema, the retry, the parsed result."""

from __future__ import annotations

from typing import Any, cast

import pytest
from scripted import config, text_turn

from linch_rust import (
    InitEvent,
    InvalidOutputSchema,
    OutputSchema,
    ResultEvent,
    Server,
    VerificationEvent,
    callback,
)

INVOICE: OutputSchema = {
    "name": "invoice",
    "schema": {
        "type": "object",
        "properties": {"total": {"type": "number"}},
        "required": ["total"],
    },
}


def shaped(turns: list[dict[str, Any]], retries: int = 0) -> Any:
    agent = cast(dict[str, Any], config(turns))
    agent["spec"] = {**agent["spec"], "output_schema": INVOICE}
    if retries:
        agent["spec"]["structured_output_retries"] = retries
    return agent


async def test_a_retried_answer_ends_with_the_parsed_value_and_stop_sees_it() -> None:
    seen: list[Any] = []

    @callback("stop", "judge", 1)
    def judge(request: dict[str, Any]) -> dict[str, Any]:
        seen.append(request.get("structured_output"))
        return {"action": "allow"}

    turns = [text_turn("about four"), text_turn('```json\n{"total": 4}\n```')]
    async with Server() as server:
        agent = await server.open_agent(shaped(turns, retries=1), callbacks=[judge])
        run = await agent.run("s1", "Bill me")
        envelopes = [envelope async for envelope in run]
        result = await run.result()

    assert isinstance(result, ResultEvent)
    assert result.subtype == "success"
    assert result.structured_output == {"total": 4}
    assert seen == [{"total": 4}], "only the valid answer reaches stop"
    checks = [e.event for e in envelopes if isinstance(e.event, VerificationEvent)]
    assert [(c.verifier, c.action, c.attempt) for c in checks] == [("output_schema", "retry", 1)]


async def test_past_the_bound_the_run_fails_with_the_error() -> None:
    async with Server() as server:
        agent = await server.open_agent(shaped([text_turn("[1]")]))
        run = await agent.run("s1", "Bill me")
        result = await run.result()

    assert isinstance(result, ResultEvent) and result.subtype == "error"
    assert result.structured_output is None
    assert result.structured_error == "Expected a JSON object, got array"


async def test_a_run_brings_its_own_schema() -> None:
    person: OutputSchema = {"name": "person", "schema": {"type": "object", "required": ["name"]}}
    async with Server() as server:
        agent = await server.open_agent(config([text_turn('{"name": "Ada"}')]))
        run = await agent.run("s1", "Who?", output_schema=person)
        envelopes = [envelope async for envelope in run]
        result = await run.result()

        array: OutputSchema = {"name": "x", "schema": {"type": "array"}}
        bad = await agent.run("s2", "Who?", output_schema=array)
        with pytest.raises(InvalidOutputSchema) as raised:
            await bad.result()

    init = envelopes[0].event
    assert isinstance(init, InitEvent) and init.output_schema is not None
    assert init.output_schema["name"] == "person"
    assert isinstance(result, ResultEvent)
    assert result.structured_output == {"name": "Ada"}
    assert "array" in raised.value.reason
