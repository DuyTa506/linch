from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, cast

import pytest

from linch import Agent
from linch.context import ContextBuildResult
from linch.durability import DurabilityOptions, InboxDelivery
from linch.errors import ProviderError
from linch.hooks import HookResult
from linch.run_store import InMemoryRunStore, decode_model_input_snapshot
from linch.sessions import InMemorySessionStore
from linch.types import Message, SystemBlock, TextBlock, Usage


class _DynamicRequestHook:
    resume_policy_id = "test.exact-model-input"
    resume_policy_version = "1"
    resume_policy_config = {"kind": "dynamic-request"}

    def __init__(self, value: str) -> None:
        self.value = value
        self.context_calls = 0
        self.before_calls = 0

    async def build_context(self, session: Any, turn_index: int) -> ContextBuildResult:
        self.context_calls += 1
        return ContextBuildResult(
            system_blocks=[SystemBlock(text=f"context:{self.value}")],
            messages=[Message(role="user", content=[TextBlock(text=f"rag:{self.value}")])],
        )

    def on_before_provider_call(self, ctx: Any) -> HookResult:
        self.before_calls += 1
        assert ctx.request is not None
        ctx.request.system.append(SystemBlock(text=f"before:{self.value}"))
        return HookResult.mutate(request=ctx.request)


class _BlockingProvider:
    id = "blocking"

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        self.entered.set()
        await asyncio.Future()
        yield {}  # pragma: no cover


class _CaptureProvider:
    id = "capture"

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.requests.append(req)
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


async def _leave_provider_pending(
    session_store: InMemorySessionStore,
    run_store: InMemoryRunStore,
    *,
    durability: DurabilityOptions | None = None,
) -> tuple[str, _DynamicRequestHook]:
    hook = _DynamicRequestHook("frozen")
    provider = _BlockingProvider()
    agent = Agent(
        model="model-a",
        provider=cast(Any, provider),
        session_store=session_store,
        run_store=run_store,
        durability=durability or DurabilityOptions(exact_model_input=True),
        hooks=[hook],
    )
    session = await agent.session(id="session-1")
    events: list[Any] = []

    async def consume() -> None:
        async for event in session.run("hello"):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(provider.entered.wait(), timeout=2)
    run_id = next(event.run_id for event in events if event.type == "system")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = await run_store.load_run(run_id)
    assert record is not None and record.checkpoint is not None
    assert record.checkpoint.phase == "provider_pending"
    assert record.checkpoint.provider_attempt == 1
    assert record.checkpoint.model_input_snapshot_id is not None
    return run_id, hook


async def test_resume_replays_frozen_request_without_rerunning_dynamic_hooks() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    run_id, original_hook = await _leave_provider_pending(session_store, run_store)
    record = await run_store.load_run(run_id)
    assert record is not None and record.checkpoint is not None
    snapshot_id = record.checkpoint.model_input_snapshot_id
    assert snapshot_id is not None
    snapshot = await run_store.load(snapshot_id)
    assert snapshot is not None
    frozen = decode_model_input_snapshot(snapshot)

    changed_hook = _DynamicRequestHook("changed-after-crash")
    provider = _CaptureProvider()
    restarted = Agent(
        model="model-a",
        provider=cast(Any, provider),
        session_store=session_store,
        run_store=run_store,
        durability=DurabilityOptions(exact_model_input=True),
        hooks=[changed_hook],
    )
    session = await restarted.session(id="session-1")
    events = [event async for event in session.resume(run_id)]

    assert events[-1].type == "result"
    assert len(provider.requests) == 1
    actual = provider.requests[0]
    actual.signal = None
    assert actual == frozen
    assert original_hook.context_calls == original_hook.before_calls == 1
    assert changed_hook.context_calls == changed_hook.before_calls == 0
    completed = await run_store.load_run(run_id)
    assert completed is not None and completed.checkpoint is not None
    assert completed.checkpoint.model_input_snapshot_id is None
    assert await run_store.load(snapshot_id) is None


async def test_frozen_resume_defers_new_durable_inbox_delivery_to_next_turn() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    strict = DurabilityOptions(durable_inbox=True, exact_model_input=True)
    run_id, _ = await _leave_provider_pending(session_store, run_store, durability=strict)
    notification = Message(role="user", content=[TextBlock(text="arrived after crash")])
    await session_store.enqueue_inbox(
        "session-1", InboxDelivery("late", notification, source="host")
    )

    provider = _CaptureProvider()
    restarted = Agent(
        model="model-a",
        provider=cast(Any, provider),
        session_store=session_store,
        run_store=run_store,
        durability=strict,
        hooks=[_DynamicRequestHook("frozen")],
    )
    session = await restarted.session(id="session-1")
    resumed = [event async for event in session.resume(run_id)]

    assert not any(event.type == "user" and event.subtype == "notification" for event in resumed)
    assert all(
        "arrived after crash"
        not in [
            getattr(block, "text", "") for message in request.messages for block in message.content
        ]
        for request in provider.requests
    )

    next_turn = [event async for event in session.run("continue")]
    assert any(
        event.type == "user" and event.subtype == "notification" and event.message == notification
        for event in next_turn
    )


