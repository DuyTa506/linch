"""Live: structured output through the final tool, and `tool_choice`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from live_core_support import (
    ONLY_DEEPSEEK,
    PROVIDERS,
    VLLM,
    Live,
    ask,
    drive,
    live_server,
    make_config,
    retry_once,
)

from linch_rust import ContextBuildEvent, InitEvent, LoopGuardEvent, RpcError, tool

pytestmark = pytest.mark.live

PERSON: dict[str, Any] = {
    "name": "person",
    "description": "The person described in the text.",
    "schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
        "additionalProperties": False,
    },
}
PROMPT = "Extract the person: Ada Lovelace was 36 years old when she died."


def check_person(trace: Any) -> None:
    result = trace.result
    assert result.subtype == "success", result
    assert result.structured_output is not None and result.structured_error is None
    assert result.structured_output["age"] == 36
    assert "Lovelace" in result.structured_output["name"]
    assert set(result.structured_output) == {"name", "age"}
    assert json.loads(result.final_text or "null") == result.structured_output


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_the_final_tool_returns_a_schema_valid_answer_without_running(live: Live) -> None:
    config = make_config(
        live, live.spec(output_schema=PERSON, structured_output_retries=1, max_turns=4)
    )
    trace = await ask(live, PROMPT, config=config)
    check_person(trace)
    [init] = trace.of(InitEvent)
    # Neither DeepSeek nor vLLM has a native schema: the run offers a final tool.
    assert init.final_tool == "person"
    assert trace.of(ContextBuildEvent)[0].selected_tools == ["person"]
    assert not trace.starts("person") and not trace.ends("person")
    calls = [b for a in trace.assistants() for b in a.message["content"] if b["type"] == "tool_use"]
    assert calls and calls[-1]["name"] == "person"


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_structured_output_with_thinking_on(live: Live) -> None:
    # DeepSeek refuses a forced choice while it thinks, so the tool is only offered.
    config = make_config(
        live,
        live.spec(
            thinking={"type": "enabled"},
            output_schema=PERSON,
            structured_output_retries=2,
            max_output_tokens=1500 if live is VLLM else 800,
            max_turns=4,
        ),
    )
    check_person(await ask(live, PROMPT, config=config))


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_per_run_schema_replaces_the_agents(live: Live) -> None:
    other: dict[str, Any] = {
        "name": "city",
        "schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    }
    config = make_config(live, live.spec(output_schema=PERSON, max_turns=4))
    trace = await ask(
        live, "Extract the city: I live in Paris.", config=config, output_schema=other
    )
    assert trace.result.structured_output is not None
    assert "Paris" in trace.result.structured_output["city"]
    assert trace.of(InitEvent)[0].output_schema is not None


def lookup_tool(seen: list[str]) -> Any:
    @tool(scope="read")
    async def lookup(email: str) -> str:
        """Find a customer by email."""
        seen.append(email)
        return "Ada Lovelace"

    return lookup


@tool(scope="read")
async def clock() -> str:
    """Return the time."""
    return "12:00"


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_a_named_tool_choice_forces_that_tool(live: Live) -> None:
    seen: list[str] = []
    config = make_config(
        live, live.spec(tool_choice={"type": "tool", "name": "lookup"}, max_turns=1)
    )
    trace = await ask(
        live, "Say hello to the user.", tools=(lookup_tool(seen), clock), config=config
    )
    first = trace.assistants()[0].message["content"]
    assert [b["name"] for b in first if b["type"] == "tool_use"] == ["lookup"]
    # A forced choice holds every turn: the run ends on max_turns, not on text.
    assert [g.reason for g in trace.of(LoopGuardEvent)] == ["max_turns"]
    assert trace.result.subtype == "error"


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_tool_choice_required_makes_the_model_call_some_tool(live: Live) -> None:
    seen: list[str] = []
    config = make_config(live, live.spec(tool_choice={"type": "required"}, max_turns=1))
    trace = await ask(
        live, "Say hello to the user.", tools=(lookup_tool(seen), clock), config=config
    )
    first = trace.assistants()[0].message["content"]
    assert any(b["type"] == "tool_use" for b in first)


@pytest.mark.parametrize("live", PROVIDERS)
@retry_once
async def test_tool_choice_none_and_a_per_run_override(live: Live) -> None:
    seen: list[str] = []
    config = make_config(live, live.spec(tool_choice={"type": "none"}, max_turns=3))
    trace = await ask(
        live,
        "Call the lookup tool with email a@b.c and tell me the name.",
        tools=(lookup_tool(seen), clock),
        config=config,
    )
    assert seen == [] and not trace.starts()
    assert all(b["type"] != "tool_use" for a in trace.assistants() for b in a.message["content"])

    # The run's own choice replaces the agent's.
    override = await ask(
        live,
        "Call the lookup tool with email a@b.c and tell me the name.",
        tools=(lookup_tool(seen), clock),
        config=make_config(live, live.spec(tool_choice={"type": "none"}, max_turns=4)),
        tool_choice={"type": "auto"},
    )
    assert seen == ["a@b.c"] and "Lovelace" in (override.result.final_text or "")
    assert override.of(InitEvent)[0].tool_choice == {"type": "auto"}


@pytest.mark.parametrize("live", ONLY_DEEPSEEK)
async def test_deepseek_refuses_a_forced_choice_while_it_thinks(live: Live) -> None:
    async with live_server() as server:
        config = make_config(
            live,
            live.spec(thinking={"type": "enabled"}, tool_choice={"type": "required"}),
        )
        with pytest.raises(RpcError) as refused:
            agent = await server.open_agent(config, tools=[lookup_tool([])])
            await drive(agent, await agent.run("s1", "hi"))
    assert "thinking" in str(refused.value).lower() or "tool_choice" in str(refused.value).lower()


@pytest.mark.parametrize("live", [PROVIDERS[0]])
@retry_once
async def test_vllm_forced_tool_choice_works_with_thinking_on(live: Live) -> None:
    seen: list[str] = []
    config = make_config(
        live,
        live.spec(
            thinking={"type": "enabled"},
            tool_choice={"type": "tool", "name": "lookup"},
            max_turns=1,
            max_output_tokens=1500,
        ),
    )
    trace = await ask(live, "Look up a@b.c.", tools=(lookup_tool(seen), clock), config=config)
    first = trace.assistants()[0].message["content"]
    assert [b["name"] for b in first if b["type"] == "tool_use"] == ["lookup"]
