"""Live e2e (core): structured output, tool_choice, effort, temperature, budgets (vLLM, wire-checked)."""

from __future__ import annotations

import json
import statistics
from typing import Any

import pytest
from e2e_core_support import (
    MODEL,
    attempts,
    config,
    dump,
    fwd,  # noqa: F401  (fixture)
    needs_e2e,
    opened,
    run_to_end,
    spec,
)

from linch_rust import (
    BudgetEvent,
    ContextBuildEvent,
    ErrorEvent,
    InitEvent,
    LoopGuardEvent,
    PartialAssistantEvent,
    UsageEvent,
    VerificationEvent,
    callback,
    tool,
)

pytestmark = [pytest.mark.live, needs_e2e]

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
IMPOSSIBLE: dict[str, Any] = {
    "name": "answer",
    "schema": {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 10, "maximum": 5}},
        "required": ["n"],
        "additionalProperties": False,
    },
}


@tool(scope="read")
async def lookup(email: str) -> str:
    """Find a customer by email."""
    return "Ada Lovelace"


@tool(scope="read")
async def clock() -> str:
    """Return the time."""
    return "12:00"


@attempts("3b final tool run")
async def test_3b_the_final_tool_ends_the_run_with_a_valid_object(fwd: Any) -> None:
    cfg = config(fwd, spec(output_schema=PERSON, structured_output_retries=1, max_turns=4))
    async with opened(cfg) as (_s, agent):
        trace = await run_to_end(
            "3b-structured",
            agent,
            "Extract the person: Ada Lovelace was 36 years old when she died.",
            fwd=fwd,
        )
    [init] = trace.of(InitEvent)
    result = trace.result
    assert init.final_tool == "person" and "person" in init.tools
    assert not trace.starts("person") and not trace.ends("person")
    assert result.subtype == "success" and result.structured_error is None
    out = result.structured_output
    assert out is not None and set(out) == {"name", "age"} and isinstance(out["age"], int)
    assert out["age"] == 36 and "Lovelace" in out["name"]
    assert json.loads(result.final_text or "null") == out
    try:
        import jsonschema

        jsonschema.validate(out, PERSON["schema"])
    except ImportError:
        pass
    wire = fwd.chat()[-len(trace.of(UsageEvent)) :]
    assert all("response_format" not in r["body"] for r in wire)  # no schema on the wire
    assert any(t["function"]["name"] == "person" for t in wire[0]["body"]["tools"])


@attempts("3c unsatisfiable schema, final tool beside another tool")
async def test_3c_an_unsatisfiable_schema_fails_with_a_typed_error_after_the_retries(fwd: Any) -> None:
    cfg = config(fwd, spec(output_schema=IMPOSSIBLE, structured_output_retries=2, max_turns=8))
    async with opened(cfg, tools=(clock,)) as (_s, agent):
        mark = fwd.mark()
        trace = await run_to_end(
            "3c-unsatisfiable",
            agent,
            "Give the final answer with n = 7. Do not call any other tool; just answer.",
            fwd=fwd,
        )
        calls = len(fwd.chat()) - mark
    result = trace.result
    ver = trace.of(VerificationEvent)
    errors = trace.of(ErrorEvent)
    dump(
        "3c-unsatisfiable",
        {
            "provider_calls": calls,
            "verification": [(v.verifier, v.action, v.attempt) for v in ver],
            "error": errors[0].error if errors else None,
            "structured_error": result.structured_error,
            "tools_called": [s.tool_name for s in trace.starts()],
        },
    )
    assert not trace.starts(), "the model called another tool; model behaviour"
    assert result.subtype == "error" and errors and errors[0].error["name"] == "StructuredOutputError"
    assert [v.action for v in ver] == ["retry", "retry", "stop"]
    assert [v.attempt for v in ver[:2]] == [1, 2]
    assert result.structured_error
    assert calls == 3, f"provider calls {calls}"


async def test_3c_forced_only_variant_is_recorded(fwd: Any) -> None:
    """Observation, no verdict assertion: the final tool is the only tool, so it is forced by name."""
    cfg = config(fwd, spec(output_schema=IMPOSSIBLE, structured_output_retries=2, max_turns=8))
    async with opened(cfg) as (_s, agent):
        mark = fwd.mark()
        trace = await run_to_end("3c-forced-only", agent, "Give the final answer with n = 7.", fwd=fwd)
        calls = fwd.chat()[mark:]
    errors = trace.of(ErrorEvent)
    dump(
        "3c-forced-only",
        {
            "provider_calls": len(calls),
            "tool_choice_on_wire": [c["body"].get("tool_choice") for c in calls],
            "statuses": [c.get("status") for c in calls],
            "subtype": trace.result.subtype,
            "error": errors[0].error if errors else None,
            "verification": [(v.action, v.attempt) for v in trace.of(VerificationEvent)],
        },
    )


