"""A real lease takeover stops every later loop-owned store write."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

_RUN_WRITES = {
    "create_run",
    "save_checkpoint",
    "save_checkpoint_with_events",
    "append_event",
    "append_events",
    "mark_completed",
    "mark_failed",
    "save",
    "delete",
    "prune",
}
_SESSION_WRITES = {
    "create",
    "create_if_absent",
    "append_messages",
    "save_provider_snapshot",
    "update_meta",
    "set_invoked_skills",
    "enqueue_inbox",
    "commit_inbox",
    "delete",
    "create_task",
    "update_task",
    "delete_task",
    "claim_task",
    "release_task",
}


@pytest.fixture(params=["memory", "postgres"])
async def lease_stores(request: pytest.FixtureRequest, pg_dsn: str) -> AsyncIterator[Any]:
    from linch.run_store import InMemoryRunStore
    from linch.run_stores.postgres import PostgresRunStore

    if request.param == "memory":
        first = InMemoryRunStore()
        second = copy.copy(first)
        for name in ("_held_leases", "_lease_ttls", "_lease_owners"):
            setattr(second, name, dict(getattr(first, name)))
    else:
        first = PostgresRunStore(pg_dsn)
        second = PostgresRunStore(pg_dsn)
        await first.load_run("fixture-probe")
        await second.load_run("fixture-probe")
    try:
        yield first, second
    finally:
        await first.close()
        await second.close()


class _Takeover:
    def __init__(self, first: Any, second: Any, target: str) -> None:
        self.first = first
        self.second = second
        self.target = target
        self.run_id = ""
        self.lease: Any = None
        self.lost = False
        self.takeover_done = False
        self.after_loss: list[str] = []
        self.first_loss: str | None = None
        self.primary_failed = False
        self.winner_record: Any = None
        self.winner_events: Any = None
        self.winner_snapshot: Any = None

    async def take_over(self) -> None:
        from linch.events import ErrorEvent
        from linch.run_store import RunCheckpoint
        from linch.types import ProviderRequest, Usage

        await asyncio.sleep(max(0.0, self.lease.expires_at - time.time()) + 0.01)
        await self.second.claim(self.run_id, "winner-b", 60.0)
        checkpoint = RunCheckpoint(
            phase="completed", prompt="winner-b", turn_index=99, total_usage=Usage()
        )
        await self.second.append_event(
            self.run_id,
            ErrorEvent(error={"name": "WinnerMarker", "message": "B", "retryable": False}),
        )
        self.winner_snapshot = await self.second.save(
            self.run_id, 99, ProviderRequest(model="winner", system=[], tools=[], messages=[])
        )
        await self.second.mark_completed(self.run_id, checkpoint)
        self.winner_record = await self.second.load_run(self.run_id)
        self.winner_events = await self.second.load_events(self.run_id)
        self.takeover_done = True

    def wrap(self, inner: Any, *, session: bool = False) -> Any:
        state = self
        writes = _SESSION_WRITES if session else _RUN_WRITES

        class Store:
            def __getattr__(self, name: str) -> Any:
                method = getattr(inner, name)
                if name not in writes:
                    return method

                async def write(*args: Any, **kwargs: Any) -> Any:
                    from linch.run_store import LeaseLostError

                    label = f"{'session' if session else 'run'}.{name}"
                    if state.lost:
                        state.after_loss.append(label)
                    if not session and name == "create_run":
                        record = await method(*args, **kwargs)
                        state.run_id = record.id
                        state.lease = await state.first.claim(record.id, "holder-a", 0.001)
                        return record
                    # Secondary terminal loss must replace the original OSError
                    # tail, whether detected by flush or failed-status persistence.
                    secondary = state.target.startswith("secondary_")
                    logical_name = name
                    if name == "save_checkpoint_with_events":
                        logical_name = (
                            "mark_completed"
                            if kwargs.get("status") == "completed"
                            else "append_events"
                        )
                    if secondary and logical_name == "mark_completed" and not state.primary_failed:
                        state.primary_failed = True
                        raise OSError("primary completion failure")
                    target = state.target.removeprefix("secondary_")
                    matches = logical_name == target and (not secondary or state.primary_failed)
                    if target == "ledger_flush":
                        matches = name == "append_events" and any(
                            event.type == "model_call" for event in args[1]
                        )
                    if not session and matches and not state.takeover_done:
                        await state.take_over()
                    try:
                        return await method(*args, **kwargs)
                    except LeaseLostError:
                        if not state.lost:
                            state.first_loss = label
                        state.lost = True
                        raise

                return write

        return Store()


class _TextProvider:
    id = "lease-test"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, request: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": request.model}
        yield {"type": "text_delta", "text": "done"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(input_tokens=1, output_tokens=1),
        }


class _StopCounter:
    resume_policy_id = "test.lease-stop"
    resume_policy_version = "1"

    def __init__(self) -> None:
        self.results: list[Any] = []

    def on_agent_stop(self, ctx: Any) -> None:
        self.results.append(ctx.result)


@pytest.mark.parametrize(
    "target",
    [
        "save_checkpoint",
        "append_events",
        "ledger_flush",
        "save",
        "prune",
        "delete",
        "mark_completed",
        "secondary_mark_failed",
    ],
)
async def test_takeover_stops_all_later_writes_and_preserves_winner(
    lease_stores: Any, target: str
) -> None:
    from linch import Agent
    from linch.durability import DurabilityOptions
    from linch.sessions import InMemorySessionStore

    first, second = lease_stores
    state = _Takeover(first, second, target)
    stop = _StopCounter()
    agent = Agent(
        model="lease-model",
        provider=cast(Any, _TextProvider()),
        session_store=state.wrap(InMemorySessionStore(), session=True),
        run_store=state.wrap(first),
        hooks=[stop],
        cwd=".",
        permissions={"mode": "skip-dangerous"},
        durability=DurabilityOptions(exact_model_input=True),
    )
    session = await agent.session()
    started = asyncio.Event()
    cancelled_and_awaited = asyncio.Event()

    async def background() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cancelled_and_awaited.set()

    task = asyncio.create_task(background())
    session.background_tasks.append(task)
    await started.wait()
    try:
        events = [event async for event in session.run("hello")]
        assert state.lost, f"target {target} never exercised real fencing"
        assert state.after_loss == [], (state.first_loss, state.after_loss)
        assert cancelled_and_awaited.is_set() and task.done()
        assert len(stop.results) == 1
        assert stop.results[0].stop_reason == "lease_lost"
        assert [event.type for event in events[-2:]] == ["error", "result"]
        assert events[-2].error["name"] == "LeaseLostError"
        assert events[-2].error["retryable"] is False
        assert events[-1].subtype == "error"
        assert events[-1].stop_reason == "lease_lost"
        assert await second.load_run(state.run_id) == state.winner_record
        assert await second.load_events(state.run_id) == state.winner_events
        assert await second.load(state.winner_snapshot.id) == state.winner_snapshot
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.close()


async def test_model_call_journal_failure_keeps_original_store_error_class() -> None:
    from linch import Agent
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class FailLedgerStore(InMemoryRunStore):
        async def append_events(self, run_id: str, events: list[Any]) -> list[int]:
            if any(event.type == "model_call" for event in events):
                raise OSError("model-call store unavailable")
            return await super().append_events(run_id, events)

    agent = Agent(
        model="lease-model",
        provider=cast(Any, _TextProvider()),
        session_store=InMemorySessionStore(),
        run_store=FailLedgerStore(),
        cwd=".",
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    try:
        events = [event async for event in session.run("hello")]
        assert events[-2].error["name"] == "OSError"
        assert events[-2].error["message"] == "model-call store unavailable"
        assert events[-1].subtype == "error"
    finally:
        await agent.close()


async def test_alignment_dispatch_lease_loss_never_appends_another_error(lease_stores: Any) -> None:
    from linch import Agent
    from linch.loop.runtime import drain_alignment
    from linch.run_store import LeaseLostError, RunCheckpoint
    from linch.session import AlignmentEntry
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    first, second = lease_stores
    state = _Takeover(first, second, "save_checkpoint")
    runs = state.wrap(first)
    agent = Agent(
        model="lease-model",
        provider=cast(Any, _TextProvider()),
        session_store=state.wrap(InMemorySessionStore(), session=True),
        run_store=runs,
        cwd=".",
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    record = await runs.create_run(session.id)
    future = asyncio.get_running_loop().create_future()
    session.alignment_queue.append(AlignmentEntry(prompt="steer", images=None, future=future))

    async def dispatch(*args: Any) -> Any:
        await runs.save_checkpoint(
            record.id, RunCheckpoint(phase="started", prompt="A", turn_index=0, total_usage=Usage())
        )
        raise AssertionError("the stale holder must be fenced")

    try:
        with pytest.raises(LeaseLostError):
            async for _ in drain_alignment(session, record.id, dispatch):
                pass
        assert state.after_loss == []
        assert await second.load_events(record.id) == state.winner_events
        assert isinstance(future.exception(), LeaseLostError)
    finally:
        # Consume an exceptional alignment future even when the RED assertion
        # exits before the normal assertion reads it.
        if future.done():
            future.exception()
        await agent.close()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_run_creation_failure_propagates_unchanged_without_lifecycle(wrapped: bool) -> None:
    from linch import Agent
    from linch.run_store import LeaseLostError
    from linch.sessions import InMemorySessionStore

    class CreationLosesLease:
        async def close(self) -> None:
            pass

        async def create_run(self, *args: Any, **kwargs: Any) -> Any:
            if wrapped:
                try:
                    raise LeaseLostError("admission lease lost")
                except LeaseLostError:
                    raise RuntimeError("admission wrapper") from None
            raise LeaseLostError("admission lease lost")

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"creation loss must not access {name}")

    stop = _StopCounter()
    agent = Agent(
        model="lease-model",
        provider=cast(Any, _TextProvider()),
        session_store=InMemorySessionStore(),
        run_store=CreationLosesLease(),
        hooks=[stop],
        cwd=".",
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    started = asyncio.Event()
    cancelled_and_awaited = asyncio.Event()

    async def background() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cancelled_and_awaited.set()

    task = asyncio.create_task(background())
    session.background_tasks.append(task)
    await started.wait()
    try:
        with pytest.raises(RuntimeError if wrapped else LeaseLostError) as raised:
            _ = [event async for event in session.run("hello")]
        assert str(raised.value) == ("admission wrapper" if wrapped else "admission lease lost")
        assert not task.done()
        assert stop.results == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.close()


async def test_early_close_flush_lease_loss_cancels_background_and_reports_stop(
    lease_stores: Any,
) -> None:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    first, second = lease_stores
    state = _Takeover(first, second, "manual-takeover")

    class Stop(_StopCounter):
        def on_before_provider_call(self, ctx):
            from linch import HookResult

            return HookResult.continue_()

    stop = Stop()
    agent = Agent(
        model="lease-model",
        provider=cast(Any, _TextProvider()),
        session_store=state.wrap(InMemorySessionStore(), session=True),
        run_store=state.wrap(first),
        hooks=[stop],
        cwd=".",
        permissions={"mode": "skip-dangerous"},
    )
    session = await agent.session()
    started = asyncio.Event()
    cancelled_and_awaited = asyncio.Event()

    async def background() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cancelled_and_awaited.set()

    task = asyncio.create_task(background())
    session.background_tasks.append(task)
    await started.wait()
    iterator = session.run("hello")
    try:
        async for event in iterator:
            if event.type == "hook":
                break
        assert event.type == "hook"
        # Startup is already atomic; this provider hook is buffered after it.
        # Teardown attempts a stale holder's actual fenced observational write.
        await state.take_over()
        await iterator.aclose()
        await iterator.aclose()

        assert state.lost
        assert state.first_loss == "run.append_events"
        assert state.after_loss == []
        assert cancelled_and_awaited.is_set() and task.done()
        assert len(stop.results) == 1
        assert stop.results[0].subtype == "error"
        assert stop.results[0].stop_reason == "lease_lost"
        assert stop.results[0].error["name"] == "LeaseLostError"
        assert stop.results[0].error["retryable"] is False
        assert session._event_journal is None
        assert await second.load_run(state.run_id) == state.winner_record
        assert await second.load_events(state.run_id) == state.winner_events
        assert await second.load(state.winner_snapshot.id) == state.winner_snapshot
    finally:
        await iterator.aclose()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.close()


@pytest.mark.parametrize("origin", ["ledger", "compaction_skills"])
async def test_fenced_persistence_origin_preserves_lease_exception(origin: str) -> None:
    from types import SimpleNamespace

    from linch import Context
    from linch.compaction import _persist_compaction_skill_context
    from linch.model_calls import ModelCallIntent, _ModelCallLedger
    from linch.run_store import LeaseLostError

    loss = LeaseLostError("origin fenced")

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise loss

    ledger = _ModelCallLedger(run_id="origin", persist=fail)
    with pytest.raises(LeaseLostError) as raised:
        if origin == "ledger":
            await ledger.begin(
                ModelCallIntent(
                    "logical", "delivery", None, "primary", "origin", 0, "model", "a" * 64, None
                )
            )
        else:
            session = SimpleNamespace(
                id="origin",
                agent=SimpleNamespace(context=Context()),
                invoked_skills=[SimpleNamespace(name="s", substituted_body="b", invoked_at=0)],
                store=SimpleNamespace(set_invoked_skills=fail),
            )
            await _persist_compaction_skill_context(session, ledger)
    assert raised.value is loss


async def test_failed_tail_atomic_lease_loss_skips_later_writes() -> None:
    import gc

    from linch import Agent, HookResult
    from linch.run_store import InMemoryRunStore, LeaseLostError
    from linch.sessions import InMemorySessionStore

    class Store(InMemoryRunStore):
        failed_calls = 0

        async def mark_failed(
            self,
            run_id: str,
            checkpoint: Any = None,
            error: Any = None,
            *,
            events: list[Any] = (),
        ) -> Any:
            self.failed_calls += 1
            assert any(event.type == "result" and event.subtype == "error" for event in events)
            raise LeaseLostError("terminal batch fenced")

    class StopHook:
        resume_policy_id = "test.tail-lease-stop"
        resume_policy_version = "1"

        def on_before_final_answer(self, ctx: Any) -> Any:
            return HookResult.stop("blocked")

    store = Store()
    agent = Agent(
        model="lease-model",
        provider=cast(Any, _TextProvider()),
        session_store=InMemorySessionStore(),
        run_store=store,
        hooks=[StopHook()],
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    session = await agent.session()
    try:
        events = [event async for event in session.run("hello")]
        assert store.failed_calls == 1
        assert events[-2].error["name"] == "LeaseLostError"
        assert events[-1].stop_reason == "lease_lost"
        gc.collect()
    finally:
        await agent.close()


async def test_blocked_prompt_terminal_lease_loss_reaches_single_agent_stop(
    lease_stores: Any,
) -> None:
    from linch import Agent, HookResult
    from linch.sessions import InMemorySessionStore

    class BlockHook(_StopCounter):
        def on_user_prompt_submit(self, ctx: Any) -> Any:
            return HookResult.block("blocked")

    first, second = lease_stores
    state = _Takeover(first, second, "mark_failed")
    stop = BlockHook()
    agent = Agent(
        model="lease-model",
        provider=cast(Any, _TextProvider()),
        session_store=InMemorySessionStore(),
        run_store=state.wrap(first),
        hooks=[stop],
        permissions={"mode": "skip-dangerous"},
        cwd=".",
    )
    session = await agent.session()
    try:
        events = [event async for event in session.run("hello")]
        assert state.lost and state.after_loss == []
        assert events[-2].error["name"] == "LeaseLostError"
        assert events[-1].stop_reason == "lease_lost"
        assert len(stop.results) == 1
        assert stop.results[0].stop_reason == "lease_lost"
        assert stop.results[0].error["name"] == "LeaseLostError"
        assert await second.load_run(state.run_id) == state.winner_record
        assert await second.load_events(state.run_id) == state.winner_events
    finally:
        await agent.close()
