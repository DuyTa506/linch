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
@pytest.mark.parametrize(
    "raw_input",
    ['{"secret": "native-leak"', '["native-leak"]', "", "   "],
)
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
async def test_explicit_empty_native_object_remains_a_valid_tool_input() -> None:
    """Only an explicit ``{}`` is accepted; an omitted delta is not guessed."""
    from linch import Agent
    from linch.events import ResultEvent
    from linch.providers import BaseProvider
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry, ToolResult
    from linch.types import Usage

    calls: list[dict[str, object]] = []

    class EmptyInputTool:
        name = "EmptyInputTool"
        description = "Accept an empty JSON object."
        input_schema = {"type": "object"}
        scope: Any = "read"
        parallel = False

        def validate(self, raw: dict[str, object]) -> dict[str, object]:
            return raw

        def summarize(self, input: dict[str, object]) -> str:
            return "empty"

        async def execute(self, input: dict[str, object], ctx: Any) -> ToolResult:
            calls.append(input)
            return ToolResult(content="accepted")

    class Provider(BaseProvider):
        id = "explicit-empty-native"

        def __init__(self) -> None:
            self.calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            self.calls += 1
            yield {"type": "message_start", "model": req.model}
            if self.calls == 1:
                yield {"type": "tool_use_start", "id": "empty-1", "name": "EmptyInputTool"}
                yield {"type": "tool_use_input_delta", "id": "empty-1", "json_delta": "{}"}
                yield {"type": "tool_use_end", "id": "empty-1"}
                yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
                return
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    tools = ToolRegistry()
    tools.register(EmptyInputTool())
    provider = Provider()
    agent = Agent(
        model="offline-model",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        tools=tools,
    )
    session = await agent.session()
    events = [event async for event in session.run("go")]

    assert calls == [{}]
    assert provider.calls == 2
    assert next(event for event in events if isinstance(event, ResultEvent)).subtype == "success"


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

    original_stream_turn = streaming.stream_turn
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

    monkeypatch.setattr(streaming, "stream_turn", context_length_then_normal_stream)
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
    from linch import Agent, RunBudget
    from linch.errors import ConfigError
    from linch.providers import BaseProvider
    from linch.run_store import InMemoryRunStore, RunCheckpoint
    from linch.session import RunOptions
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    class Provider(BaseProvider):
        id = "legacy-resume-fence"

        def __init__(self) -> None:
            self.calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            self.calls += 1
            raise AssertionError("legacy aggregate checkpoint must not dispatch a provider call")
            yield {}  # pragma: no cover

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

    provider = Provider()
    budget = RunBudget(max_tokens=100)
    agent = Agent(
        model="claude-sonnet-4-6",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session(id="legacy-accounting")

    with pytest.raises(ConfigError, match="receipt provenance"):
        _ = [
            event
            async for event in session.resume(
                run.id,
                RunOptions(allow_legacy_resume=True, budget=budget),
            )
        ]

    assert provider.calls == 0
    assert budget.spent_tokens == 0
    assert budget.spent_usd == 0.0


@pytest.mark.asyncio
async def test_resume_rejects_an_unproven_legacy_offset_alongside_new_receipts() -> None:
    """A later ledger cannot legitimize a prior aggregate-only usage offset."""
    from linch import Agent
    from linch.errors import ConfigError
    from linch.loop.checkpoint import _model_call_runtime_to_extension
    from linch.model_calls import ModelCallIntent, ModelCallReceipt, _ModelCallLedger
    from linch.providers import BaseProvider
    from linch.run_store import InMemoryRunStore, RunCheckpoint
    from linch.session import RunOptions
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    class Provider(BaseProvider):
        id = "legacy-offset-fence"

        def __init__(self) -> None:
            self.calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
            self.calls += 1
            raise AssertionError("unproven aggregate offset must not dispatch a provider call")
            yield {}  # pragma: no cover

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    await session_store.create(id="legacy-offset")
    run = await run_store.create_run("legacy-offset", id="legacy-offset-run")

    intent = ModelCallIntent(
        logical_call_id="receipt-backed-logical",
        delivery_attempt_id="receipt-backed-delivery",
        parent_logical_call_id=None,
        kind="primary",
        run_id=run.id,
        turn_index=0,
        requested_model="offline-model",
        request_fingerprint="receipt-backed-request",
        strategy_id=None,
    )
    receipt = ModelCallReceipt(
        logical_call_id=intent.logical_call_id,
        delivery_attempt_id=intent.delivery_attempt_id,
        kind=intent.kind,
        run_id=intent.run_id,
        turn_index=intent.turn_index,
        actual_model="offline-model",
        usage_status="reported",
        usage=Usage(input_tokens=1),
        cost_usd=None,
        disposition="rejected",
        rejection_reason="fixture",
        accepted_text=None,
        accepted_text_sha256=None,
        duration_ms=0,
        request_fingerprint=intent.request_fingerprint,
        strategy_id=None,
        source_context_fingerprint=None,
    )
    ledger = _ModelCallLedger(run.id)
    await ledger.begin(intent)
    await ledger.mark_dispatch_started(intent)
    await ledger.settle(receipt)
    extension_state: dict[str, Any] = {}
    _model_call_runtime_to_extension(
        extension_state,
        ledger_snapshot=ledger.snapshot(),
        active_primary=None,
    )
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="turn_complete",
            prompt="continue",
            turn_index=0,
            # This differs from the receipt-backed one-token ledger total,
            # representing the former aggregate migration offset.
            total_usage=Usage(input_tokens=10),
            extension_state=extension_state,
        ),
    )

    provider = Provider()
    agent = Agent(
        model="offline-model",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session(id="legacy-offset")

    with pytest.raises(ConfigError, match="receipt provenance"):
        _ = [event async for event in session.resume(run.id, RunOptions(allow_legacy_resume=True))]

    assert provider.calls == 0


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