@attempts("4a required")
async def test_4a_required_makes_the_model_call_a_tool(fwd: Any) -> None:
    cfg = config(fwd, spec(tool_choice={"type": "required"}, max_turns=1))
    async with opened(cfg, tools=(lookup, clock)) as (_s, agent):
        trace = await run_to_end("4a-required", agent, "Say hello to the user in plain text.", fwd=fwd)
        wire = fwd.chat()[-1]["body"]
    first = trace.assistants()[0].message["content"]
    assert any(b["type"] == "tool_use" for b in first)
    assert wire["tool_choice"] == "required", wire.get("tool_choice")


@attempts("4b named")
async def test_4b_a_named_choice_calls_that_tool(fwd: Any) -> None:
    cfg = config(fwd, spec(tool_choice={"type": "tool", "name": "lookup"}, max_turns=1))
    async with opened(cfg, tools=(lookup, clock)) as (_s, agent):
        trace = await run_to_end("4b-named", agent, "Say hello to the user.", fwd=fwd)
        wire = fwd.chat()[-1]["body"]
    first = trace.assistants()[0].message["content"]
    assert [b["name"] for b in first if b["type"] == "tool_use"] == ["lookup"]
    assert wire["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}


@attempts("4c none")
async def test_4c_none_makes_the_model_call_no_tool(fwd: Any) -> None:
    cfg = config(fwd, spec(tool_choice={"type": "none"}, max_turns=3))
    async with opened(cfg, tools=(lookup, clock)) as (_s, agent):
        trace = await run_to_end(
            "4c-none", agent, "Call the lookup tool with email a@b.c and tell me the name.", fwd=fwd
        )
        wire = fwd.chat()[-1]["body"]
    assert not trace.starts()
    assert all(b["type"] != "tool_use" for a in trace.assistants() for b in a.message["content"])
    assert wire["tool_choice"] == "none"


@attempts("4d named tool not offered this turn")
async def test_4d_a_named_choice_the_turn_does_not_offer_goes_out_as_auto(fwd: Any) -> None:
    @callback("before_provider_call", "hide-lookup", 1)
    async def hide(request: dict[str, Any]) -> dict[str, Any]:
        body = json.loads(json.dumps(request["request"]))
        body["tools"] = [t for t in body["tools"] if t["name"] != "lookup"]
        return {"action": "rewrite", "request": body}

    cfg = config(fwd, spec(tool_choice={"type": "tool", "name": "lookup"}, max_turns=1))
    async with opened(cfg, tools=(lookup, clock), callbacks=(hide,)) as (_s, agent):
        trace = await run_to_end("4d-dropped", agent, "Say hello to the user.", fwd=fwd)
        wire = fwd.chat()[-1]["body"]
    metadata = trace.of(ContextBuildEvent)[0].metadata
    dump("4d-dropped", {"wire_tool_choice": wire.get("tool_choice"), "metadata": metadata, "tools": [t["function"]["name"] for t in wire["tools"]]})
    assert trace.of(ErrorEvent) == [] or trace.result.subtype == "error"  # max_turns ends it, not a 400
    assert [t["function"]["name"] for t in wire["tools"]] == ["clock"]
    assert wire.get("tool_choice") in (None, "auto")
    assert metadata.get("tool_choice_dropped") == "lookup"
    assert not [g for g in trace.of(LoopGuardEvent) if g.reason != "max_turns"]


# ---------------------------------------------------------------- effort

PROMPTS = {
    "mid": (
        "A train leaves at 9:40 and travels 150 km at 60 km/h, then waits 25 minutes, then "
        "travels 45 km at 90 km/h. At what time does it arrive? Answer with the time only."
    ),
    "p17": "What is 17 + 25? Answer with the number only.",
}
LEVELS = [
    ("low", "mid"),
    ("medium", "mid"),
    ("high", "mid"),
    ("xhigh", "mid"),
    ("max", "mid"),
    ("off", "mid"),
    ("low", "p17"),
    ("off", "p17"),
]


@pytest.mark.parametrize(("effort", "prompt_key"), LEVELS)
async def test_5_reasoning_effort_levels(fwd: Any, effort: str, prompt_key: str) -> None:
    thinking = {"type": "disabled"} if effort == "off" else {"type": "enabled"}
    extra: dict[str, Any] = {} if effort == "off" else {"effort": effort}
    cfg = config(fwd, spec(thinking=thinking, max_output_tokens=600, max_turns=2, **extra))
    async with opened(cfg) as (_s, agent):
        for rep in range(3):
            mark = fwd.mark()
            trace = await run_to_end(
                f"5-effort-{effort}-{prompt_key}", agent, PROMPTS[prompt_key], session=f"s{rep}", fwd=None
            )
            wire = fwd.chat()[mark:]
            body = wire[0]["body"]
            usage = wire[0]["usage"] or {}
            deltas = [d for d in trace.of(PartialAssistantEvent) if d.delta["kind"] == "thinking"]
            dump(
                "5-effort",
                {
                    "effort": effort,
                    "prompt": prompt_key,
                    "rep": rep,
                    "completion_tokens": usage.get("completion_tokens"),
                    "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                    "result_output_tokens": trace.result.total_usage["output_tokens"],
                    "thinking_deltas": len(deltas),
                    "wire_effort_fields": {
                        k: body[k] for k in body if "effort" in k or "reasoning" in k or "chat_template" in k
                    },
                    "subtype": trace.result.subtype,
                    "stop_reason": trace.result.stop_reason,
                    "errors": [e.error for e in trace.of(ErrorEvent)],
                },
            )
            assert not trace.of(ErrorEvent), trace.of(ErrorEvent)
            assert len(wire) == 1
            if effort == "off":
                assert not deltas and usage["completion_tokens_details"]["reasoning_tokens"] == 0
            elif effort in ("high", "max"):
                assert "xhigh" in json.dumps({k: v for k, v in body.items() if "effort" in k or "chat_template" in k})
            else:
                assert effort in json.dumps({k: v for k, v in body.items() if "effort" in k or "chat_template" in k})


def effort_means() -> dict[str, float]:
    from e2e_core_support import RESULTS

    rows = [json.loads(line) for line in (RESULTS / "5-effort.jsonl").read_text().splitlines()]
    by: dict[str, list[int]] = {}
    for row in rows:
        by.setdefault(f"{row['effort']}/{row['prompt']}", []).append(row["completion_tokens"])
    return {k: statistics.mean(v) for k, v in by.items()}


async def test_5d_thinking_off_is_cheaper_than_thinking_on_for_a_simple_prompt() -> None:
    means = effort_means()
    dump("5-effort-means", means)
    assert means["off/p17"] < 0.5 * means["low/p17"], means


@pytest.mark.xfail(
    reason="model behaviour: this vLLM/Qwen3.6 serves effort but its reasoning length does not "
    "rise with it (see 5-effort-means.jsonl and the probe in the report)",
    strict=False,
)
async def test_5c_reasoning_tokens_rise_with_effort() -> None:
    means = effort_means()
    assert means["low/mid"] < means["medium/mid"] <= means["xhigh/mid"], means


# ---------------------------------------------------------------- temperature


async def test_6a_temperature_zero_is_sent_and_gives_near_identical_text(fwd: Any) -> None:
    cfg = config(fwd, spec(temperature=0, max_output_tokens=60))
    texts: list[str] = []
    async with opened(cfg) as (_s, agent):
        for rep in range(3):
            mark = fwd.mark()
            trace = await run_to_end(
                "6a-temp0", agent, "Write one sentence about the moon.", session=f"t{rep}", fwd=None
            )
            body = fwd.chat()[mark]["body"]
            assert "temperature" in body and body["temperature"] == 0
            texts.append(trace.result.final_text or "")
    dump("6a-temp0", {"texts": texts})
    best = max(texts.count(t) for t in texts)
    assert best >= 2, texts


# ---------------------------------------------------------------- budgets


@tool(scope="read")
async def ping() -> str:
    """Ping the service."""
    return "pong"


@attempts("7b priced cost cap trips")
async def test_7b_a_tiny_cost_cap_ends_the_run_with_the_budget_error(fwd: Any) -> None:
    cfg = config(
        fwd,
        spec(budget={"max_cost_usd": 1e-9}, max_turns=6),
        pricing={MODEL: {"input": 1.0, "output": 2.0}},
    )
    async with opened(cfg, tools=(ping,)) as (_s, agent):
        mark = fwd.mark()
        trace = await run_to_end(
            "7b-cost-cap", agent, "Call ping, wait for the result, then say done.", fwd=fwd
        )
        calls = len(fwd.chat()) - mark
    budget = trace.of(BudgetEvent)
    errors = trace.of(ErrorEvent)
    dump("7b-cost-cap", {"calls": calls, "budget": [vars(b) for b in budget], "errors": [e.error for e in errors]})
    assert trace.starts("ping"), "the model did not call ping; model behaviour"
    assert [b.kind for b in budget][-1] == "exceeded"
    assert errors and errors[0].error["name"] == "BudgetExceededError"
    assert trace.result.subtype == "error"
    assert calls == 1, calls


@attempts("7c token cap trips")
async def test_7c_a_token_cap_ends_the_run_with_the_budget_error(fwd: Any) -> None:
    cfg = config(fwd, spec(budget={"max_tokens": 50}, max_turns=6))
    async with opened(cfg, tools=(ping,)) as (_s, agent):
        mark = fwd.mark()
        trace = await run_to_end(
            "7c-token-cap", agent, "Call ping, wait for the result, then say done.", fwd=fwd
        )
        calls = len(fwd.chat()) - mark
    errors = trace.of(ErrorEvent)
    assert trace.starts("ping")
    assert [b.kind for b in trace.of(BudgetEvent)][-1] == "exceeded"
    assert errors and errors[0].error["name"] == "BudgetExceededError"
    assert trace.result.subtype == "error" and calls == 1
