"""Compaction deliveries retain spend independently of accepted summary output."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest


class _Limiter:
    def __init__(self) -> None:
        self.sem = asyncio.Semaphore(1)
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.held = 0

    async def acquire(self, *, model: str) -> None:
        await self.sem.acquire()
        self.acquired.append(model)
        self.held += 1

    def release(self, *, model: str) -> None:
        self.released.append(model)
        self.held -= 1
        self.sem.release()


def _events(text: str = " Paid summary. ", *, usage: Any = None) -> list[dict[str, Any]]:
    from linch.types import Usage

    return [
        {"type": "message_start", "model": "claude-haiku-4-5"},
        {"type": "text_delta", "text": text},
        {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(111, 22) if usage is None else usage,
        },
    ]


class _Provider:
    id = "normalized-fixture"

    def __init__(self, events: list[Any] | None = None, *, trace: list[str] | None = None):
        self.events = _events() if events is None else events
        self.trace = [] if trace is None else trace
        self.requests: list[Any] = []
        self.closed = 0

    def context_window(self, model: str) -> int:
        return 1024

    async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
        self.trace.append("advanced")
        self.requests.append(request)
        try:
            for event in self.events:
                if isinstance(event, BaseException):
                    raise event
                yield event
        finally:
            self.closed += 1


def _setup(*, provider: Any = None, budget: Any = None, persist: Any = None):
    from linch.model_calls import _AccountedAuxiliaryCaller, _ModelCallLedger

    provider = _Provider() if provider is None else provider
    ledger = _ModelCallLedger("run-fixture", budget=budget, persist=persist)
    limiter = _Limiter()
    caller = _AccountedAuxiliaryCaller(
        provider, ledger, limiter=limiter, parent_logical_call_id="parent", turn_index=2
    )
    return provider, ledger, limiter, caller


async def _text(caller: Any, *, signal: Any = None, logical: str = "logical-operation"):
    from linch.abort import AbortContext
    from linch.types import Message, SystemBlock, TextBlock

    return await caller.text(
        logical_call_id=logical,
        model="requested-model",
        system=[SystemBlock("Exact summary prompt.")],
        messages=[Message("user", [TextBlock("Earlier conversation.")])],
        max_output_tokens=4096,
        signal=AbortContext() if signal is None else signal,
    )


async def test_gateway_settles_before_return_and_gates_only_one_delivery() -> None:
    from linch.types import Usage

    trace: list[str] = []

    async def persist(event: Any) -> None:
        trace.append(event.record_kind)

    provider, ledger, limiter, caller = _setup(provider=_Provider(trace=trace), persist=persist)
    result = await asyncio.wait_for(_text(caller), timeout=1)
    assert result.text == "Paid summary."
    assert result.receipt.accepted_text == result.text
    assert result.receipt.accepted_text_sha256 == hashlib.sha256(b"Paid summary.").hexdigest()
    assert result.receipt.actual_model == "claude-haiku-4-5"
    assert result.receipt.cost_usd == pytest.approx(0.000221)
    assert result.receipt.usage == Usage(111, 22)
    assert ledger.total_usage == Usage(111, 22)
    assert trace == ["intent", "dispatch_started", "advanced", "receipt"]
    assert limiter.acquired == limiter.released == ["requested-model"]
    assert provider.closed == 1
    assert provider.requests[0].tools == []
    assert provider.requests[0].system[0].text == "Exact summary prompt."
    assert provider.requests[0].max_output_tokens == 4096
    receipt = ledger.receipts()[0]
    assert receipt.turn_index == 2
    assert UUID(receipt.delivery_attempt_id).version == 4


@pytest.mark.parametrize("output", ["", "  ", "tool", "thinking", "oversized", "native-json"])
async def test_valid_final_usage_is_charged_when_output_is_rejected(output: str) -> None:
    from linch.types import Usage

    events = _events(output)
    if output in ("tool", "native-json"):
        events[1:2] = [
            {"type": "tool_use_start", "id": "tool-1", "name": "read"},
            {
                "type": "tool_use_input_delta",
                "id": "tool-1",
                "json_delta": "[invalid" if output == "native-json" else "{}",
            },
            {"type": "tool_use_end", "id": "tool-1"},
        ]
        events[-1]["stop_reason"] = "tool_use"
    elif output == "thinking":
        events.insert(1, {"type": "thinking_delta", "text": "private reasoning"})
    elif output == "oversized":
        events[1:2] = [
            {"type": "text_delta", "text": "é" * 262144},
            {"type": "text_delta", "text": "é" * 262145},
            {"type": "text_delta", "text": "ignored remainder"},
        ]
    provider, ledger, limiter, caller = _setup(provider=_Provider(events))
    result = await _text(caller)
    assert result.text is None
    assert result.receipt.disposition == "rejected"
    assert result.receipt.usage_status == "reported"
    assert result.receipt.usage == Usage(111, 22)
    assert result.receipt.accepted_text is None
    assert result.receipt.accepted_text_sha256 is None
    assert result.receipt.rejection_reason
    assert ledger.total_usage == Usage(111, 22)
    assert ledger.usage_complete
    assert provider.closed == 1
    assert limiter.held == 0


@pytest.mark.parametrize(
    "fault",
    [
        "missing-model",
        "blank-model",
        "invalid-model",
        "no-start",
        "duplicate-start",
        "no-end",
        "missing-usage",
        "invalid-usage",
        "negative-usage",
        "boolean-usage",
        "delta-before-tool",
    ],
)
async def test_invalid_normalized_stream_never_fabricates_usage(fault: str):
    from linch.errors import ProviderError
    from linch.types import Usage

    events = _events()
    mutations = {
        "missing-model": lambda: events[0].pop("model"),
        "blank-model": lambda: events[0].update(model="   "),
        "invalid-model": lambda: events[0].update(model=42),
        "no-start": lambda: events.pop(0),
        "duplicate-start": lambda: events.insert(1, events[0].copy()),
        "no-end": lambda: events.pop(),
        "missing-usage": lambda: events[-1].pop("usage"),
        "invalid-usage": lambda: events[-1].update(
            usage={"input_tokens": 111, "output_tokens": 22}
        ),
        "negative-usage": lambda: events[-1].update(usage=Usage(-1, 22)),
        "boolean-usage": lambda: events[-1].update(usage=Usage(True, 22)),
        "delta-before-tool": lambda: events.insert(
            1, {"type": "tool_use_input_delta", "id": "missing", "json_delta": "{}"}
        ),
    }
    mutations[fault]()
    _, ledger, limiter, caller = _setup(provider=_Provider(events))
    with pytest.raises(ProviderError, match="normalized provider stream"):
        await _text(caller)
    assert ledger.total_usage == Usage()
    assert not ledger.usage_complete
    assert ledger.receipts()[0].usage_status == "unknown"
    assert limiter.held == 0


async def test_reported_usage_retains_all_buckets_and_is_copied() -> None:
    from linch.types import Usage

    usage = Usage(111, 22, 3, 4)
    _, ledger, _, caller = _setup(provider=_Provider(_events(usage=usage)))
    result = await _text(caller)
    usage.input_tokens = 999
    assert result.receipt.usage == Usage(111, 22, 3, 4)
    assert ledger.total_usage == Usage(111, 22, 3, 4)
    assert result.receipt.cost_usd == pytest.approx(0.0002263)


@pytest.mark.parametrize("mode", ["abort", "budget"])
async def test_model_call_admission_rejections(mode: str) -> None:
    from linch.abort import AbortContext
    from linch.budget import RunBudget
    from linch.errors import AbortError
    from linch.model_calls import _ModelCallError

    journal: list[Any] = []

    async def persist(event: Any) -> None:
        journal.append(event)

    budget = RunBudget(max_tokens=0 if mode == "budget" else 1000)
    signal = AbortContext()
    if mode == "abort":
        signal.abort()
    provider, ledger, limiter, caller = _setup(budget=budget, persist=persist)
    expected = AbortError if mode == "abort" else _ModelCallError
    with pytest.raises(expected) as raised:
        await _text(caller, signal=signal)
    assert provider.requests == []
    assert ledger.usage_complete
    assert budget.spent_tokens == 0
    assert ledger.receipts() == []
    assert limiter.held == 0
    if mode == "abort":
        assert journal[-1].record_kind == "abandoned"
        assert journal[-1].record["usage_status"] == "not_dispatched"
    else:
        assert raised.value.code == "budget_exceeded"
        assert limiter.acquired == limiter.released == ["requested-model"]


@pytest.mark.parametrize("cancel_task", [False, True])
async def test_abort_during_hanging_stream_records_unknown_and_releases_slot(cancel_task: bool):
    from linch.abort import AbortContext
    from linch.budget import RunBudget
    from linch.errors import AbortError
    from linch.model_calls import _ModelCallError, _ModelCallLedger

    ready = asyncio.Event()

    class Hanging(_Provider):
        async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
            try:
                yield {"type": "message_start", "model": "actual-model"}
                ready.set()
                await asyncio.Event().wait()
            finally:
                self.closed += 1

    budget = RunBudget(max_tokens=1000)
    provider, ledger, limiter, caller = _setup(provider=Hanging(), budget=budget)
    signal = AbortContext()
    task = asyncio.create_task(_text(caller, signal=signal))
    await asyncio.wait_for(ready.wait(), timeout=1)
    if cancel_task:
        task.cancel()
    else:
        signal.abort()
    with pytest.raises(asyncio.CancelledError if cancel_task else AbortError):
        await asyncio.wait_for(task, timeout=1)
    assert not ledger.usage_complete
    assert ledger.receipts()[0].disposition == "cancelled"
    assert limiter.held == 0
    assert provider.closed == 1
    with pytest.raises(_ModelCallError, match="unknown use"):
        _ModelCallLedger("sibling", budget=budget).assert_may_start()


@pytest.mark.parametrize("failing_kind", ["intent", "dispatch_started", "receipt"])
async def test_persistence_failure_stops_and_retains_observed_spend(failing_kind: str):
    from linch.model_calls import _ModelCallError
    from linch.types import Usage

    async def persist(event: Any) -> None:
        if event.record_kind == failing_kind:
            raise OSError("journal unavailable")

    provider, ledger, limiter, caller = _setup(persist=persist)
    with pytest.raises(_ModelCallError) as exc:
        await _text(caller)
    assert exc.value.code == "persistence_failed"
    assert len(provider.requests) == (1 if failing_kind == "receipt" else 0)
    assert ledger.total_usage == (Usage(111, 22) if failing_kind == "receipt" else Usage())
    assert ledger.receipts() == []
    assert limiter.held == 0
    with pytest.raises(_ModelCallError) as continuation:
        await _text(caller)
    assert continuation.value.code == "persistence_failed"


async def test_retry_deliveries_are_fresh_with_stable_operation_and_fingerprint() -> None:
    _, ledger, _, caller = _setup()
    first = await _text(caller)
    second = await _text(caller)
    assert first.receipt.delivery_attempt_id != second.receipt.delivery_attempt_id
    assert first.receipt.logical_call_id == second.receipt.logical_call_id == "logical-operation"
    assert first.receipt.request_fingerprint == second.receipt.request_fingerprint
    assert ledger.total_usage.input_tokens == 222
    assert ledger.total_usage.output_tokens == 44


def _compaction_fixture(*, strategy: Any, provider: Any, limiter: Any):
    from types import SimpleNamespace

    from linch import Context
    from linch.session_log import SessionLog
    from linch.types import Message, TextBlock

    messages = [
        Message("assistant" if i % 2 else "user", [TextBlock("earlier " * 100)]) for i in range(30)
    ]
    agent = SimpleNamespace(
        model="requested-model",
        provider=provider,
        limiter=limiter,
        compaction=strategy,
        max_output_tokens=None,
        token_estimator=None,
        hooks=None,
        context=Context(),
    )

    class Session:
        active_run_id = "run-fixture"
        id = "session-fixture"
        active_model = None
        last_compaction_info = None

        def __init__(self) -> None:
            self.session_log = SessionLog.seed(historical=messages)

        @property
        def provider_view(self):
            return self.session_log.provider_view

    session = Session()
    session.agent = agent
    return session, agent


@pytest.mark.parametrize("detailed", [False, True])
async def test_builtins_use_injected_gateway_without_nested_slot(detailed: bool):
    from linch.abort import AbortContext
    from linch.compaction import DefaultCompaction, DetailedCompaction, run_forced_compaction

    provider, ledger, limiter, caller = _setup()
    strategy = DetailedCompaction() if detailed else DefaultCompaction()
    session, agent = _compaction_fixture(strategy=strategy, provider=provider, limiter=limiter)
    original_history = session.session_log.full_history
    await asyncio.wait_for(
        run_forced_compaction(
            session, agent, AbortContext(), auxiliary_caller=caller, logical_call_id="compaction-op"
        ),
        timeout=1,
    )
    assert len(session.provider_view) < 30
    assert session.session_log.full_history == original_history
    assert len(provider.requests) == 1
    assert limiter.acquired == limiter.released == ["requested-model"]
    receipt = ledger.receipts()[0]
    assert receipt.strategy_id == strategy.id
    assert receipt.logical_call_id == "compaction-op"
    assert receipt.source_context_fingerprint
    assert provider.requests[0].system[0].text == strategy.prompt
    assert provider.requests[0].max_output_tokens == (8192 if detailed else 4096)


@pytest.mark.parametrize("early_break", [False, True])
async def test_custom_provider_settles_before_end_or_closes_early_stream(early_break: bool):
    from linch.abort import AbortContext
    from linch.compaction import run_forced_compaction
    from linch.types import ProviderRequest

    provider, ledger, limiter, caller = _setup()

    class Custom:
        id = "custom-fixture"

        async def compact(self, ctx: Any, supplied: Any) -> list[Any]:
            assert supplied.id == "normalized-fixture"
            assert supplied.context_window(ctx.model) == 1024
            request = ProviderRequest(ctx.model, [], [], ctx.messages, signal=ctx.signal)
            async for event in supplied.stream(request):
                if early_break and event["type"] == "text_delta":
                    break
                if event["type"] == "message_end":
                    assert ledger.total_usage.input_tokens == 111
                    assert ledger.receipts()[0].usage_status == "reported"
                    break
            return ctx.messages

    session, agent = _compaction_fixture(strategy=Custom(), provider=provider, limiter=limiter)
    await run_forced_compaction(
        session, agent, AbortContext(), auxiliary_caller=caller, logical_call_id="custom-operation"
    )
    assert limiter.acquired == limiter.released == ["requested-model"]
    assert provider.closed == 1
    receipt = ledger.receipts()[0]
    assert receipt.usage_status == ("unknown" if early_break else "reported")
    assert receipt.strategy_id == "custom-fixture"


async def test_opaque_custom_work_is_outside_supplied_provider_guarantee() -> None:
    from linch.abort import AbortContext
    from linch.compaction import run_forced_compaction

    provider, ledger, limiter, caller = _setup()

    class Opaque:
        id = "opaque-custom"

        async def compact(self, ctx: Any, supplied: Any) -> list[Any]:
            return ctx.messages

    session, agent = _compaction_fixture(strategy=Opaque(), provider=provider, limiter=limiter)
    await run_forced_compaction(session, agent, AbortContext(), auxiliary_caller=caller)
    assert ledger.receipts() == []
    assert provider.requests == []
    assert limiter.acquired == []


async def test_three_positional_context_and_standalone_strategy_remain_usable() -> None:
    from linch.abort import AbortContext
    from linch.compaction import CompactionContext, DefaultCompaction

    provider = _Provider()
    session, _ = _compaction_fixture(strategy=DefaultCompaction(), provider=provider, limiter=None)
    ctx = CompactionContext(session.provider_view, "requested-model", AbortContext())
    result = await DefaultCompaction().compact(ctx, provider)
    assert len(result) < len(ctx.messages)
    assert len(provider.requests) == 1


@pytest.mark.parametrize("cancel_task", [False, True])
async def test_abort_while_waiting_for_slot_abandons_without_dispatch_or_fence(cancel_task: bool):
    from linch.abort import AbortContext
    from linch.budget import RunBudget
    from linch.errors import AbortError
    from linch.model_calls import _ModelCallLedger

    budget = RunBudget(max_tokens=1000)
    provider, ledger, limiter, caller = _setup(budget=budget)
    await limiter.acquire(model="held-by-sibling")
    signal = AbortContext()
    task = asyncio.create_task(_text(caller, signal=signal))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()
    if cancel_task:
        task.cancel()
    else:
        signal.abort()
    with pytest.raises(asyncio.CancelledError if cancel_task else AbortError):
        await asyncio.wait_for(task, timeout=1)
    assert provider.requests == []
    assert ledger.usage_complete
    assert ledger.receipts() == []
    assert limiter.held == 1
    limiter.release(model="held-by-sibling")
    await asyncio.sleep(0)
    assert limiter.held == 0
    assert limiter.acquired == limiter.released == ["held-by-sibling"]
    _ModelCallLedger("sibling", budget=budget).assert_may_start()


@pytest.mark.parametrize("failure", ["transport", "invalid-end"])
async def test_provider_failure_preserves_usage(failure: str) -> None:
    from linch.errors import ProviderError
    from linch.types import Usage

    if failure == "transport":
        provider = _Provider(
            [
                {"type": "message_start", "model": "actual-model"},
                ProviderError("transport disconnected", retryable=True),
            ]
        )
        provider, ledger, limiter, caller = _setup(provider=provider)
        expected = "transport disconnected"
    else:
        events = _events()
        events[-1]["stop_reason"] = "invalid-stop"
        provider, ledger, limiter, caller = _setup(provider=_Provider(events))
        expected = "normalized provider stream"
    with pytest.raises(ProviderError, match=expected):
        await _text(caller)
    if failure == "transport":
        assert not ledger.usage_complete
        assert ledger.receipts()[0].disposition == "failed"
    else:
        assert ledger.total_usage == Usage(111, 22)
        assert ledger.usage_complete
        assert ledger.receipts()[0].disposition == "rejected"
    assert provider.closed == 1
    assert limiter.held == 0


async def test_facade_uses_run_signal_and_durable_known_usage_survives_consumer_close() -> None:
    from linch.abort import AbortContext
    from linch.compaction import _AccountedCompactionProvider
    from linch.types import ProviderRequest, Usage

    provider, ledger, limiter, caller = _setup()
    signal = AbortContext()
    facade = _AccountedCompactionProvider(caller, "operation", signal)
    request = ProviderRequest("requested-model", [], [], [], signal=AbortContext())
    stream = facade.stream(request)
    async for event in stream:
        if event["type"] == "message_end":
            break
    await facade.aclose()
    signal.abort()
    await stream.aclose()
    assert provider.requests[0].signal is signal
    assert ledger.total_usage == Usage(111, 22)
    assert ledger.usage_complete
    assert len(ledger.receipts()) == 1
    assert limiter.held == 0


async def test_facade_closes_all_streams_even_when_one_provider_close_fails() -> None:
    from linch.abort import AbortContext
    from linch.compaction import _AccountedCompactionProvider
    from linch.model_calls import _AccountedAuxiliaryCaller, _ModelCallLedger
    from linch.types import ProviderRequest

    class CloseFails(_Provider):
        async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
            try:
                yield {"type": "message_start", "model": "actual-model"}
                yield {"type": "text_delta", "text": "partial"}
            finally:
                self.closed += 1
                raise OSError("close failed")

    provider = CloseFails()
    ledger = _ModelCallLedger("run-fixture")
    caller = _AccountedAuxiliaryCaller(provider, ledger)
    facade = _AccountedCompactionProvider(caller, "operation", AbortContext())
    for _ in range(2):
        stream = facade.stream(ProviderRequest("requested-model", [], [], []))
        await anext(stream)
    with pytest.raises(OSError, match="close failed"):
        await facade.aclose()
    assert provider.closed == 2
    assert len(ledger.receipts()) == 2
    assert not ledger.usage_complete


@pytest.mark.parametrize("tail", ["late-event", "late-failure"])
async def test_valid_end_followed_by_invalid_tail_is_charged_once_and_rejected(tail: str):
    from linch.errors import ProviderError
    from linch.types import Usage

    events = _events()
    events.append(
        {"type": "text_delta", "text": "after end"}
        if tail == "late-event"
        else ProviderError("late transport failure", retryable=False)
    )
    provider, ledger, limiter, caller = _setup(provider=_Provider(events))
    with pytest.raises(ProviderError):
        await _text(caller)
    assert ledger.total_usage == Usage(111, 22)
    assert ledger.usage_complete
    receipts = ledger.receipts()
    assert len(receipts) == 1
    assert receipts[0].usage_status == "reported"
    assert receipts[0].disposition == "rejected"
    assert receipts[0].accepted_text is None
    assert provider.closed == 1
    assert limiter.held == 0


@pytest.mark.parametrize("mode", ["tail", "completion"])
async def test_abort_after_end_rejects_paid_output(mode: str) -> None:
    from linch.abort import AbortContext
    from linch.budget import RunBudget
    from linch.errors import AbortError
    from linch.types import Usage

    if mode == "tail":
        tail_waiting = asyncio.Event()

        class TailHangs(_Provider):
            async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
                try:
                    for event in _events():
                        yield event
                    tail_waiting.set()
                    await asyncio.Event().wait()
                finally:
                    self.closed += 1

        provider, ledger, limiter, caller = _setup(provider=TailHangs())
        signal = AbortContext()
        task = asyncio.create_task(_text(caller, signal=signal))
        await asyncio.wait_for(tail_waiting.wait(), timeout=1)
        signal.abort()
        with pytest.raises(AbortError):
            await asyncio.wait_for(task, timeout=1)
    else:

        class AbortOnCompletion(_Provider):
            async def stream(self, request: Any) -> AsyncIterator[dict[str, Any]]:
                try:
                    for event in _events():
                        yield event
                    request.signal.abort()
                finally:
                    self.closed += 1

        budget = RunBudget(max_tokens=1000)
        provider, ledger, limiter, caller = _setup(provider=AbortOnCompletion(), budget=budget)
        with pytest.raises(AbortError):
            await _text(caller)

    receipts = ledger.receipts()
    assert len(receipts) == 1
    assert receipts[0].usage_status == "reported"
    assert receipts[0].disposition == "rejected"
    assert receipts[0].accepted_text is None
    assert receipts[0].accepted_text_sha256 is None
    assert ledger.total_usage == Usage(111, 22)
    assert ledger.usage_complete
    if mode == "completion":
        assert budget.spent_tokens == 133
    assert provider.closed == 1
    assert limiter.acquired == limiter.released == ["requested-model"]
    assert limiter.held == 0
