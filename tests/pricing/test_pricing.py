"""Tests for model pricing and cost fields."""

from __future__ import annotations

import pytest

from linch.pricing import _DEFAULT_PRICING, ModelPricing, cost_usd
from linch.types import Usage
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


def _usage_payload(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return dict(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )


@pytest.mark.parametrize(
    ("usage", "expected", "tolerance"),
    [
        ({"input_tokens": 1_000, "output_tokens": 500}, 0.0105, 1e-9),
        (
            {
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_read_tokens": 1_000_000,
                "cache_creation_tokens": 1_000_000,
            },
            7.05,
            1e-6,
        ),
        ({}, 0.0, 0.0),
    ],
    ids=["input-output", "all-token-buckets", "zero-usage"],
)
def test_known_cost(usage, expected, tolerance):
    result = cost_usd(Usage(**usage), "claude-sonnet-4-6")
    assert result is not None
    assert abs(result - expected) < tolerance if tolerance else result == expected


@pytest.mark.parametrize(
    ("model", "table"),
    [("gpt-unknown-xyz-9999", None), ("no-such-model", {})],
    ids=["default-table", "custom-empty-table"],
)
def test_unknown_model_returns_none(model, table):
    assert cost_usd(Usage(), model, table=table) is None


def test_cache_read_is_discounted():
    for model_id, pricing in _DEFAULT_PRICING.items():
        for other in ("cache_creation", "input"):
            assert pricing.cache_read < getattr(pricing, other), (
                f"{model_id}: cache_read ({pricing.cache_read}) must be cheaper than "
                f"{other} ({getattr(pricing, other)})"
            )


def test_custom_table_override():
    custom_table = {
        "my-model": ModelPricing(input=1.0, output=2.0, cache_read=0.1, cache_creation=1.25)
    }
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    result = cost_usd(usage, "my-model", table=custom_table)
    assert result is not None
    assert abs(result - 3.0) < 1e-9


def test_all_claude_models_priced():
    expected = {
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    }
    missing = expected - set(_DEFAULT_PRICING.keys())
    assert not missing, f"missing pricing entries: {missing}"


@pytest.mark.parametrize("with_cost", [True, False], ids=["cost-fields", "legacy"])
def test_usage_event_cost_round_trip(with_cost):
    from linch.events import UsageEvent, event_from_dict, event_to_dict

    if with_cost:
        event = UsageEvent(
            usage=Usage(input_tokens=100, output_tokens=50),
            cumulative=Usage(input_tokens=200, output_tokens=100),
            cost_usd=0.0015,
            cumulative_cost_usd=0.003,
        )
        d = event_to_dict(event)
        assert d["cost_usd"] == pytest.approx(0.0015)
        assert d["cumulative_cost_usd"] == pytest.approx(0.003)
    else:
        d = {
            "type": "usage",
            "usage": _usage_payload(100, 50),
            "cumulative": _usage_payload(100, 50),
        }

    restored = event_from_dict(d)
    assert restored.type == "usage"
    if with_cost:
        assert restored.cost_usd == pytest.approx(0.0015)
        assert restored.cumulative_cost_usd == pytest.approx(0.003)
    else:
        assert restored.cost_usd is None
        assert restored.cumulative_cost_usd is None


@pytest.mark.parametrize("with_cost", [True, False], ids=["cost-field", "legacy"])
def test_result_event_cost_round_trip(with_cost):
    from linch.events import ResultEvent, event_from_dict, event_to_dict

    if with_cost:
        event = ResultEvent(
            subtype="success",
            stop_reason="end_turn",
            total_usage=Usage(input_tokens=500, output_tokens=200),
            duration_ms=1234,
            final_text="done",
            total_cost_usd=0.0085,
        )
        d = event_to_dict(event)
        assert d["total_cost_usd"] == pytest.approx(0.0085)
    else:
        d = {
            "type": "result",
            "subtype": "success",
            "stop_reason": "end_turn",
            "total_usage": _usage_payload(100, 50),
            "duration_ms": 500,
            "final_text": "done",
        }

    restored = event_from_dict(d)
    assert restored.type == "result"
    if with_cost:
        assert restored.total_cost_usd == pytest.approx(0.0085)
    else:
        assert restored.total_cost_usd is None


@pytest.mark.asyncio
async def test_loop_emits_cost_on_usage_and_result_events():
    test_usage = Usage(input_tokens=1_000, output_tokens=500)
    agent = make_agent(
        ScriptedProvider([TextTurn("hello", usage=test_usage)]),
        model="claude-sonnet-4-6",
        run_store=None,
    )
    session = await agent.session()

    events = [event async for event in session.run("hi")]
    usage_events = [event for event in events if event.type == "usage"]
    result_event = next((event for event in events if event.type == "result"), None)

    assert usage_events, "expected at least one UsageEvent"
    u = usage_events[-1]
    assert u.cost_usd is not None
    assert abs(u.cost_usd - 0.0105) < 1e-9

    assert result_event is not None
    assert result_event.total_cost_usd is not None
    assert abs(result_event.total_cost_usd - 0.0105) < 1e-9
