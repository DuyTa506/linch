"""End-to-end accounting at the primary/compaction boundary.

These cases deliberately use the real default compaction strategy and a long
session history.  A summary and the subsequent primary turn report distinct
usage so a primary-only accumulator cannot accidentally satisfy the contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.runs import pin_run_contract
from tests.support.stores import memory_session_store


class _NeverCalledProvider:
    """Count calls and fail if a resume path dispatches unexpectedly."""

    id = "never-called"

    def __init__(self) -> None:
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        raise AssertionError("resume must not dispatch a provider call")
        yield {}


async def _run_compaction_case(
    *,
    max_tokens: int,
    summary_model: str = "offline-model",
    main_model: str = "offline-model",
) -> tuple[Any, list[Any], Any]:
    """Run one long-history turn and return provider, emitted events, and budget."""
    from linch import RunBudget
    from linch.providers import BaseProvider
    from linch.session import RunOptions
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
    agent = make_agent(
        provider,
        model="offline-model",
        session_store=memory_session_store(),
        run_store=None,
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
async def test_after_provider_stop_preserves_settled_primary_usage() -> None:
    """A stop hook cannot erase an already settled primary receipt."""
    from linch import HookResult
    from linch.events import ResultEvent, UsageEvent
    from linch.types import Usage

    class StopHook:
        def on_after_provider_call(self, ctx: Any) -> Any:
            return HookResult.stop("test", feedback="stopped after payment")

    agent = make_agent(
        ScriptedProvider([TextTurn("paid output", usage=Usage(input_tokens=9, output_tokens=4))]),
        run_store=None,
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
async def test_rejected_native_arguments_are_charged_but_never_durable() -> None:
    """Malformed native input drains final usage without an SDK raw-argument leak."""
    import json

    raw_input = '["native-leak"]'

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
            # A valid sibling must not escape the rejected-native terminal
            # path when another native call had no valid JSON object.
            yield {"type": "tool_use_start", "id": "tool-2", "name": "AlsoNeverRun"}
            yield {"type": "tool_use_input_delta", "id": "tool-2", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "tool-2"}
            yield {
                "type": "message_end",
                "stop_reason": "tool_use",
                "usage": Usage(input_tokens=12, output_tokens=3),
            }

    run_store = InMemoryRunStore()
    agent = Agent(
        model="offline-model",
        provider=Provider(),
        reliability=None,
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


@pytest.mark.asyncio
async def test_forced_compaction_budget_stops_before_rebuilding_main_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paid forced summary must be drained and admitted before retry construction."""
    from linch import Agent, RunBudget
    from linch.context import ContextBuildResult
    from linch.errors import ContextLengthError
    from linch.events import BudgetEvent, ModelCallEvent, ResultEvent, SystemEvent
    from linch.loop import streaming
    from linch.providers import BaseProvider
    from linch.run_store import InMemoryRunStore
    from linch.session import RunOptions
    from linch.sessions import InMemorySessionStore
    from linch.types import SystemBlock, Usage

    class ProbeHook:
        resume_policy_id = "test.forced-compaction-budget"
        resume_policy_version = "1"
        resume_policy_config: dict[str, object] = {}

        def __init__(self) -> None:
            self.context_calls = 0
            self.before_calls = 0
            self.provider_start_calls = 0

        async def build_context(self, session: Any, turn_index: int) -> ContextBuildResult | None:
            self.context_calls += 1
            return None

        def on_before_provider_call(self, ctx: Any) -> None:
            self.before_calls += 1

        def on_provider_call_start(self, ctx: Any) -> None:
            self.provider_start_calls += 1

    class Provider(BaseProvider):
        id = "forced-compaction-budget"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            summary = any(block.text == "forced accounting summary" for block in req.system)
            self.calls.append("summary" if summary else "main")
            if not summary:
                raise AssertionError("main provider must not start after the paid forced summary")
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "summary"}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": Usage(input_tokens=111, output_tokens=22),
            }

    original_stream_turn = streaming._accounted_primary_stream
    stream_attempts = 0

    async def context_length_then_normal_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        nonlocal stream_attempts
        stream_attempts += 1
        if stream_attempts == 1:
            raise ContextLengthError("force compaction")
        async for item in original_stream_turn(*args, **kwargs):
            yield item

    async def paid_forced_compaction(
        session: Any,
        agent: Any,
        signal: Any,
        *,
        model: str | None = None,
        auxiliary_caller: Any = None,
        logical_call_id: str | None = None,
    ) -> bool:
        assert auxiliary_caller is not None
        await auxiliary_caller.text(
            logical_call_id=logical_call_id or "forced-compaction",
            model=model or agent.model,
            system=[SystemBlock(text="forced accounting summary")],
            messages=list(session.provider_view),
            max_output_tokens=32,
            signal=signal,
        )
        messages = len(session.provider_view)
        session.last_compaction_info = {
            "type": "compaction",
            "messages_before": messages,
            "messages_after": messages,
            "tokens_before": 0,
            "tokens_after": 0,
            "strategy": "forced-accounting-test",
            "model": model or agent.model,
        }
        return True

    monkeypatch.setattr(streaming, "_accounted_primary_stream", context_length_then_normal_stream)
    monkeypatch.setattr(streaming, "run_forced_compaction", paid_forced_compaction)

    provider = Provider()
    hook = ProbeHook()
    run_store = InMemoryRunStore()
    budget = RunBudget(max_tokens=100)
    agent = Agent(
        model="offline-model",
        provider=provider,
        session_store=InMemorySessionStore(),
        run_store=run_store,
        permissions={"mode": "skip-dangerous"},
        hooks=[hook],
    )
    session = await agent.session()
    events = [event async for event in session.run("go", RunOptions(budget=budget))]
    run_id = next(event.run_id for event in events if isinstance(event, SystemEvent))
    rows = await run_store.load_events(run_id)

    result = next(event for event in events if isinstance(event, ResultEvent))
    primary_intents = [
        row.event
        for row in rows
        if isinstance(row.event, ModelCallEvent)
        and row.event.record_kind == "intent"
        and row.event.record["kind"] == "primary"
    ]

    assert provider.calls == ["summary"]
    assert budget.spent_tokens == 133
    assert result.total_usage == Usage(input_tokens=111, output_tokens=22)
    assert result.subtype == "error"
    assert any(isinstance(event, BudgetEvent) and event.kind == "exceeded" for event in events)
    # The first artificial context failure is before a primary stream.  A
    # second context build/hook/intent proves the generator was resumed after
    # compaction instead of stopped at the runner's admission boundary.
    assert hook.context_calls == 1
    assert hook.before_calls == 1
    assert hook.provider_start_calls == 1
    assert primary_intents == []


