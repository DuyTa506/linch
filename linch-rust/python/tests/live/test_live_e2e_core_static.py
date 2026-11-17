"""Live e2e (core): the checks that send no model call: capabilities, models, open-time refusals."""

from __future__ import annotations

import json
from typing import Any

import pytest
from e2e_core_support import MODEL, UPSTREAM, config, dump, needs_e2e, opened, spec
from live_core_support import live_server

from linch_rust import ModelNotFound, OpenError, RpcError, tool

pytestmark = [pytest.mark.live, needs_e2e]


@tool(scope="read")
async def build() -> str:
    """Build the project."""
    return "built"


@tool(scope="read")
async def deploy() -> str:
    """Deploy the project."""
    return "deployed"


async def open_outcome(label: str, cfg: Any, tools: tuple[Any, ...] = ()) -> str:
    """Open `cfg` on a fresh server; return `opened` or the refusal as `code: message`."""
    async with live_server() as server:
        try:
            agent = await server.open_agent(cfg, tools=list(tools))
            await agent.close()
            outcome = "opened"
        except RpcError as error:
            outcome = f"{type(error).__name__} {getattr(error, 'code', '?')}: {error}"
    dump("static", {"case": label, "outcome": outcome})
    return outcome


async def test_13_capabilities_and_models_from_the_live_server() -> None:
    cfg = config(None)
    async with live_server() as server:
        caps = await server.provider_capabilities(cfg)
        models = await server.list_models(cfg["provider"])
        info = await server.model_info(cfg["provider"], MODEL)
        with pytest.raises(ModelNotFound) as missing:
            await server.model_info(cfg["provider"], "no-such-model-xyz")
    dump(
        "static",
        {
            "case": "13",
            "caps": {
                "context_window": caps.context_window,
                "context_source": caps.context_source,
                "output_schema": caps.output_schema,
                "tool_choice": vars(caps.tool_choice),
                "thinking": vars(caps.thinking),
            },
            "models": [m.id for m in models],
            "truncated": models.truncated,
            "model_info": str(info),
            "missing": [type(missing.value).__name__, getattr(missing.value, "code", None)],
        },
    )
    assert UPSTREAM
    assert caps.context_window == 131072 and caps.context_source == "server"
    assert caps.output_schema == "final_tool"  # 3a
    assert caps.tool_choice.required and caps.tool_choice.named
    assert MODEL in [m.id for m in models]
    assert info.id == MODEL
    assert getattr(missing.value, "code", None) == -32036


async def test_7a_a_cost_cap_without_a_price_is_refused_at_open() -> None:
    cfg = config(None, spec(budget={"max_cost_usd": 0.01}))
    outcome = await open_outcome("7a unpriced cap", cfg)
    assert outcome.startswith("OpenError") and "-32011" in outcome
    assert MODEL in outcome
    # With a price the same agent opens.
    priced = config(
        None,
        spec(budget={"max_cost_usd": 0.01}),
        pricing={MODEL: {"input": 1.0, "output": 2.0}},
    )
    assert await open_outcome("7a priced cap", priced) == "opened"
    # A token cap alone opens without a price.
    assert await open_outcome("7a token cap", config(None, spec(budget={"max_tokens": 100}))) == "opened"


@pytest.mark.parametrize("ms", [0, 99, 100])
async def test_7d_call_timeout_ms_bounds(ms: int) -> None:
    outcome = await open_outcome(f"7d call_timeout_ms={ms}", config(None, spec(call_timeout_ms=ms)))
    if ms < 100:
        assert outcome.startswith("OpenError") and "-32011" in outcome and "call_timeout_ms" in outcome
    else:
        assert outcome == "opened"


@pytest.mark.parametrize("temperature", [0, 0.0, 1.0, 2.0])
async def test_6b_temperature_in_range_opens(temperature: float) -> None:
    out = await open_outcome(f"6b temperature={temperature}", config(None, spec(temperature=temperature)))
    assert out == "opened"


@pytest.mark.parametrize("temperature", [2.5, -0.1])
async def test_6b_temperature_out_of_range_is_refused(temperature: float) -> None:
    out = await open_outcome(f"6b temperature={temperature}", config(None, spec(temperature=temperature)))
    assert out.startswith("OpenError") and "temperature" in out


async def test_6b_temperature_nan_is_refused_by_someone() -> None:
    """Run last: Python's json writes a bare NaN; record who refuses it."""
    cfg = config(None, spec(temperature=float("nan")))
    async with live_server() as server:
        try:
            agent = await server.open_agent(cfg)
            await agent.close()
            outcome = "opened"
        except Exception as error:  # noqa: BLE001
            outcome = f"{type(error).__name__} {getattr(error, 'code', '?')}: {error}"
    dump("static", {"case": "6b temperature=NaN", "outcome": outcome})
    assert outcome != "opened"


POLICY: dict[str, Any] = {"prerequisites": [{"tool": "deploy", "requires": ["build"]}]}


async def test_9c_a_policy_and_a_forced_choice_are_refused_at_open() -> None:
    cases = {
        "required + prerequisite": spec(policy=POLICY, tool_choice={"type": "required"}, max_turns=4),
        "none + prerequisite": spec(policy=POLICY, tool_choice={"type": "none"}, max_turns=4),
        "named deploy + prerequisite": spec(
            policy=POLICY, tool_choice={"type": "tool", "name": "deploy"}, max_turns=4
        ),
        "none + gate succeeded": spec(
            policy={"gates": [{"name": "built", "succeeded": ["build"]}]},
            tool_choice={"type": "none"},
            max_turns=4,
        ),
    }
    for label, sp in cases.items():
        out = await open_outcome(f"9c {label}", config(None, sp), (build, deploy))
        assert out.startswith("OpenError") and "-32011" in out, (label, out)
    # Controls: the policy alone, and auto, open.
    assert await open_outcome("9c policy only", config(None, spec(policy=POLICY, max_turns=4)), (build, deploy)) == "opened"
    assert (
        await open_outcome(
            "9c policy + auto", config(None, spec(policy=POLICY, tool_choice={"type": "auto"})), (build, deploy)
        )
        == "opened"
    )


async def test_4e_stdio_has_no_admission_for_a_forced_choice() -> None:
    required = spec(tool_choice={"type": "required"})  # no max_turns, no final tool
    out = await open_outcome("4e stdio required, no max_turns", config(None, required), (build, deploy))
    dump("static", {"case": "4e stdio verdict", "outcome": out})
    assert out == "opened"


def test_json_nan_note() -> None:
    # Documents what the client's encoder does with NaN, for the 6b record.
    dump("static", {"case": "6b json.dumps(nan)", "outcome": json.dumps({"t": float("nan")})})
