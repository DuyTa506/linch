"""End-to-end accounting at the primary/compaction boundary.

These cases deliberately use the real default compaction strategy and a long
session history.  A summary and the subsequent primary turn report distinct
usage so a primary-only accumulator cannot accidentally satisfy the contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest


async def _run_compaction_case(
    *,
    max_tokens: int,
    summary_model: str = "offline-model",
    main_model: str = "offline-model",
) -> tuple[Any, list[Any], Any]:
    """Run one long-history turn and return provider, emitted events, and budget."""
    from linch import Agent, RunBudget
    from linch.providers import BaseProvider
    from linch.session import RunOptions
    from linch.sessions import InMemorySessionStore
    from linch.types import Message, TextBlock, Usage

    summary_usage = Usage(input_tokens=111, output_tokens=22)
    main_usage = Usage(input_tokens=333, output_tokens=44)

    class DistinctUsageProvider(BaseProvider):
        id = "accounting-long-history"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def context_window(self, model: str) -> int:
            return 1_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            summary = any(
                "Summarize the conversation so far." in block.text for block in req.system
            )
            self.calls.append("summary" if summary else "main")
            usage = summary_usage if summary else main_usage
            yield {
                "type": "message_start",
                "model": summary_model if summary else main_model,
            }
            yield {"type": "text_delta", "text": "summary" if summary else "main"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": usage}

    provider = DistinctUsageProvider()
    agent = Agent(
        model="offline-model",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        system_prompt="main system",
        max_output_tokens=100,
    )
    session = await agent.session()
    await session.append(
        [
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=[TextBlock(text=(f"seed-{index} " * 120).strip())],
            )
            for index in range(30)
        ]
    )
    budget = RunBudget(max_tokens=max_tokens)
    events = [event async for event in session.run("run main", RunOptions(budget=budget))]
    return provider, events, budget


@pytest.mark.asyncio
async def test_default_compaction_and_primary_share_one_run_ledger() -> None:
    """Removing an auxiliary receipt must lose the 111+22 summary usage."""
    from linch.events import ResultEvent, UsageEvent
    from linch.types import Usage

    provider, events, budget = await _run_compaction_case(max_tokens=10_000)

    result = next(event for event in events if isinstance(event, ResultEvent))
    usages = [event for event in events if isinstance(event, UsageEvent)]
    assert provider.calls == ["summary", "main"]
    assert [event.usage for event in usages] == [Usage(111, 22), Usage(333, 44)]
    assert usages[0].source == "compaction"
    assert usages[1].source is None
    assert result.total_usage == Usage(444, 66)
    assert budget.spent_tokens == 510
    assert result.usage_complete is True


@pytest.mark.asyncio
async def test_summary_budget_exhaustion_stops_before_primary_delivery() -> None:
    """Removing post-compaction admission allows an over-cap primary delivery."""
    from linch.events import BudgetEvent, ResultEvent
    from linch.types import Usage

    provider, events, budget = await _run_compaction_case(max_tokens=100)

    result = next(event for event in events if isinstance(event, ResultEvent))
    assert provider.calls == ["summary"]
    assert budget.spent_tokens == 133
    assert budget.exceeded
    assert result.subtype == "error"
    assert result.total_usage == Usage(111, 22)
    assert any(isinstance(event, BudgetEvent) and event.kind == "exceeded" for event in events)


@pytest.mark.asyncio
async def test_actual_models_price_each_settled_receipt() -> None:
    """Requested offline model must not overwrite normalized actual models."""
    from linch.events import ResultEvent, UsageEvent
    from linch.pricing import cost_usd
    from linch.types import Usage

    summary_model = "claude-haiku-4-5"
    main_model = "claude-sonnet-4-6"
    _, events, _ = await _run_compaction_case(
        max_tokens=10_000,
        summary_model=summary_model,
        main_model=main_model,
    )

    usages = [event for event in events if isinstance(event, UsageEvent)]
    result = next(event for event in events if isinstance(event, ResultEvent))
    expected_summary = cost_usd(Usage(111, 22), summary_model)
    expected_main = cost_usd(Usage(333, 44), main_model)
    assert expected_summary is not None and expected_main is not None
    assert usages[0].cost_usd == expected_summary
    assert usages[1].cost_usd == expected_main
    assert result.total_cost_usd == pytest.approx(expected_summary + expected_main)


@pytest.mark.asyncio
async def test_after_provider_stop_preserves_settled_primary_usage() -> None:
    """A stop hook cannot erase an already settled primary receipt."""
    from linch import Agent, HookResult
    from linch.events import ResultEvent, UsageEvent
    from linch.providers import BaseProvider
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    class Provider(BaseProvider):
        id = "after-provider-stop"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "paid output"}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(input_tokens=9, output_tokens=4),
            }

    class StopHook:
        def on_after_provider_call(self, ctx: Any) -> Any:
            return HookResult.stop("test", feedback="stopped after payment")

    agent = Agent(
        model="offline-model",
        provider=Provider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        hooks=[StopHook()],
    )
    session = await agent.session()
    events = [event async for event in session.run("go")]

    usage = next(event for event in events if isinstance(event, UsageEvent))
    result = next(event for event in events if isinstance(event, ResultEvent))
    assert usage.usage == Usage(9, 4)
    assert result.subtype == "error"
    assert result.total_usage == Usage(9, 4)


@pytest.mark.asyncio
async def test_fallback_deliveries_get_fresh_ids_under_one_logical_operation() -> None:
    """A retryable fallback is two physical deliveries, never one reused ID."""
    from linch import Agent
    from linch.errors import ProviderError
    from linch.events import ModelCallEvent, SystemEvent
    from linch.providers import BaseProvider
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    class Provider(BaseProvider):
        id = "accounted-fallback"

        def __init__(self) -> None:
            self.models: list[str] = []

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            self.models.append(req.model)
            if req.model == "primary":
                raise ProviderError("overloaded", status=529, retryable=True)
            yield {"type": "message_start", "model": "claude-haiku-4-5"}
            yield {"type": "text_delta", "text": "recovered"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage(3, 2)}

    provider = Provider()
    run_store = InMemoryRunStore()
    agent = Agent(
        model="primary",
        fallback_models=["backup"],
        provider=provider,
        session_store=InMemorySessionStore(),
        run_store=run_store,
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    events = [event async for event in session.run("go")]
    run_id = next(event.run_id for event in events if isinstance(event, SystemEvent))
    rows = await run_store.load_events(run_id)
    intents = [
        row.event.record
        for row in rows
        if isinstance(row.event, ModelCallEvent)
        and row.event.record_kind == "intent"
        and row.event.record["kind"] == "primary"
    ]

    assert provider.models == ["primary", "backup"]
    assert len(intents) == 2
    assert intents[0]["logical_call_id"] == intents[1]["logical_call_id"]
    assert intents[0]["delivery_attempt_id"] != intents[1]["delivery_attempt_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_input", ['{"secret": "native-leak"', '["native-leak"]'])
async def test_rejected_native_arguments_are_charged_but_never_durable(raw_input: str) -> None:
    """Malformed native input drains final usage without an SDK raw-argument leak."""
    import json

    from linch import Agent
    from linch.events import (
        ErrorEvent,
        ModelCallEvent,
        PartialAssistantEvent,
        ResultEvent,
        SystemEvent,
        UsageEvent,
        event_to_dict,
    )
    from linch.providers import BaseProvider
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage, message_to_dict

    class Provider(BaseProvider):
        id = "bad-native"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "visible"}
            yield {"type": "tool_use_start", "id": "tool-1", "name": "NeverRun"}
            yield {"type": "tool_use_input_delta", "id": "tool-1", "json_delta": raw_input}
            yield {"type": "tool_use_end", "id": "tool-1"}
            yield {
                "type": "message_end",
                "stop_reason": "tool_use",
                "usage": Usage(input_tokens=12, output_tokens=3),
            }

    run_store = InMemoryRunStore()
    agent = Agent(
        model="offline-model",
        provider=Provider(),
        session_store=InMemorySessionStore(),
        run_store=run_store,
        permissions={"mode": "skip-dangerous"},
        include_partial_messages=True,
    )
    session = await agent.session()
    events = [event async for event in session.run("go")]
    run_id = next(event.run_id for event in events if isinstance(event, SystemEvent))
    rows = await run_store.load_events(run_id)
    persisted = [event_to_dict(row.event) for row in rows]
    result = next(event for event in events if isinstance(event, ResultEvent))
    usage = next(event for event in events if isinstance(event, UsageEvent))
    receipt = next(
        row.event.record
        for row in rows
        if isinstance(row.event, ModelCallEvent)
        and row.event.record_kind == "receipt"
        and row.event.record["kind"] == "primary"
    )

    assert usage.usage == Usage(12, 3)
    assert result.subtype == "error"
    assert result.total_usage == Usage(12, 3)
    assert receipt["disposition"] == "rejected_native"
    assert not any(event.type.startswith("tool_call_") for event in events)
    assert any(isinstance(event, PartialAssistantEvent) for event in events)
    assert not any(isinstance(row.event, PartialAssistantEvent) for row in rows)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert "native-leak" not in json.dumps(persisted, sort_keys=True)
    assert "native-leak" not in json.dumps(
        [message_to_dict(message) for message in session.full_history], sort_keys=True
    )


def test_run_report_propagates_incomplete_usage_lower_bound() -> None:
    """The report must retain the terminal lower-bound marker verbatim."""
    from linch.events import ResultEvent
    from linch.reports import build_run_report
    from linch.types import Usage

    report = build_run_report(
        [
            ResultEvent(
                subtype="error",
                stop_reason="error",
                total_usage=Usage(input_tokens=2),
                duration_ms=1,
                usage_complete=False,
            )
        ]
    )
    assert report.final is not None
    assert report.final["usage_complete"] is False
