"""Tests for linch.pricing — Feature B (RED until pricing.py is created).

NOTE: linch imports inside test functions so tests survive sys.modules resets.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Unit tests — ModelPricing + cost_usd
# ---------------------------------------------------------------------------


def test_known_model_returns_cost():
    """cost_usd sums input + output for a known model."""
    from linch.pricing import cost_usd
    from linch.types import Usage

    # $3.00/1M input + $15.00/1M output
    # 1_000 input = $0.003000; 500 output = $0.007500 → $0.010500
    usage = Usage(input_tokens=1_000, output_tokens=500)
    result = cost_usd(usage, "claude-sonnet-4-6")
    assert result is not None
    assert abs(result - 0.0105) < 1e-9


def test_cost_includes_all_four_token_buckets():
    """All four token buckets contribute to cost independently."""
    from linch.pricing import cost_usd
    from linch.types import Usage

    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    result = cost_usd(usage, "claude-sonnet-4-6")
    assert result is not None
    # $3.00 (input) + $0.00 (output) + $0.30 (cache_read) + $3.75 (cache_creation) = $7.05
    assert abs(result - 7.05) < 1e-6


def test_unknown_model_returns_none():
    """cost_usd returns None for unrecognised model IDs — never silently free."""
    from linch.pricing import cost_usd
    from linch.types import Usage

    result = cost_usd(Usage(), "gpt-unknown-xyz-9999")
    assert result is None


def test_zero_usage_zero_cost():
    """All-zero Usage yields $0.00 for a known model."""
    from linch.pricing import cost_usd
    from linch.types import Usage

    result = cost_usd(Usage(), "claude-sonnet-4-6")
    assert result is not None
    assert result == 0.0


def test_cache_read_cheaper_than_cache_creation():
    """For every priced model, cache_read < cache_creation (guards rate swap)."""
    from linch.pricing import _DEFAULT_PRICING

    for model_id, pricing in _DEFAULT_PRICING.items():
        assert pricing.cache_read < pricing.cache_creation, (
            f"{model_id}: cache_read ({pricing.cache_read}) must be cheaper than "
            f"cache_creation ({pricing.cache_creation})"
        )


def test_cache_read_cheaper_than_input():
    """For every priced model, cache_read < input (cache hits are discounted)."""
    from linch.pricing import _DEFAULT_PRICING

    for model_id, pricing in _DEFAULT_PRICING.items():
        assert pricing.cache_read < pricing.input, (
            f"{model_id}: cache_read ({pricing.cache_read}) must be cheaper than "
            f"input ({pricing.input})"
        )


def test_custom_table_override():
    """table= kwarg lets callers inject their own pricing without mutating globals."""
    from linch.pricing import ModelPricing, cost_usd
    from linch.types import Usage

    custom_table = {
        "my-model": ModelPricing(input=1.0, output=2.0, cache_read=0.1, cache_creation=1.25)
    }
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    result = cost_usd(usage, "my-model", table=custom_table)
    assert result is not None
    # $1.00 + $2.00 = $3.00
    assert abs(result - 3.0) < 1e-9


def test_custom_table_unknown_falls_through_to_none():
    """Unknown model in a custom table returns None, not a KeyError."""
    from linch.pricing import cost_usd
    from linch.types import Usage

    result = cost_usd(Usage(), "no-such-model", table={})
    assert result is None


def test_all_claude_models_priced():
    """Core claude-* model IDs all have a pricing entry."""
    from linch.pricing import _DEFAULT_PRICING

    expected = {
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    }
    missing = expected - set(_DEFAULT_PRICING.keys())
    assert not missing, f"missing pricing entries: {missing}"


# ---------------------------------------------------------------------------
# Event round-trip tests (RED until events.py adds cost fields)
# ---------------------------------------------------------------------------


def test_usage_event_cost_fields_survive_round_trip():
    """UsageEvent with cost fields serialises and deserialises correctly."""
    from linch.events import UsageEvent, event_from_dict, event_to_dict
    from linch.types import Usage

    event = UsageEvent(
        usage=Usage(input_tokens=100, output_tokens=50),
        cumulative=Usage(input_tokens=200, output_tokens=100),
        cost_usd=0.0015,
        cumulative_cost_usd=0.003,
    )
    d = event_to_dict(event)
    assert d["cost_usd"] == pytest.approx(0.0015)
    assert d["cumulative_cost_usd"] == pytest.approx(0.003)

    restored = event_from_dict(d)
    assert restored.type == "usage"
    assert restored.cost_usd == pytest.approx(0.0015)
    assert restored.cumulative_cost_usd == pytest.approx(0.003)


def test_usage_event_missing_cost_defaults_to_none():
    """Pre-Feature-B persisted dicts (no cost fields) still deserialise cleanly."""
    from linch.events import event_from_dict

    d = {
        "type": "usage",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        "cumulative": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        # no cost_usd or cumulative_cost_usd
    }
    event = event_from_dict(d)
    assert event.type == "usage"
    assert event.cost_usd is None
    assert event.cumulative_cost_usd is None


def test_result_event_total_cost_survives_round_trip():
    """ResultEvent with total_cost_usd serialises and deserialises correctly."""
    from linch.events import ResultEvent, event_from_dict, event_to_dict
    from linch.types import Usage

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

    restored = event_from_dict(d)
    assert restored.type == "result"
    assert restored.total_cost_usd == pytest.approx(0.0085)


def test_result_event_missing_cost_defaults_to_none():
    """Pre-Feature-B ResultEvent dicts (no total_cost_usd) still deserialise."""
    from linch.events import event_from_dict

    d = {
        "type": "result",
        "subtype": "success",
        "stop_reason": "end_turn",
        "total_usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        "duration_ms": 500,
        "final_text": "done",
        # no total_cost_usd
    }
    event = event_from_dict(d)
    assert event.type == "result"
    assert event.total_cost_usd is None


# ---------------------------------------------------------------------------
# End-to-end: loop threads cost through UsageEvent and ResultEvent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_emits_cost_on_usage_and_result_events():
    """FakeProvider with a known priced model → UsageEvent.cost_usd + ResultEvent.total_cost_usd."""
    from linch import Agent
    from linch.providers.base import BaseProvider
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools
    from linch.types import Usage

    # claude-sonnet-4-6: $3/1M input, $15/1M output
    # 1000 input + 500 output = $0.003 + $0.0075 = $0.0105
    test_usage = Usage(input_tokens=1_000, output_tokens=500)

    class _PricedProvider(BaseProvider):
        id = "fake-priced"

        def context_window(self, model: str) -> int:
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "hello"}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": test_usage,
                "provider_metadata": None,
            }

    agent = Agent(
        model="claude-sonnet-4-6",
        provider=_PricedProvider(),
        tools=empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()

    usage_events = []
    result_event = None
    async for event in session.run("hi"):
        if event.type == "usage":
            usage_events.append(event)
        if event.type == "result":
            result_event = event

    assert usage_events, "expected at least one UsageEvent"
    u = usage_events[-1]
    assert u.cost_usd is not None
    assert abs(u.cost_usd - 0.0105) < 1e-9

    assert result_event is not None
    assert result_event.total_cost_usd is not None
    assert abs(result_event.total_cost_usd - 0.0105) < 1e-9