async def test_missing_pending_snapshot_fails_closed_before_provider_call() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    run_id, _ = await _leave_provider_pending(session_store, run_store)
    record = await run_store.load_run(run_id)
    assert record is not None and record.checkpoint is not None
    snapshot_id = record.checkpoint.model_input_snapshot_id
    assert snapshot_id is not None
    await run_store.delete(snapshot_id)

    provider = _CaptureProvider()
    restarted = Agent(
        model="model-a",
        provider=cast(Any, provider),
        session_store=session_store,
        run_store=run_store,
        durability=DurabilityOptions(exact_model_input=True),
        hooks=[_DynamicRequestHook("changed")],
    )
    session = await restarted.session(id="session-1")
    events = [event async for event in session.resume(run_id)]

    error = next(event for event in events if event.type == "error")
    assert error.error["name"] == "ConfigError"
    assert "is missing" in error.error["message"]
    assert provider.requests == []


async def test_corrupt_pending_snapshot_fails_closed_before_provider_call() -> None:
    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    run_id, _ = await _leave_provider_pending(session_store, run_store)
    record = await run_store.load_run(run_id)
    assert record is not None and record.checkpoint is not None
    snapshot_id = record.checkpoint.model_input_snapshot_id
    assert snapshot_id is not None
    snapshot = await run_store.load(snapshot_id)
    assert snapshot is not None
    run_store._model_input_snapshots[snapshot_id] = replace(snapshot, request_json="{}")

    provider = _CaptureProvider()
    restarted = Agent(
        model="model-a",
        provider=cast(Any, provider),
        session_store=session_store,
        run_store=run_store,
        durability=DurabilityOptions(exact_model_input=True),
        hooks=[_DynamicRequestHook("changed")],
    )
    session = await restarted.session(id="session-1")
    events = [event async for event in session.resume(run_id)]

    error = next(event for event in events if event.type == "error")
    assert error.error["name"] == "ConfigError"
    assert "integrity" in error.error["message"]
    assert provider.requests == []


async def test_exact_mode_requires_snapshot_store_capabilities() -> None:
    class _LegacyRunStore:
        pass

    provider = _CaptureProvider()
    agent = Agent(
        model="model-a",
        provider=cast(Any, provider),
        run_store=_LegacyRunStore(),  # type: ignore[arg-type]
        durability=DurabilityOptions(exact_model_input=True),
    )
    session = await agent.session()

    with pytest.raises(Exception, match="snapshot capabilities"):
        async for _ in session.run("hello"):
            pass
    assert provider.requests == []


async def test_snapshot_write_failure_makes_zero_provider_calls() -> None:
    class _FailingStore(InMemoryRunStore):
        async def save(self, run_id: str, provider_attempt: int, request: Any) -> Any:
            raise OSError("snapshot disk unavailable")

    store = _FailingStore()
    provider = _CaptureProvider()
    agent = Agent(
        model="model-a",
        provider=cast(Any, provider),
        run_store=store,
        durability=DurabilityOptions(exact_model_input=True),
    )
    session = await agent.session()
    events = [event async for event in session.run("hello")]

    error = next(event for event in events if event.type == "error")
    assert error.error["name"] == "ConfigError"
    assert "failed to save exact model input" in error.error["message"]
    assert provider.requests == []


async def test_fallback_freezes_a_new_incremented_provider_attempt() -> None:
    class _RecordingStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.attempts: list[tuple[int, str]] = []

        async def save(self, run_id: str, provider_attempt: int, request: Any) -> Any:
            self.attempts.append((provider_attempt, request.model))
            return await super().save(run_id, provider_attempt, request)

    class _FallbackProvider(_CaptureProvider):
        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            self.requests.append(req)
            if len(self.requests) == 1:
                raise ProviderError("overloaded", retryable=True)
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    store = _RecordingStore()
    provider = _FallbackProvider()
    agent = Agent(
        model="model-a",
        fallback_models=["model-b"],
        provider=cast(Any, provider),
        run_store=store,
        durability=DurabilityOptions(exact_model_input=True),
    )
    session = await agent.session()
    events = [event async for event in session.run("hello")]

    assert events[-1].type == "result"
    assert store.attempts == [(1, "model-a"), (2, "model-b")]
    run_id = next(event.run_id for event in events if event.type == "system")
    record = await store.load_run(run_id)
    assert record is not None and record.checkpoint is not None
    assert record.checkpoint.provider_attempt == 2
