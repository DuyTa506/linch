"""Store-write failures are surfaced as terminal loop events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest


class _TextProvider:
    id = "failure-tests"

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
    resume_policy_id = "test.stop-counter"
    resume_policy_version = "1"

    def __init__(self) -> None:
        self.calls = 0
        self.run_ids: list[str] = []

    def on_agent_stop(self, ctx: Any) -> None:
        self.calls += 1
        self.run_ids.append(ctx.run_id)


def _agent(
    *,
    session_store: Any,
    run_store: Any = None,
    hooks: list[Any] | None = None,
    **kw: Any,
) -> Any:
    from linch import Agent

    return Agent(
        model="failure-model",
        provider=cast(Any, _TextProvider()),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        hooks=hooks,
        cwd=".",
        **kw,
    )


async def _events(session: Any) -> list[Any]:
    return [event async for event in session.run("hello")]


class _FailCheckpointStore:
    def __init__(self) -> None:
        from linch.run_store import InMemoryRunStore

        self._inner = InMemoryRunStore()
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def save_checkpoint(
        self, run_id: str, checkpoint: Any, *, status: str = "running"
    ) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise OSError("checkpoint write failed")
        return await self._inner.save_checkpoint(run_id, checkpoint, status=status)

    async def save_checkpoint_with_events(
        self, run_id: str, checkpoint: Any, events: list[Any], *, status: str = "running"
    ) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise OSError("checkpoint write failed")
        return await self._inner.save_checkpoint_with_events(
            run_id, checkpoint, events, status=status
        )


class _FailEventStore:
    def __init__(self) -> None:
        from linch.run_store import InMemoryRunStore

        self._inner = InMemoryRunStore()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def append_events(self, run_id: str, events: list[Any]) -> list[int]:
        raise OSError("event flush failed")

    async def save_checkpoint_with_events(
        self, run_id: str, checkpoint: Any, events: list[Any], *, status: str = "running"
    ) -> Any:
        raise OSError("event flush failed")


class _FailCompletedStore:
    def __init__(self) -> None:
        from linch.run_store import InMemoryRunStore

        self._inner = InMemoryRunStore()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def mark_completed(self, run_id: str, checkpoint: Any) -> Any:
        raise OSError("completion write failed")

    async def save_checkpoint_with_events(
        self, run_id: str, checkpoint: Any, events: list[Any], *, status: str = "running"
    ) -> Any:
        if status == "completed":
            raise OSError("completion write failed")
        return await self._inner.save_checkpoint_with_events(
            run_id, checkpoint, events, status=status
        )


class _FailCreateStore:
    def __init__(self) -> None:
        self.error = OSError("run creation failed")

    async def create_run(self, session_id: str, **kwargs: Any) -> Any:
        raise self.error


class _FailCompletedAndFailedStore(_FailCompletedStore):
    async def mark_failed(
        self,
        run_id: str,
        checkpoint: Any = None,
        error: dict[str, object] | None = None,
        *,
        events: list[Any] = (),
    ) -> Any:
        raise RuntimeError("secondary failure")


class _RetryableCompletionError(RuntimeError):
    retryable = True


class _FailRetryableCompletedStore(_FailCompletedStore):
    async def mark_completed(self, run_id: str, checkpoint: Any) -> Any:
        raise _RetryableCompletionError("try again")

    async def save_checkpoint_with_events(
        self, run_id: str, checkpoint: Any, events: list[Any], *, status: str = "running"
    ) -> Any:
        if status == "completed":
            raise _RetryableCompletionError("try again")
        return await self._inner.save_checkpoint_with_events(
            run_id, checkpoint, events, status=status
        )


class _FailSessionStore:
    def __init__(self) -> None:
        from linch.sessions import InMemorySessionStore

        self._inner = InMemorySessionStore()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def append_messages(self, session_id: str, messages: list[Any]) -> Any:
        raise OSError("session append failed")


class _FailPruneStore:
    def __init__(self) -> None:
        from linch.run_store import InMemoryRunStore

        self._inner = InMemoryRunStore()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def prune(self, run_id: str, keep_ids: tuple[str, ...] = ()) -> int:
        raise OSError("snapshot prune failed")


class _FailDeleteStore:
    def __init__(self) -> None:
        from linch.run_store import InMemoryRunStore

        self._inner = InMemoryRunStore()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def delete(self, snapshot_id: str, *, run_id: str) -> None:
        raise OSError("snapshot delete failed")


class _FailProviderSnapshotStore:
    def __init__(self) -> None:
        from linch.sessions import InMemorySessionStore

        self._inner = InMemorySessionStore()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def save_provider_snapshot(self, session_id: str, snapshot: Any) -> None:
        raise OSError("provider snapshot failed")


class _FailMarkFailedStore:
    def __init__(self) -> None:
        from linch.run_store import InMemoryRunStore

        self._inner = InMemoryRunStore()
        self.mark_failed_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def mark_failed(
        self,
        run_id: str,
        checkpoint: Any = None,
        error: dict[str, object] | None = None,
        *,
        events: list[Any] = (),
    ) -> Any:
        self.mark_failed_calls += 1
        raise OSError("failed status write")


@pytest.mark.parametrize(
    ("store_factory", "expected"),
    [
        (_FailCheckpointStore, "OSError"),
        (_FailEventStore, "OSError"),
        (_FailCompletedStore, "OSError"),
    ],
)
async def test_run_store_write_failure_is_error_result_and_single_stop(
    store_factory: Any, expected: str
) -> None:
    from linch.sessions import InMemorySessionStore

    stop = _StopCounter()
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=store_factory(),
        hooks=[stop],
    )
    session = await agent.session(id=f"store-{expected}")

    events = await _events(session)

    assert [event.type for event in events[-2:]] == ["error", "result"]
    assert events[-2].error["name"] == expected
    assert events[-2].error["retryable"] is False
    assert events[-1].subtype == "error"
    assert stop.calls == 1


async def test_session_append_failure_without_run_store_is_terminalized() -> None:
    stop = _StopCounter()
    agent = _agent(session_store=_FailSessionStore(), hooks=[stop])
    session = await agent.session(id="session-only")

    events = await _events(session)

    assert [event.type for event in events[-2:]] == ["error", "result"]
    assert events[-2].error["name"] == "OSError"
    assert events[-1].subtype == "error"
    assert stop.calls == 1


@pytest.mark.parametrize("failure", ["checkpoint", "session", "provider"])
async def test_error_pair_is_durable_when_failed_status_becomes_visible(failure: str) -> None:
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class RecordingStore(InMemoryRunStore):
        visible_terminal_pairs: list[list[str]]

        def __init__(self) -> None:
            super().__init__()
            self.visible_terminal_pairs = []

        async def save_checkpoint(
            self, run_id: str, checkpoint: Any, *, status: str = "running"
        ) -> Any:
            if failure == "checkpoint":
                raise OSError("checkpoint unavailable")
            return await super().save_checkpoint(run_id, checkpoint, status=status)

        async def save_checkpoint_with_events(
            self, run_id: str, checkpoint: Any, events: list[Any], *, status: str = "running"
        ) -> Any:
            if failure == "checkpoint":
                raise OSError("checkpoint unavailable")
            return await super().save_checkpoint_with_events(
                run_id, checkpoint, events, status=status
            )

        async def mark_failed(
            self,
            run_id: str,
            checkpoint: Any = None,
            error: Any = None,
            *,
            events: list[Any] = (),
        ) -> Any:
            record = await super().mark_failed(run_id, checkpoint, error=error, events=events)
            visible = await self.load_run(run_id)
            assert visible is not None and visible.status == "failed"
            events = await self.load_events(run_id)
            self.visible_terminal_pairs.append([row.event.type for row in events[-2:]])
            return record

    class FailProvider(_TextProvider):
        async def stream(self, request: Any) -> AsyncIterator[dict[str, object]]:
            raise OSError("provider unavailable")
            yield  # pragma: no cover

    store = RecordingStore()
    stop = _StopCounter()
    agent = _agent(
        session_store=_FailSessionStore() if failure == "session" else InMemorySessionStore(),
        run_store=store,
        hooks=[stop],
    )
    if failure == "provider":
        agent.provider = cast(Any, FailProvider())
    session = await agent.session(id=f"ordered-{failure}")
    iterator = session.run("hello")
    try:
        async for event in iterator:
            if event.type == "error":
                assert store.visible_terminal_pairs == [["error", "result"]]
                break
        else:
            pytest.fail("the injected failure must emit an error event")
    finally:
        await iterator.aclose()
        await agent.close()
    assert stop.calls == 1


async def test_run_creation_failure_raises_before_loop() -> None:
    from linch.sessions import InMemorySessionStore

    stop = _StopCounter()
    store = _FailCreateStore()
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=store,
        hooks=[stop],
    )
    session = await agent.session(id="create-failure")

    with pytest.raises(OSError, match="run creation failed") as raised:
        await _events(session)

    assert raised.value is store.error
    assert stop.calls == 0


async def test_secondary_terminal_write_is_logged_with_run_and_original_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from linch.sessions import InMemorySessionStore

    stop = _StopCounter()
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=_FailCompletedAndFailedStore(),
        hooks=[stop],
    )
    session = await agent.session(id="secondary")

    events = await _events(session)

    assert events[-2].error["name"] == "OSError"
    assert stop.calls == 1
    record = next(record for record in caplog.records if "run.mark_failed" in record.message)
    assert stop.run_ids and stop.run_ids[0] in record.message
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)
    assert record.exc_info[1].__context__ is not None
    assert record.exc_info[1].__context__.__class__ is OSError


async def test_retryable_attribute_is_propagated_to_error_event() -> None:
    from linch.sessions import InMemorySessionStore

    stop = _StopCounter()
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=_FailRetryableCompletedStore(),
        hooks=[stop],
    )
    session = await agent.session(id="retryable")

    events = await _events(session)

    assert events[-2].error["name"] == "_RetryableCompletionError"
    assert events[-2].error["retryable"] is True
    assert stop.calls == 1


async def test_exact_model_input_snapshot_failure_keeps_original_exception_class() -> None:
    from linch.durability import DurabilityOptions
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class FailSnapshotStore(InMemoryRunStore):
        async def save(self, run_id: str, provider_attempt: int, request: Any) -> Any:
            raise LookupError("snapshot write failed")

    stop = _StopCounter()
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=FailSnapshotStore(),
        hooks=[stop],
        durability=DurabilityOptions(exact_model_input=True),
    )
    session = await agent.session(id="snapshot")

    events = await _events(session)

    assert events[-2].error["name"] == "LookupError"
    assert events[-1].subtype == "error"
    assert stop.calls == 1


@pytest.mark.parametrize("events_to_consume", [1, 2])
async def test_startup_failure_closing_after_terminal_event_still_dispatches_stop(
    events_to_consume: int,
) -> None:
    from linch.sessions import InMemorySessionStore

    stop = _StopCounter()
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=_FailCheckpointStore(),
        hooks=[stop],
    )
    session = await agent.session(id="startup-close")
    iterator = session.run("hello")

    events = [await anext(iterator) for _ in range(events_to_consume)]
    assert events[0].type == "error"
    await iterator.aclose()

    assert stop.calls == 1


@pytest.mark.parametrize(
    ("store_factory", "message"),
    [(_FailPruneStore, "snapshot prune failed"), (_FailDeleteStore, "snapshot delete failed")],
)
async def test_exact_model_input_cleanup_failure_is_best_effort(
    store_factory: Any,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from linch.durability import DurabilityOptions
    from linch.sessions import InMemorySessionStore

    stop = _StopCounter()
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=store_factory(),
        hooks=[stop],
        durability=DurabilityOptions(exact_model_input=True),
    )
    session = await agent.session(id="prune")

    events = await _events(session)

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert message in caplog.text
    assert stop.calls == 1


async def test_provider_snapshot_write_failure_is_best_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from linch.loop.runtime import maybe_save_provider_snapshot

    session_store = _FailProviderSnapshotStore()
    agent = _agent(session_store=session_store)
    session = await agent.session(id="provider-snapshot")
    await session.append([])
    session.session_log.record_projection([], reason="test")

    await maybe_save_provider_snapshot(session)

    assert "provider snapshot failed" in caplog.text


async def test_alignment_append_failure_escapes_for_typed_run_terminal() -> None:
    from linch.loop.runtime import drain_alignment
    from linch.session import AlignmentEntry

    agent = _agent(session_store=_FailSessionStore())
    session = await agent.session(id="alignment-failure")
    future = asyncio.get_running_loop().create_future()
    session.alignment_queue.append(AlignmentEntry(prompt="steer", images=None, future=future))

    async def dispatch(prompt: str, images: Any, source: str) -> tuple[str, Any, list[Any], None]:
        return prompt, images, [], None

    with pytest.raises(OSError, match="session append failed"):
        async for _ in drain_alignment(session, "run-alignment", dispatch):
            pass


async def test_terminal_mark_failed_failure_has_one_bounded_error_status_attempt() -> None:
    from linch import HookResult
    from linch.sessions import InMemorySessionStore

    class StopHook:
        resume_policy_id = "test.stop-hook"
        resume_policy_version = "1"

        def on_before_final_answer(self, ctx: Any) -> Any:
            return HookResult.stop("blocked")

    store = _FailMarkFailedStore()
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=store,
        hooks=[StopHook()],
    )
    session = await agent.session(id="mark-failed-once")

    events = await _events(session)

    assert [event.type for event in events[-2:]] == ["error", "result"]
    assert events[-2].error["name"] == "OSError"
    assert events[-2].error["message"] == "failed status write"
    assert store.mark_failed_calls == 2


async def test_abort_terminal_write_failure_persists_error_pair_before_failed_status() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class FailAbortStore(InMemoryRunStore):
        failed_calls = 0
        visible_terminal_pairs: list[list[str]]

        def __init__(self) -> None:
            super().__init__()
            self.visible_terminal_pairs = []

        async def save_checkpoint(
            self, run_id: str, checkpoint: Any, *, status: str = "running"
        ) -> Any:
            if status == "aborted":
                raise OSError("abort status failed")
            return await super().save_checkpoint(run_id, checkpoint, status=status)

        async def save_checkpoint_with_events(
            self, run_id: str, checkpoint: Any, events: list[Any], *, status: str = "running"
        ) -> Any:
            if status == "aborted":
                raise OSError("abort status failed")
            return await super().save_checkpoint_with_events(
                run_id, checkpoint, events, status=status
            )

        async def mark_failed(
            self,
            run_id: str,
            checkpoint: Any = None,
            error: Any = None,
            *,
            events: list[Any] = (),
        ) -> Any:
            self.failed_calls += 1
            record = await super().mark_failed(run_id, checkpoint, error=error, events=events)
            visible = await self.load_run(run_id)
            assert visible is not None and visible.status == "failed"
            events = await self.load_events(run_id)
            self.visible_terminal_pairs.append([row.event.type for row in events[-2:]])
            return record

    store = FailAbortStore()
    stop = _StopCounter()
    agent = _agent(session_store=InMemorySessionStore(), run_store=store, hooks=[stop])
    session = await agent.session(id="abort-failure")
    iterator = session.run("hello")
    system = await anext(iterator)
    session.abort()
    events = [event async for event in iterator]
    run = await store.load_run(system.run_id)
    assert run is not None and run.status == "failed"
    assert store.failed_calls == 1
    assert store.visible_terminal_pairs == [["error", "result"]]
    assert [event.type for event in events[-2:]] == ["error", "result"]
    assert events[-2].error["name"] == "OSError"
    assert stop.calls == 1


@pytest.mark.parametrize("close_after", ["error", "result", None])
async def test_blocked_prompt_stops_once_even_when_closed_at_terminal(
    close_after: str | None,
) -> None:
    from linch import HookResult
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class BlockHook(_StopCounter):
        def on_user_prompt_submit(self, ctx: Any) -> Any:
            return HookResult.block("blocked")

    stop = BlockHook()
    agent = _agent(session_store=InMemorySessionStore(), run_store=InMemoryRunStore(), hooks=[stop])
    session = await agent.session()
    iterator = session.run("hello")
    try:
        async for event in iterator:
            if event.type == close_after:
                break
    finally:
        await iterator.aclose()
    assert stop.calls == 1


@pytest.mark.parametrize("route", ["budget", "max_turns", "stop", "abort", "interrupt"])
async def test_each_terminal_route_commits_result_with_status(route: str) -> None:
    from linch import HookResult, RunBudget
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class Store(InMemoryRunStore):
        terminals: list[list[Any]]

        async def append_events(self, run_id, events):
            assert not any(event.type == "result" for event in events)
            return await super().append_events(run_id, events)

        async def save_checkpoint_with_events(self, run_id, checkpoint, events, **kw):
            if kw.get("status") in {"aborted", "completed"}:
                self.terminals.append(list(events))
            return await super().save_checkpoint_with_events(run_id, checkpoint, events, **kw)

        async def mark_failed(self, run_id, checkpoint=None, error=None, *, events=()):
            self.terminals.append(list(events))
            return await super().mark_failed(run_id, checkpoint, error, events=events)

    class Stop:
        resume_policy_id = "test.atomic-stop"
        resume_policy_version = "1"

        def on_before_final_answer(self, ctx):
            return HookResult.stop("stop")

    store = Store()
    store.terminals = []
    options: dict[str, Any] = {}
    if route == "budget":
        options["budget"] = RunBudget(max_tokens=1)
    if route == "max_turns":
        options["max_turns"] = 1
    agent = _agent(
        session_store=InMemorySessionStore(),
        run_store=store,
        hooks=[Stop()] if route == "stop" else None,
        **options,
    )
    if route in {"budget", "max_turns"}:

        class ToolProvider(_TextProvider):
            async def stream(self, request):
                from linch.types import Usage

                yield {"type": "message_start", "model": request.model}
                yield {"type": "tool_use_start", "id": "tool", "name": "Unavailable"}
                yield {"type": "tool_use_end", "id": "tool"}
                yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage(1, 1)}

        agent.provider = ToolProvider()
    session = await agent.session()
    iterator = session.run("hello")
    try:
        await anext(iterator)
        if route == "abort":
            session.abort()
        if route == "interrupt":
            session.interrupt()
        events = [event async for event in iterator]
        assert len(store.terminals) == 1
        assert store.terminals[0][-1] == events[-1]
        assert events[-1].subtype == {
            "abort": "aborted",
            "interrupt": "interrupted",
        }.get(route, "error")
    finally:
        await agent.close()