@pytest.mark.asyncio
async def test_legacy_nonzero_checkpoint_without_receipts_is_rejected_before_dispatch() -> None:
    """Aggregate pre-accounting usage cannot be admitted or repriced on resume."""
    from linch import RunBudget
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore, RunCheckpoint
    from linch.session import RunOptions
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    await session_store.create(id="legacy-accounting")
    run = await run_store.create_run("legacy-accounting", id="legacy-accounting-run")
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="turn_complete",
            prompt="continue",
            turn_index=0,
            total_usage=Usage(input_tokens=9, output_tokens=4),
        ),
    )

    provider = _NeverCalledProvider()
    budget = RunBudget(max_tokens=100)
    agent = make_agent(
        provider,
        model="claude-sonnet-4-6",
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="legacy-accounting")

    await pin_run_contract(session, run.id, RunOptions(budget=budget))

    with pytest.raises(ConfigError, match="receipt provenance"):
        _ = [
            event
            async for event in session.resume(
                run.id,
                RunOptions(budget=budget),
            )
        ]

    assert provider.calls == 0
    assert budget.spent_tokens == 0
    assert budget.spent_usd == 0.0


async def test_rejected_forced_compaction_yields_its_paid_usage_once() -> None:
    """A rejected paid summary remains visible before its original terminal error."""
    from linch import Agent, RunBudget
    from linch.errors import ContextLengthError
    from linch.events import ModelCallEvent, ResultEvent, UsageEvent
    from linch.pricing import cost_usd
    from linch.providers import BaseProvider
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.types import Message, TextBlock, Usage

    class ProbeHook:
        resume_policy_id = "test.rejected-forced-compaction"
        resume_policy_version = "1"
        resume_policy_config: dict[str, Any] = {}

        def __init__(self) -> None:
            self.context_calls = self.before_calls = 0

        async def build_context(self, session: Any, turn_index: int) -> None:
            self.context_calls += 1

        def on_before_provider_call(self, ctx: Any) -> None:
            self.before_calls += 1

    class RejectedSummaryProvider(BaseProvider):
        id = "rejected-forced-compaction"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            summary = any(
                "Summarize the conversation so far." in block.text for block in req.system
            )
            self.calls.append("summary" if summary else "main")
            if not summary:
                raise ContextLengthError("force a paid compaction")
            yield {"type": "message_start", "model": "claude-haiku-4-5"}
            yield {"type": "text_delta", "text": ""}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage(111, 22)}

    provider, hook = RejectedSummaryProvider(), ProbeHook()
    runs, budget = InMemoryRunStore(), RunBudget()
    agent = Agent(
        model="offline-model",
        provider=provider,
        run_store=runs,
        session_store=InMemorySessionStore(),
        max_output_tokens=100,
        permissions={"mode": "skip-dangerous"},
        hooks=[hook],
        budget=budget,
        compaction_ladder=None,
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
    events = [event async for event in session.run("go")]
    run_id = next(event.run_id for event in events if event.type == "system")
    rows = await runs.load_events(run_id)
    receipts = [
        row.event.record
        for row in rows
        if isinstance(row.event, ModelCallEvent)
        and row.event.record_kind == "receipt"
        and row.event.record["kind"] == "compaction"
    ]
    usages = [
        event for event in events if isinstance(event, UsageEvent) and event.source == "compaction"
    ]
    assert len(usages) == len(receipts) == 1
    usage, receipt = usages[0], receipts[0]
    assert receipt["disposition"] == "rejected"
    assert receipt["rejection_reason"] == "empty_output"
    assert usage.call_id == receipt["delivery_attempt_id"]
    assert usage.logical_call_id == receipt["logical_call_id"]
    assert usage.model == receipt["actual_model"] == "claude-haiku-4-5"
    assert usage.usage == usage.cumulative == Usage(111, 22)
    assert usage.cost_usd == receipt["cost_usd"] == cost_usd(Usage(111, 22), "claude-haiku-4-5")
    result = next(event for event in events if isinstance(event, ResultEvent))
    assert result.subtype == "error"
    assert result.total_usage == Usage(111, 22)
    assert result.total_cost_usd == usage.cost_usd
    assert result.usage_complete is False  # the preceding primary dispatch has unknown usage
    assert budget.spent_tokens == 133
    assert budget.spent_usd == usage.cost_usd
    assert provider.calls == ["main", "summary"]
    assert hook.context_calls == hook.before_calls == 1
    assert not any(event.type == "compaction" for event in events)
    error_index = next(index for index, event in enumerate(events) if event.type == "error")
    assert events.index(usage) < error_index < events.index(result)
    assert events[error_index].error["name"] == "ProviderError"
    assert events[error_index].error["retryable"] is False
    assert "unusable summary" in events[error_index].error["message"]
    assert (
        sum(
            isinstance(row.event, UsageEvent) and row.event.call_id == usage.call_id for row in rows
        )
        == 1
    )
    saved = await runs.load_run(run_id)
    accounting = saved.checkpoint.extension_state["linch.model_calls.v1"]
    assert accounting["format"] == "journal-v1"
    assert accounting["ledger"] is None
    assert accounting["journal_count"] == sum(isinstance(row.event, ModelCallEvent) for row in rows)
