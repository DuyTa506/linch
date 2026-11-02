from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.stores import memory_run_store as _memory_run_store
from tests.support.stores import memory_session_store as _memory_session_store


async def _pin_run_contract(session: Any, run_id: str, opts: Any = None) -> None:
    """Give a hand-built in-memory checkpoint its original execution contract."""
    from tests.support.runs import pin_run_contract

    await pin_run_contract(session, run_id, opts)


class ScriptProvider:
    id = "script"

    def __init__(
        self,
        *,
        tool_names: list[str] | None = None,
        fail_on_call: bool = False,
        partial_before_final: bool = False,
    ) -> None:
        self.tool_names = list(tool_names or [])
        self.fail_on_call = fail_on_call
        self.partial_before_final = partial_before_final
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        if self.fail_on_call:
            raise AssertionError("provider should not be called")
        self.calls += 1
        yield {"type": "message_start", "model": req.model}

        if _last_message_is_tool_result(req.messages) or not self.tool_names:
            if self.partial_before_final:
                yield {"type": "text_delta", "text": "partial"}
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
            return

        for idx, name in enumerate(self.tool_names, start=1):
            tool_id = f"call-{idx}"
            yield {"type": "tool_use_start", "id": tool_id, "name": name}
            yield {
                "type": "tool_use_input_delta",
                "id": tool_id,
                "json_delta": json.dumps({"value": name}),
            }
            yield {"type": "tool_use_end", "id": tool_id}
        yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}


class ThreeBatchProvider:
    """Emit the same tool call for three batches, then answer without tools."""

    id = "three-batch"

    def __init__(self, *, start_calls: int = 0) -> None:
        self.calls = start_calls

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if not req.tools:
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
            return
        tool_id = f"call-{self.calls}"
        yield {"type": "tool_use_start", "id": tool_id, "name": "A"}
        yield {
            "type": "tool_use_input_delta",
            "id": tool_id,
            "json_delta": json.dumps({"value": "same"}),
        }
        yield {"type": "tool_use_end", "id": tool_id}
        yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}


class _Crash(BaseException):
    pass


class _CheckpointCrashStore:
    """Crash once at a selected checkpoint boundary, preserving prior writes."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.boundary: str | None = None
        self.crashed = False
        self.drop_assistant_events = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def reset(self, boundary: str | None) -> None:
        self.boundary = boundary
        self.crashed = False

    async def save_checkpoint(self, run_id: str, checkpoint: Any, **kwargs: Any) -> Any:
        if (
            not self.crashed
            and self.boundary is not None
            and self.boundary == f"checkpoint:{checkpoint.phase}"
        ):
            self.crashed = True
            raise _Crash(self.boundary)
        return await self.store.save_checkpoint(run_id, checkpoint, **kwargs)

    async def save_checkpoint_with_events(
        self, run_id: str, checkpoint: Any, events: list[Any], **kwargs: Any
    ) -> Any:
        if (
            not self.crashed
            and self.boundary is not None
            and self.boundary == f"checkpoint:{checkpoint.phase}"
        ):
            self.crashed = True
            raise _Crash(self.boundary)
        if self.drop_assistant_events:
            events = [event for event in events if getattr(event, "type", None) != "assistant"]
        return await self.store.save_checkpoint_with_events(run_id, checkpoint, events, **kwargs)

    async def append_event(self, run_id: str, event: Any) -> int:
        if self.drop_assistant_events and getattr(event, "type", None) == "assistant":
            return 0
        return await self.store.append_event(run_id, event)

    async def append_events(self, run_id: str, events: list[Any]) -> list[int]:
        return [await self.append_event(run_id, event) for event in events]


class _TransientCheckpointStore:
    """Inject one connection loss at the durable tool checkpoint boundary."""

    def __init__(
        self,
        store: Any,
        error_factory: Any = None,
        *,
        fail_after_commit: bool = False,
    ) -> None:
        self.store = store
        self.error_factory = error_factory
        self.fail_after_commit = fail_after_commit
        self.fail_once = True
        self.failure_seen = False
        self.after_failure_calls: list[str] = []
        self.checkpoint_phases: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def _record_after_failure(self, name: str) -> None:
        if self.failure_seen:
            self.after_failure_calls.append(name)

    async def save_checkpoint(self, run_id: str, checkpoint: Any, **kwargs: Any) -> Any:
        self.checkpoint_phases.append(checkpoint.phase)
        self._record_after_failure("save_checkpoint")
        return await self.store.save_checkpoint(run_id, checkpoint, **kwargs)

    async def save_checkpoint_with_events(
        self, run_id: str, checkpoint: Any, events: list[Any], **kwargs: Any
    ) -> Any:
        self.checkpoint_phases.append(checkpoint.phase)
        if self.fail_once and checkpoint.phase == "tool_batch_pending":
            self.fail_once = False
            self.failure_seen = True
            import asyncpg

            error = (
                self.error_factory()
                if self.error_factory is not None
                else asyncpg.ConnectionDoesNotExistError("database connection lost")
            )
            if self.fail_after_commit:
                await self.store.save_checkpoint_with_events(run_id, checkpoint, events, **kwargs)
                raise error
            raise error
        self._record_after_failure("save_checkpoint_with_events")
        return await self.store.save_checkpoint_with_events(run_id, checkpoint, events, **kwargs)

    async def append_event(self, run_id: str, event: Any) -> int:
        self._record_after_failure("append_event")
        return await self.store.append_event(run_id, event)

    async def append_events(self, run_id: str, events: list[Any]) -> list[int]:
        self._record_after_failure("append_events")
        return await self.store.append_events(run_id, events)

    async def mark_failed(self, run_id: str, *args: Any, **kwargs: Any) -> Any:
        self._record_after_failure("mark_failed")
        return await self.store.mark_failed(run_id, *args, **kwargs)


class _CrashAfterFinalToolAckStore:
    """Persist the terminal-tool acknowledgement, then simulate process loss."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.crashed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def append_messages(self, session_id: str, messages: list[Any]) -> Any:
        result = await self.store.append_messages(session_id, messages)
        if not self.crashed and any(
            any(
                getattr(block, "content", None) == "Final answer captured by the host."
                for block in message.content
            )
            for message in messages
        ):
            self.crashed = True
            raise _Crash("after-final-tool-ack")
        return result


class _CrashAfterSessionMessageStore:
    """Persist one matching text message, then simulate process loss."""

    def __init__(self, store: Any, marker: str) -> None:
        self.store = store
        self.marker = marker
        self.crashed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def append_messages(self, session_id: str, messages: list[Any]) -> Any:
        result = await self.store.append_messages(session_id, messages)
        if not self.crashed and any(
            any(self.marker in getattr(block, "text", "") for block in message.content)
            for message in messages
        ):
            self.crashed = True
            raise _Crash(f"after-message:{self.marker}")
        return result


def _agent(**kwargs):
    options: dict[str, Any] = {"model": "gpt-5", "cwd": "."}
    options.update(kwargs)
    provider = options.pop("provider", None)
    options.setdefault("permissions", None)
    options.setdefault("run_store", None)
    return make_agent(provider, **options)


def _registry(counts: dict[str, int], *names: str, scope: str = "read"):
    from tests.support.tools import StubTool, tool_registry

    tools = []
    for name in names:

        def count(_input: dict[str, Any], _ctx: Any, *, name: str = name) -> None:
            counts[name] = counts.get(name, 0) + 1

        tools.append(StubTool(name, scope=scope, on_execute=count))
    return tool_registry(*tools)


def _last_message_is_tool_result(messages: list[Any]) -> bool:
    if not messages:
        return False
    return any(getattr(block, "type", None) == "tool_result" for block in messages[-1].content)


async def _run_until(session, prompt: str, predicate) -> tuple[str, list[Any]]:
    events: list[Any] = []
    run_id = ""
    async for event in session.run(prompt):
        events.append(event)
        if getattr(event, "type", None) == "system":
            run_id = event.run_id
        if predicate(event):
            break
    assert run_id
    return run_id, events


async def _collect(iterator) -> list[Any]:
    return [event async for event in iterator]


async def _collect_until(iterator, predicate) -> list[Any]:
    events: list[Any] = []
    async for event in iterator:
        events.append(event)
        if predicate(event):
            break
    return events


async def test_resume_after_user_append_does_not_duplicate_user_message() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()

    provider = ScriptProvider()
    agent = _agent(
        provider=provider,
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "hello", lambda event: event.type == "user")

    restarted = _agent(
        provider=provider,
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    messages = await session_store.load_messages("s1")
    assert sum(1 for row in messages if row.message.role == "user") == 1
    assert events[-1].type == "result"
    assert provider.calls == 1


async def test_resume_retries_provider_before_assistant_append() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = ScriptProvider(partial_before_final=True)
    agent = _agent(
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        include_partial_messages=True,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(
        session,
        "hello",
        lambda event: event.type == "partial_assistant",
    )

    restarted = _agent(
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        include_partial_messages=True,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert provider.calls == 2


async def test_resume_after_assistant_tool_use_skips_provider() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "assistant")

    restarted = _agent(
        provider=ScriptProvider(fail_on_call=True),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect_until(
        resumed.resume(run_id),
        lambda event: event.type == "tool_call_end",
    )

    assert counts == {"A": 1}
    assert [event.type for event in events] == ["tool_call_start", "tool_call_end"]


async def test_resume_after_final_tool_ack_does_not_treat_ack_as_feedback() -> None:
    """A crash after final-tool acknowledgement still resumes successfully."""
    from tests.support.providers import ScriptedProvider, ToolUseTurn

    session_store = _CrashAfterFinalToolAckStore(_memory_session_store())
    run_store = _memory_run_store()
    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="answer", tool_input={"value": 1}, tool_id="final-1")]
    )
    agent = _agent(
        provider=provider,
        final_tool_name="answer",
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-final-tool-ack"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    restarted = _agent(
        provider=ScriptedProvider([]),
        final_tool_name="answer",
        session_store=session_store.store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].structured_output == {"value": 1}


async def test_resume_final_tool_ack_and_hook_retry_are_history_idempotent() -> None:
    """A composite terminal-tool tail must not duplicate its acknowledgement."""
    from linch import HookResult
    from linch.types import ToolResultBlock
    from tests.support.providers import ScriptedProvider, ToolUseTurn

    class RetryThenAccept:
        resume_policy_id = "test.resume-final-tool-retry"
        resume_policy_version = "1"

        def __init__(self) -> None:
            self.calls = 0

        def on_before_final_answer(self, _ctx: Any) -> Any:
            self.calls += 1
            return HookResult.retry("please revise") if self.calls == 1 else None

    session_store = _CrashAfterSessionMessageStore(_memory_session_store(), "please revise")
    run_store = _memory_run_store()
    agent = _agent(
        provider=ScriptedProvider(
            [ToolUseTurn(tool_name="answer", tool_input={"value": 1}, tool_id="final-1")]
        ),
        final_tool_name="answer",
        hooks=[RetryThenAccept()],
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:please revise"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    resumed_provider = ScriptedProvider(
        [ToolUseTurn(tool_name="answer", tool_input={"value": 2}, tool_id="final-2")]
    )
    restarted = _agent(
        provider=resumed_provider,
        final_tool_name="answer",
        hooks=[RetryThenAccept()],
        session_store=session_store.store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].structured_output == {"value": 2}
    assert len(resumed_provider.requests) == 1
    assert (
        sum(
            isinstance(block, ToolResultBlock) and block.tool_use_id == "final-1"
            for message in resumed.full_history
            for block in message.content
        )
        == 1
    )
    assert (
        sum(
            any("please revise" in getattr(block, "text", "") for block in message.content)
            for message in resumed.full_history
        )
        == 1
    )


async def test_resume_repeated_final_tool_hook_crashes_keep_ack_boundary() -> None:
    """Repeated final-tool hook crashes preserve the original ACK boundary."""
    from linch import HookResult
    from linch.types import ToolResultBlock
    from tests.support.providers import ScriptedProvider, ToolUseTurn

    class RetryThreeTimes:
        resume_policy_id = "test.resume-final-tool-retry-three"
        resume_policy_version = "1"

        def __init__(self) -> None:
            self.calls = 0

        def on_before_final_answer(self, _ctx: Any) -> Any:
            self.calls += 1
            return HookResult.retry("please revise") if self.calls <= 3 else None

    base_session_store = _memory_session_store()
    first_session_store = _CrashAfterSessionMessageStore(base_session_store, "please revise")
    run_store = _memory_run_store()
    hook = RetryThreeTimes()
    agent = _agent(
        provider=ScriptedProvider(
            [ToolUseTurn(tool_name="answer", tool_input={"value": 1}, tool_id="final-1")]
        ),
        final_tool_name="answer",
        hooks=[hook],
        session_store=first_session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:please revise"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    second_run_store = _CheckpointCrashStore(run_store)
    second_run_store.reset("checkpoint:turn_complete")
    second = _agent(
        provider=ScriptedProvider([]),
        final_tool_name="answer",
        hooks=[hook],
        session_store=base_session_store,
        run_store=second_run_store,
    )
    second_session = await second.session(id="s1")
    with pytest.raises(_Crash, match="checkpoint:turn_complete"):
        async for _event in second_session.resume(run_id):
            pass
    await second.close()
    stored_messages = await base_session_store.load_messages("s1")
    assistant_base_count = next(
        index + 1
        for index, row in enumerate(stored_messages)
        if any(getattr(block, "id", None) == "final-1" for block in row.message.content)
    )
    record = await run_store.load_run(run_id)
    assert record is not None
    assert record.checkpoint.extension_state["linch.resume"] == {
        "feedback_origin": "assistant_boundary",
        "feedback_base_count": assistant_base_count,
    }

    resumed_provider = ScriptedProvider(
        [ToolUseTurn(tool_name="answer", tool_input={"value": 2}, tool_id="final-2")]
    )
    restarted = _agent(
        provider=resumed_provider,
        final_tool_name="answer",
        hooks=[hook],
        session_store=base_session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].structured_output == {"value": 2}
    assert len(resumed_provider.requests) == 1
    assert (
        sum(
            isinstance(block, ToolResultBlock) and block.tool_use_id == "final-1"
            for message in resumed.full_history
            for block in message.content
        )
        == 1
    )
    assert (
        sum(
            any("please revise" in getattr(block, "text", "") for block in message.content)
            for message in resumed.full_history
        )
        == 1
    )


@pytest.mark.parametrize("checkpointed_listener", [False, True])
async def test_resume_after_replay_checkpoint_commit_preserves_accepted_tail(
    checkpointed_listener: bool,
) -> None:
    """A second crash after the replay write must preserve an accepted decision."""
    from linch import HookResult, SystemEvent
    from linch.types import ToolResultBlock
    from tests.support.providers import ScriptedProvider, ToolUseTurn

    class RetryAndCountAcceptedTurns:
        resume_policy_id = "test.resume-accepted-tail"
        resume_policy_version = "1"
        checkpoint_key = "test.accepted-turns"

        def __init__(self) -> None:
            self.hook_calls = 0
            self.accepted_turns: list[int] = []

        def __call__(self, event: Any) -> None:
            self.accepted_turns.append(event.turn_index)

        def on_before_final_answer(self, _ctx: Any) -> Any:
            self.hook_calls += 1
            return HookResult.retry("please revise") if self.hook_calls == 1 else None

        def checkpoint_state(self, _session: Any, _run_id: str) -> dict[str, Any]:
            return {"accepted_turns": list(self.accepted_turns)}

        def restore_checkpoint_state(
            self, state: dict[str, Any], _session: Any, _run_id: str
        ) -> None:
            self.accepted_turns = list(state["accepted_turns"])

    class CrashAfterReplayCheckpoint:
        def __init__(self, store: Any) -> None:
            self.store = store
            self.committed = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.store, name)

        def _crash_after_commit(self, checkpoint: Any) -> None:
            if checkpoint.phase == "assistant_appended":
                self.committed = True
                raise _Crash("after-replay-checkpoint-commit")

        async def save_checkpoint(self, run_id: str, checkpoint: Any, **kwargs: Any) -> Any:
            result = await self.store.save_checkpoint(run_id, checkpoint, **kwargs)
            self._crash_after_commit(checkpoint)
            return result

        async def save_checkpoint_with_events(
            self, run_id: str, checkpoint: Any, events: list[Any], **kwargs: Any
        ) -> Any:
            result = await self.store.save_checkpoint_with_events(
                run_id, checkpoint, events, **kwargs
            )
            self._crash_after_commit(checkpoint)
            return result

    base_session_store = _memory_session_store()
    session_store = _CrashAfterSessionMessageStore(base_session_store, "please revise")
    run_store = _memory_run_store()
    first_hook = RetryAndCountAcceptedTurns()
    agent = _agent(
        provider=ScriptedProvider(
            [ToolUseTurn(tool_name="answer", tool_input={"value": 1}, tool_id="final-1")]
        ),
        final_tool_name="answer",
        hooks=[first_hook],
        session_store=session_store,
        run_store=run_store,
    )
    if checkpointed_listener:
        agent.on_turn_stopping(first_hook)
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:please revise"):
        async for event in session.run("hello"):
            if isinstance(event, SystemEvent):
                run_id = event.run_id
    assert run_id
    await agent.close()

    second_run_store = CrashAfterReplayCheckpoint(run_store)
    second_hook = RetryAndCountAcceptedTurns()
    second = _agent(
        provider=ScriptedProvider([]),
        final_tool_name="answer",
        hooks=[second_hook],
        session_store=base_session_store,
        run_store=second_run_store,
    )
    if checkpointed_listener:
        second.on_turn_stopping(second_hook)
    second_session = await second.session(id="s1")
    with pytest.raises(_Crash, match="after-replay-checkpoint-commit"):
        await _collect(second_session.resume(run_id))
    assert second_run_store.committed
    await second.close()

    final_hook = RetryAndCountAcceptedTurns()
    resumed_provider = ScriptedProvider(
        [ToolUseTurn(tool_name="answer", tool_input={"value": 2}, tool_id="final-2")]
    )
    restarted = _agent(
        provider=resumed_provider,
        final_tool_name="answer",
        hooks=[final_hook],
        session_store=base_session_store,
        run_store=run_store,
    )
    if checkpointed_listener:
        restarted.on_turn_stopping(final_hook)
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].structured_output == {"value": 2}
    assert len(resumed_provider.requests) == 1
    assert final_hook.accepted_turns == ([0, 1] if checkpointed_listener else [])
    assert (
        sum(
            isinstance(block, ToolResultBlock) and block.tool_use_id == "final-1"
            for message in resumed.full_history
            for block in message.content
        )
        == 1
    )
    assert (
        sum(
            any("please revise" in getattr(block, "text", "") for block in message.content)
            for message in resumed.full_history
        )
        == 1
    )


async def test_resume_after_before_final_answer_retry_does_not_replay_as_turn_feedback() -> None:
    """A hook retry persisted after the assistant checkpoint is not turn feedback."""
    from linch import HookResult
    from linch.types import TextBlock
    from tests.support.providers import ScriptedProvider, TextTurn

    class RetryThenAccept:
        resume_policy_id = "test.resume-retry-once"
        resume_policy_version = "1"

        def __init__(self) -> None:
            self.calls = 0

        def on_before_final_answer(self, _ctx: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                return HookResult.retry("please revise")
            return None

    hook = RetryThenAccept()
    session_store = _CrashAfterSessionMessageStore(_memory_session_store(), "please revise")
    run_store = _memory_run_store()
    agent = _agent(
        provider=ScriptedProvider([TextTurn("draft")]),
        hooks=[hook],
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:please revise"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    resumed_hook = RetryThenAccept()
    restarted_provider = ScriptedProvider([TextTurn("revised")])
    restarted = _agent(
        provider=restarted_provider,
        hooks=[resumed_hook],
        session_store=session_store.store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].final_text == "revised"
    assert len(restarted_provider.requests) == 1
    assert (
        sum(
            any("please revise" in getattr(block, "text", "") for block in message.content)
            for message in resumed.full_history
        )
        == 1
    )
    assert [
        next((block.text for block in message.content if isinstance(block, TextBlock)), "")
        for message in resumed.full_history
        if message.role == "assistant"
    ] == ["draft", "revised"]


async def test_resume_after_truncation_nudge_clears_replayed_feedback() -> None:
    """A persisted truncation nudge must not poison the following turn."""
    from linch import TruncationRecovery
    from linch.loop.turn_stopping import TurnStoppingDecision
    from linch.types import Message, TextBlock
    from tests.support.providers import ScriptedProvider, TextTurn

    session_store = _CrashAfterSessionMessageStore(_memory_session_store(), "cut off")
    run_store = _memory_run_store()
    agent = _agent(
        provider=ScriptedProvider([TextTurn("part", stop_reason="max_tokens")]),
        truncation_recovery=TruncationRecovery(max_attempts=1, feedback="cut off; continue"),
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:cut off"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    restarted = _agent(
        provider=ScriptedProvider([TextTurn("done"), TextTurn("final")]),
        truncation_recovery=TruncationRecovery(max_attempts=1, feedback="cut off; continue"),
        session_store=session_store.store,
        run_store=run_store,
    )
    restarted.on_turn_stopping(
        lambda event: (
            TurnStoppingDecision(
                "continue",
                feedback=(Message(role="user", content=[TextBlock(text="next turn")]),),
            )
            if event.turn_index == 1
            else None
        )
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].final_text == "final"
    assert len(restarted.provider.requests) == 2


async def test_resume_rejects_changed_turn_stopping_feedback_decision() -> None:
    """A changed turn-stopping decision must not discard persisted feedback."""
    from linch.loop.turn_stopping import TurnStoppingDecision
    from linch.types import Message, TextBlock

    class ContinueThenAccept:
        def __init__(self) -> None:
            self.continue_ = True

        def __call__(self, _event: Any) -> Any:
            if self.continue_:
                return TurnStoppingDecision(
                    "continue", feedback=(Message(role="user", content=[TextBlock(text="retry")]),)
                )
            return TurnStoppingDecision("accept")

    listener = ContinueThenAccept()
    session_store = _CrashAfterSessionMessageStore(_memory_session_store(), "retry")
    run_store = _memory_run_store()
    agent = _agent(
        provider=ScriptProvider(),
        session_store=session_store,
        run_store=run_store,
    )
    agent.on_turn_stopping(listener)
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:retry"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    listener.continue_ = False
    restarted = _agent(
        provider=ScriptProvider(),
        session_store=session_store.store,
        run_store=run_store,
    )
    restarted.on_turn_stopping(listener)
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "error"


async def test_resume_completion_gate_does_not_recount_crashed_feedback() -> None:
    """A crashed completion correction is dispatched once after resume."""
    from linch import CompletionGate, ReliabilityPolicy
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn

    session_store = _CrashAfterSessionMessageStore(
        _memory_session_store(), "Completion requirements are missing"
    )
    run_store = _memory_run_store()
    agent = _agent(
        provider=ScriptedProvider([TextTurn("draft")]),
        tools=_registry({}, "prepare"),
        reliability=ReliabilityPolicy(
            max_correction_turns=1,
            completion=CompletionGate(("prepare",)),
        ),
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:Completion requirements are missing"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    restarted_provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="prepare", tool_input={}, tool_id="prepare-1"),
            TextTurn("final"),
        ]
    )
    restarted = _agent(
        provider=restarted_provider,
        tools=_registry({}, "prepare"),
        reliability=ReliabilityPolicy(
            max_correction_turns=1,
            completion=CompletionGate(("prepare",)),
        ),
        session_store=session_store.store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].final_text == "final"
    assert len(restarted_provider.requests) == 2
    rows = await run_store.load_events(run_id)
    assert sum(row.event.type == "reliability" for row in rows) == 1


async def test_resume_turn_stopping_feedback_keeps_equal_assistant_message() -> None:
    """Replay keeps feedback when it repeats the checkpointed assistant text."""
    from linch.loop.turn_stopping import TurnStoppingDecision
    from linch.types import Message, TextBlock
    from tests.support.providers import ScriptedProvider, TextTurn

    feedback = (
        Message(role="assistant", content=[TextBlock(text="draft")]),
        Message(role="user", content=[TextBlock(text="retry")]),
    )

    class ContinueOnce:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _event: Any) -> TurnStoppingDecision:
            self.calls += 1
            return (
                TurnStoppingDecision("continue", feedback=feedback)
                if self.calls == 1
                else TurnStoppingDecision("accept")
            )

    session_store = _CrashAfterSessionMessageStore(_memory_session_store(), "retry")
    run_store = _memory_run_store()
    agent = _agent(
        provider=ScriptedProvider([TextTurn("draft")]),
        session_store=session_store,
        run_store=run_store,
    )
    agent.on_turn_stopping(ContinueOnce())
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:retry"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    restarted_provider = ScriptedProvider([TextTurn("final")])
    restarted = _agent(
        provider=restarted_provider,
        session_store=session_store.store,
        run_store=run_store,
    )
    restarted.on_turn_stopping(ContinueOnce())
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].final_text == "final"
    assert len(restarted_provider.requests) == 1


async def test_resume_repeated_turn_stopping_crashes_keep_original_boundary() -> None:
    """Repeated crashes do not move the persisted feedback boundary forward."""
    from linch.loop.turn_stopping import TurnStoppingDecision
    from linch.types import Message, TextBlock
    from tests.support.providers import ScriptedProvider, TextTurn

    feedback = (Message(role="user", content=[TextBlock(text="retry")]),)

    class ContinueThreeTimes:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _event: Any) -> TurnStoppingDecision:
            self.calls += 1
            return (
                TurnStoppingDecision("continue", feedback=feedback)
                if self.calls <= 3
                else TurnStoppingDecision("accept")
            )

    listener = ContinueThreeTimes()
    base_session_store = _memory_session_store()
    first_session_store = _CrashAfterSessionMessageStore(base_session_store, "retry")
    run_store = _memory_run_store()
    agent = _agent(
        provider=ScriptedProvider([TextTurn("draft")]),
        session_store=first_session_store,
        run_store=run_store,
    )
    agent.on_turn_stopping(listener)
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash, match="after-message:retry"):
        async for event in session.run("hello"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    second_run_store = _CheckpointCrashStore(run_store)
    second_run_store.reset("checkpoint:turn_complete")
    second = _agent(
        provider=ScriptedProvider([]),
        session_store=base_session_store,
        run_store=second_run_store,
    )
    second.on_turn_stopping(listener)
    second_session = await second.session(id="s1")
    with pytest.raises(_Crash, match="checkpoint:turn_complete"):
        async for _event in second_session.resume(run_id):
            pass
    await second.close()

    resumed_provider = ScriptedProvider([TextTurn("final")])
    restarted = _agent(
        provider=resumed_provider,
        session_store=base_session_store,
        run_store=run_store,
    )
    restarted.on_turn_stopping(listener)
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert events[-1].final_text == "final"
    assert len(resumed_provider.requests) == 1


async def test_resume_after_one_tool_completed_runs_only_missing_tool() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A", "B"]),
        tools=_registry(counts, "A", "B"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(
        session,
        "use tools",
        lambda event: event.type == "tool_call_end" and event.tool_name == "A",
    )

    restarted = _agent(
        provider=ScriptProvider(fail_on_call=True),
        tools=_registry(counts, "A", "B"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect_until(
        resumed.resume(run_id),
        lambda event: event.type == "tool_call_end",
    )

    assert counts == {"A": 1, "B": 1}
    assert [event.tool_name for event in events if event.type == "tool_call_end"] == ["B"]


@pytest.mark.parametrize(
    "error_kind",
    ["connection_lost", "interface", "os_error", "connection_error", "wrapped"],
)
@pytest.mark.parametrize("fail_after_commit", [False, True], ids=["before-commit", "after-commit"])
async def test_transient_checkpoint_store_error_stops_without_terminal_and_resumes(
    error_kind: str, fail_after_commit: bool
) -> None:
    """A lost checkpoint connection leaves the run takeover-resumable."""
    asyncpg = pytest.importorskip("asyncpg")

    from linch import ErrorEvent, ResultEvent

    def error_factory() -> BaseException:
        if error_kind == "connection_lost":
            return asyncpg.ConnectionDoesNotExistError("database connection lost")
        if error_kind == "interface":
            return asyncpg.InterfaceError("connection is closed")
        if error_kind == "os_error":
            return ConnectionResetError("database socket lost")
        if error_kind == "connection_error":
            return ConnectionError("database connection lost")
        wrapped = RuntimeError("store wrapper")
        wrapped.__cause__ = ConnectionResetError("database socket lost")
        return wrapped

    session_store = _memory_session_store()
    injected = _TransientCheckpointStore(
        _memory_run_store(), error_factory, fail_after_commit=fail_after_commit
    )
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=injected,
    )
    session = await agent.session(id="s1")
    first_events = await _collect(session.run("use tool"))
    run_id = next(event.run_id for event in first_events if event.type == "system")

    # A transient durable-store failure must stop before terminal persistence;
    # the checkpoint itself is the replay boundary for the side effect.
    assert injected.failure_seen
    assert "tool_batch_pending" in injected.checkpoint_phases
    assert not any(isinstance(event, (ErrorEvent, ResultEvent)) for event in first_events)
    assert injected.after_failure_calls == []
    record = await injected.load_run(run_id)
    assert record is not None and record.status == "running"
    assert counts == {}

    restarted = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=injected,
    )
    resumed = await restarted.session(id="s1")
    resumed_events = await _collect(resumed.resume(run_id))

    assert resumed_events[-1].type == "result"
    assert counts == {"A": 1}
    stored = await injected.load_events(run_id)
    assert sum(isinstance(row.event, ErrorEvent) for row in stored) == 0
    assert sum(isinstance(row.event, ResultEvent) for row in stored) == 1
    assert sum(row.event.type == "tool_call_end" for row in stored) == 1
    assert [row.seq for row in stored] == list(range(1, len(stored) + 1))
    tool_result_blocks = [
        block
        for message in resumed.full_history
        for block in message.content
        if getattr(block, "type", None) == "tool_result"
    ]
    assert len(tool_result_blocks) == 1


async def test_transient_checkpoint_failure_ignores_telemetry_operation_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from linch import ErrorEvent, ResultEvent
    from linch.loop import checkpoint
    from linch.run_store import durable_call

    pytest.importorskip("asyncpg")

    async def opaque_durable_call(session: Any, op: str, awaitable: Any) -> Any:
        return await durable_call(session, "checkpoint-write", awaitable)

    monkeypatch.setattr(checkpoint, "durable_call", opaque_durable_call)
    injected = _TransientCheckpointStore(_memory_run_store())
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=_memory_session_store(),
        run_store=injected,
    )
    session = await agent.session(id="opaque-checkpoint")
    events = await _collect(session.run("use tool"))
    run_id = next(event.run_id for event in events if event.type == "system")

    assert injected.failure_seen
    assert not any(isinstance(event, (ErrorEvent, ResultEvent)) for event in events)
    assert injected.after_failure_calls == []
    record = await injected.load_run(run_id)
    assert record is not None and record.status == "running"
    assert counts == {}


async def test_tool_batch_checkpoint_is_not_saved_after_each_tool_end() -> None:
    from linch.run_store import InMemoryRunStore

    class CountingRunStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.saved_phases: list[str] = []

        async def save_checkpoint(self, run_id, checkpoint, *, status="running"):
            self.saved_phases.append(checkpoint.phase)
            return await super().save_checkpoint(run_id, checkpoint, status=status)

        async def save_checkpoint_with_events(
            self, run_id, checkpoint, events, *, status="running"
        ):
            self.saved_phases.append(checkpoint.phase)
            return await super().save_checkpoint_with_events(
                run_id, checkpoint, events, status=status
            )

    session_store = _memory_session_store()
    run_store = CountingRunStore()
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A", "B"]),
        tools=_registry(counts, "A", "B"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")

    events = await _collect(session.run("use tools"))

    assert events[-1].type == "result"
    assert counts == {"A": 1, "B": 1}
    assert run_store.saved_phases.count("tool_batch_pending") == 1
    # Write-amplification guard: the tool_executing checkpoint is saved once per
    # batch (by the permissions-resolved callback), NOT once per tool start/end.
    # Two tools previously produced four tool_executing saves.
    assert run_store.saved_phases.count("tool_executing") == 1


async def test_permission_pending_resume_reemits_before_tool_execution() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}

    def allow(_request) -> dict[str, str]:
        return {"behavior": "allow"}

    allow.__dict__["resume_policy_id"] = "test-allow"

    agent = _agent(
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": allow},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(
        session,
        "write",
        lambda event: event.type == "permission_request",
    )
    assert counts == {}

    restarted = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": allow},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    resumed = await restarted.session(id="s1")
    iterator = resumed.resume(run_id).__aiter__()
    first = await anext(iterator)
    assert first.type == "permission_request"
    assert counts == {}
    rest = [event async for event in iterator]

    assert any(event.type == "tool_call_end" for event in rest)
    assert counts == {"WriteThing": 1}


# ── Phase 2: background-worker notification drain ────────────────────────────


async def test_loop_drains_pending_notifications_at_turn_start() -> None:
    """Notifications in session.pending_notifications are injected as UserEvents."""
    from linch.types import Message, TextBlock

    session_store = _memory_session_store()
    agent = _agent(
        provider=ScriptProvider(),
        session_store=session_store,
    )
    session = await agent.session(id="s1")

    # Pre-populate a fake notification (simulates a completed background worker)
    note_text = (
        "<task-notification><task-id>agent-1234</task-id><status>completed</status>"
        "<result>done</result></task-notification>"
    )
    session.pending_notifications.append(Message(role="user", content=[TextBlock(text=note_text)]))

    events = await _collect(session.run("hello"))

    user_events = [e for e in events if e.type == "user"]
    assert any(
        note_text in (block.text for block in e.message.content if hasattr(block, "text"))
        for e in user_events
    ), "Notification should appear as a UserEvent before the provider is called"

    # Notification was drained from pending_notifications
    assert session.pending_notifications == []


async def test_notification_lands_in_provider_view_before_provider_call() -> None:
    """The notification is in provider_view when the provider sees the request."""
    from linch.types import Message, TextBlock

    seen_messages: list[Any] = []

    class CapturingProvider:
        id = "cap"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            from linch.types import Usage

            seen_messages.append(req.messages[:])
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    session_store = _memory_session_store()
    agent = _agent(
        provider=CapturingProvider(),
        session_store=session_store,
    )
    session = await agent.session(id="s1")

    note_text = (
        "<task-notification><task-id>agent-abcd</task-id><status>completed</status>"
        "<result>ok</result></task-notification>"
    )
    session.pending_notifications.append(Message(role="user", content=[TextBlock(text=note_text)]))

    await _collect(session.run("hello"))

    # The provider should have seen the notification message in its request
    assert seen_messages, "Provider should have been called"
    # Find the notification in any provider call's messages
    all_messages = [m for batch in seen_messages for m in batch]
    assert any(
        any(getattr(block, "text", "") == note_text for block in m.content) for m in all_messages
    ), "Notification should be in the provider's message list"


async def test_loop_abort_cancels_background_worker_tasks() -> None:
    """When the loop aborts, running background tasks are cancelled."""
    import asyncio

    from linch import Agent
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    class BlockingProvider:
        id = "blocking"
        _started = asyncio.Event()
        _gate = asyncio.Event()

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            self._started.set()
            await self._gate.wait()
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    child_provider = BlockingProvider()
    # Parent uses a ScriptProvider that calls Subagent with run_in_background=True,
    # then returns text — so the parent run ends with a background task still running.
    # We'll directly manipulate the session for this test.
    session_store = InMemorySessionStore()
    agent = Agent(
        model="gpt-5",
        provider=ScriptProvider(),
        session_store=session_store,
        permissions={"mode": "skip-dangerous"},
    )
    # agent.session() calls connect_subagents() internally
    session = await agent.session(id="s1")

    # Simulate a background task that blocks until we tell it to stop
    async def blocking_task() -> None:
        await child_provider._started.wait()
        # Just block here — represents an in-flight background worker
        await asyncio.sleep(10)

    task = asyncio.create_task(blocking_task())

    # Register the handle on the session
    from linch.subagents.types import AgentDefinition, AgentFrontmatter
    from linch.subagents.workers import WorkerHandle

    dummy_def = AgentDefinition(
        name="test",
        file_path="<test>",
        source="built-in",
        frontmatter=AgentFrontmatter(name="test", description="test"),
        body="",
    )
    handle = WorkerHandle(
        worker_id="agent-test",
        child_session_id="child-1",
        display_name="Test Worker",
        definition=dummy_def,
        status="running",
        task=task,
    )
    session.workers["agent-test"] = handle
    child_provider._started.set()  # let the task "start"

    await asyncio.sleep(0)  # yield so task runs

    # Abort the session — should cancel the background task
    session.abort()
    await asyncio.sleep(0.01)

    assert task.cancelled() or task.done(), "Background task should be cancelled on abort"


async def test_started_tool_recovered_as_interrupted_from_event_log() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _events = await _run_until(
        session,
        "use tool",
        lambda event: event.type == "tool_call_start",
    )

    run = await run_store.load_run(run_id)
    assert run is not None
    assert run.checkpoint is not None
    # The permissions-resolved callback saves the tool_executing checkpoint once,
    # before execution — per-tool checkpoint saves are gone, so the interrupted
    # placeholder is NOT written into the checkpoint anymore.
    assert run.checkpoint.phase == "tool_executing"
    assert "call-1" not in run.checkpoint.completed_tool_results
    # Event-log durability is the recovery source: a start with no matching end.
    stored = await run_store.load_events(run_id)
    assert [s.event.tool_use_id for s in stored if s.event.type == "tool_call_start"] == ["call-1"]
    assert [s for s in stored if s.event.type == "tool_call_end"] == []

    restarted = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    resume_events = await _collect(resumed.resume(run_id))

    # A started-but-unfinished tool is recovered as interrupted from the event
    # log and is not blindly re-run on resume.
    assert counts == {}
    assert any(event.type == "user" for event in resume_events)
    assert resume_events[-1].type == "result"


async def test_interrupted_tool_guard_recovery_survives_two_resume_cuts() -> None:
    """A recovered start without an end remains resumable after another crash."""
    session_store = _memory_session_store()
    run_store = _CheckpointCrashStore(_memory_run_store())
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "tool_call_start")

    run_store.reset("checkpoint:turn_complete")
    restarted = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    with pytest.raises(_Crash):
        await _collect(resumed.resume(run_id))

    checkpointed = await run_store.load_run(run_id)
    assert checkpointed is not None and checkpointed.checkpoint is not None
    assert checkpointed.checkpoint.phase == "tool_results_appended"

    run_store.reset(None)
    restarted_again = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed_again = await restarted_again.session(id="s1")
    events = await _collect(resumed_again.resume(run_id))
    assert counts == {}
    assert events[-1].type == "result"


async def test_guard_recovery_does_not_require_missing_assistant_observation() -> None:
    """A checkpointed assistant may be durable before its observation event."""
    session_store = _memory_session_store()
    run_store = _CheckpointCrashStore(_memory_run_store())
    run_store.drop_assistant_events = True
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "tool_call_start")
    assert not any(row.event.type == "assistant" for row in await run_store.load_events(run_id))

    run_store.reset("checkpoint:turn_complete")
    restarted = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    with pytest.raises(_Crash):
        await _collect(resumed.resume(run_id))

    run_store.reset(None)
    restarted_again = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed_again = await restarted_again.session(id="s1")
    events = await _collect(resumed_again.resume(run_id))

    assert counts == {}
    assert events[-1].type == "result"


async def test_guard_recovery_does_not_count_inflight_batch_twice() -> None:
    """A checkpointed in-flight batch is re-evaluated exactly once."""
    from linch import LoopGuard

    session_store = _memory_session_store()
    run_store = _CheckpointCrashStore(_memory_run_store())
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        loop_guard=LoopGuard(max_identical_tool_calls=2, max_consecutive_failures=0),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "tool_call_end")

    run_store.reset("checkpoint:tool_results_appended")
    restarted = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        loop_guard=LoopGuard(max_identical_tool_calls=2, max_consecutive_failures=0),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    with pytest.raises(_Crash):
        await _collect(resumed.resume(run_id))

    run_store.reset(None)
    restarted_again = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        loop_guard=LoopGuard(max_identical_tool_calls=2, max_consecutive_failures=0),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed_again = await restarted_again.session(id="s1")
    events = await _collect(resumed_again.resume(run_id))

    assert not any(event.type == "loop_guard" for event in events)
    assert events[-1].type == "result"


async def test_disabled_guard_has_no_checkpoint_state() -> None:
    from linch import LoopGuard

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        loop_guard=LoopGuard(max_identical_tool_calls=0, max_consecutive_failures=0),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    events = await _collect(session.run("use tool"))
    run_id = next(event.run_id for event in events if event.type == "system")

    run = await run_store.load_run(run_id)
    assert run is not None and run.checkpoint is not None
    assert run.checkpoint.loop_guard_state is None
    assert "linch.loop_guard.v1" not in run.checkpoint.extension_state


async def test_pending_guard_trip_replays_once_after_interleaved_host_notification() -> None:
    from linch import LoopGuard

    session_store = _memory_session_store()
    run_store = _CheckpointCrashStore(_memory_run_store())
    counts: dict[str, int] = {}
    guard = LoopGuard(
        max_identical_tool_calls=1, max_consecutive_failures=0, force_final_answer=True
    )
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        loop_guard=guard,
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "tool_call_end")

    run_store.reset("checkpoint:turn_complete")
    restarted = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        loop_guard=guard,
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    with pytest.raises(_Crash):
        await _collect(resumed.resume(run_id))
    rows_before_resume = await run_store.load_events(run_id)
    assert not any(row.event.type == "loop_guard" for row in rows_before_resume)
    from linch.types import Message, TextBlock

    await resumed.append([Message(role="user", content=[TextBlock(text="host note")])])
    await restarted.close()

    run_store.reset(None)
    restarted_again = _agent(
        provider=ScriptProvider(),
        tools=_registry(counts, "A"),
        loop_guard=guard,
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed_again = await restarted_again.session(id="s1")
    events = await _collect(resumed_again.resume(run_id))

    assert sum(event.type == "loop_guard" for event in events) == 1
    rows = await run_store.load_events(run_id)
    assert sum(row.event.type == "loop_guard" for row in rows) == 1
    reminders = [
        message
        for message in resumed_again.full_history
        if any(
            "You appear to be stuck in a loop" in getattr(block, "text", "")
            for block in message.content
        )
    ]
    assert len(reminders) == 1


def _failing_registry(counts: dict[str, int]):
    from linch.tools import ToolResult
    from tests.support.tools import StubTool, tool_registry

    class FailingTool(StubTool):
        async def execute(self, input: dict[str, Any], ctx: Any):
            counts[self.name] = counts.get(self.name, 0) + 1
            return ToolResult(content=f"{self.name}:{counts[self.name]}", is_error=True)

    return tool_registry(FailingTool("A"))


def _guard_run_summary(events: list[Any], history: list[Any], checkpoint: Any) -> tuple[Any, ...]:
    from linch.events import LoopGuardEvent

    decisions = [event for event in events if isinstance(event, LoopGuardEvent)]
    assert len(decisions) == 1
    tool_batches = sum(
        1
        for message in history
        if any(getattr(block, "type", None) == "tool_result" for block in message.content)
    )
    assert checkpoint is not None
    state = checkpoint.loop_guard_state
    assert isinstance(state, dict)
    counts = state.get("call_counts")
    assert isinstance(counts, dict)
    assert all(isinstance(key, str) and len(key) == 16 for key in counts)
    return (
        tool_batches,
        decisions[0].action,
        decisions[0].reason,
        decisions[0].detail,
        dict(counts),
        state["consecutive_failures"],
    )


def _guard_agent(
    provider: Any,
    session_store: Any,
    run_store: Any,
    counts: dict[str, int],
    force_final: bool,
) -> Any:
    from linch import LoopGuard

    return _agent(
        provider=provider,
        tools=_failing_registry(counts),
        loop_guard=LoopGuard(
            max_identical_tool_calls=3,
            max_consecutive_failures=5,
            force_final_answer=force_final,
        ),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )


async def _guard_baseline(force_final: bool, session_id: str) -> tuple[Any, ...]:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    agent = _guard_agent(ThreeBatchProvider(), session_store, run_store, counts, force_final)
    session = await agent.session(id=session_id)
    events = await _collect(session.run("repeat"))
    record = await run_store.load_run(
        next(event.run_id for event in events if event.type == "system")
    )
    assert record is not None
    summary = _guard_run_summary(events, list(session.full_history), record.checkpoint)
    await agent.close()
    return summary


@pytest.mark.asyncio
@pytest.mark.parametrize("force_final", [False, True])
@pytest.mark.parametrize(
    "phase",
    [
        "assistant_appended",
        "tool_batch_pending",
        "tool_executing",
        "tool_results_appended",
        "turn_complete",
    ],
)
async def test_repeated_guard_resume_preserves_decision_at_each_tool_phase(
    phase, force_final
) -> None:
    expected = await _guard_baseline(force_final, "baseline")

    session_store = _memory_session_store()
    run_store = _CheckpointCrashStore(_memory_run_store())
    counts: dict[str, int] = {}
    run_store.reset(f"checkpoint:{phase}")
    agent = _guard_agent(ThreeBatchProvider(), session_store, run_store, counts, force_final)
    session = await agent.session(id="s1")
    run_id = ""
    with pytest.raises(_Crash):
        async for event in session.run("repeat"):
            if event.type == "system":
                run_id = event.run_id
    assert run_id
    await agent.close()

    run_store.reset(None)
    restarted = _guard_agent(
        ThreeBatchProvider(start_calls=1), session_store, run_store, counts, force_final
    )
    resumed = await restarted.session(id="s1")
    resumed_events = await _collect(resumed.resume(run_id))
    resumed_record = await run_store.load_run(run_id)
    assert resumed_record is not None
    actual = _guard_run_summary(
        resumed_events,
        list(resumed.full_history),
        resumed_record.checkpoint,
    )
    await restarted.close()

    assert actual == expected
    assert actual[0] == 3
    assert actual[5] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("force_final", [False, True])
async def test_guard_trip_after_results_checkpoint_replays_guard_action(force_final) -> None:
    """A crash after batch three's checkpoint must not lose its guard action."""
    expected = await _guard_baseline(force_final, "baseline-after-checkpoint")

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    agent = _guard_agent(ThreeBatchProvider(), session_store, run_store, counts, force_final)
    session = await agent.session(id="s1-after-checkpoint")
    run_id = ""
    tool_results_seen = 0
    with pytest.raises(_Crash):
        async for event in session.run("repeat"):
            if event.type == "system":
                run_id = event.run_id
            if event.type == "user" and event.subtype == "tool_result":
                tool_results_seen += 1
                if tool_results_seen == 3:
                    raise _Crash("after durable tool_results_appended checkpoint")
    assert run_id
    assert tool_results_seen == 3
    crashed_record = await run_store.load_run(run_id)
    assert crashed_record is not None and crashed_record.checkpoint is not None
    assert crashed_record.checkpoint.phase == "tool_results_appended"
    await agent.close()

    restarted = _guard_agent(
        ThreeBatchProvider(start_calls=3), session_store, run_store, counts, force_final
    )
    resumed = await restarted.session(id="s1-after-checkpoint")
    resumed_events = await _collect(resumed.resume(run_id))
    resumed_record = await run_store.load_run(run_id)
    assert resumed_record is not None
    actual = _guard_run_summary(
        resumed_events,
        list(resumed.full_history),
        resumed_record.checkpoint,
    )
    await restarted.close()

    assert actual == expected


async def test_crash_between_permission_checkpoint_and_start_event_reruns_tool() -> None:
    """Crash after the tool_executing checkpoint but before the start event is
    durable: the resolved permission decision was persisted by the callback, so
    resume replays it (no re-prompt) and runs the tool exactly once.

    Injects the crash by dropping the ToolCallStartEvent append — simulating a
    process death after the checkpoint commit but before the event-log write.
    """
    from linch.run_store import InMemoryRunStore

    class DropStartAppendStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.drop_starts = True

        async def append_event(self, run_id, event):
            if self.drop_starts and getattr(event, "type", None) == "tool_call_start":
                # Pretend the event log write never committed.
                return 0
            return await super().append_event(run_id, event)

    session_store = _memory_session_store()
    run_store = DropStartAppendStore()
    counts: dict[str, int] = {}
    callback_calls: list[Any] = []

    def allow_callback(request: Any) -> dict[str, str]:
        callback_calls.append(request)
        return {"behavior": "allow"}

    allow_callback.__dict__["resume_policy_id"] = "test-allow"

    agent = _agent(
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": allow_callback},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "write", lambda e: e.type == "tool_call_start")
    assert len(callback_calls) == 1
    assert counts == {}  # stopped before the tool executed

    # The start event was dropped; the checkpoint still carries the allow decision.
    run = await run_store.load_run(run_id)
    assert run is not None and run.checkpoint is not None
    assert run.checkpoint.phase == "tool_executing"
    stored = await run_store.load_events(run_id)
    assert [s for s in stored if s.event.type == "tool_call_start"] == []

    run_store.drop_starts = False  # "restart" — the event log commits normally now
    restarted = _agent(
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": allow_callback},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    resume_events = await _collect(resumed.resume(run_id))

    # No re-prompt: the resolved allow decision was durable via the callback.
    assert not any(e.type == "permission_request" for e in resume_events)
    assert len(callback_calls) == 1
    # Tool had not started (no durable start event) → it runs exactly once now.
    assert counts == {"WriteThing": 1}
    assert resume_events[-1].type == "result"


async def test_completed_tool_recovered_from_event_log_when_checkpoint_lacks_result() -> None:
    """Crash after the end event is durable but before the tool_result message is
    appended: the result is recovered from the event log (not the checkpoint) and
    the tool is not re-run."""
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    agent = _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "use tool", lambda e: e.type == "tool_call_end")
    assert counts == {"A": 1}

    run = await run_store.load_run(run_id)
    assert run is not None and run.checkpoint is not None
    # The result is durable in the event log; the checkpoint was not re-saved for
    # the end event.
    assert "call-1" not in run.checkpoint.completed_tool_results
    stored = await run_store.load_events(run_id)
    assert [s.event.tool_use_id for s in stored if s.event.type == "tool_call_end"] == ["call-1"]

    restarted = _agent(
        model="gpt-5",
        provider=ScriptProvider(fail_on_call=False),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    resumed = await restarted.session(id="s1")
    resume_events = await _collect(resumed.resume(run_id))

    # Tool result recovered from the event log — not re-run.
    assert counts == {"A": 1}
    assert resume_events[-1].type == "result"


async def test_resume_marks_checkpointed_running_background_workers_killed() -> None:
    from linch.run_store import RunCheckpoint
    from linch.types import Usage

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    run = await run_store.create_run("s1", id="run-bg")
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="turn_complete",
            prompt="continue",
            turn_index=0,
            total_usage=Usage(),
            background_workers={
                "worker-1": {
                    "worker_id": "worker-1",
                    "display_name": "Researcher",
                    "status": "running",
                }
            },
        ),
    )
    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(),
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    session = await agent.session(id="s1")

    await _pin_run_contract(session, run.id)
    events = await _collect(session.resume(run.id))

    bg_events = [event for event in events if event.type == "background_worker"]
    user_events = [event for event in events if event.type == "user"]
    assert bg_events[0].status == "killed"
    assert "worker-1" in user_events[0].message.content[0].text
    loaded = await run_store.load_run(run.id)
    assert loaded is not None
    assert loaded.checkpoint is not None
    assert loaded.checkpoint.background_workers["worker-1"]["status"] == "killed"


async def test_run_loop_aclose_at_worker_yield_finishes_invoke_agent_span() -> None:
    """Closing at the early crashed-worker yield must finish the run span.

    Regression for the yield-outside-try bug: if the yield sits before ``try``,
    ``GeneratorExit`` skips the run-span cleanup and the span leaks.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from linch import Context, RunOptions
    from linch.loop import _run_loop_impl
    from linch.run_store import RunCheckpoint
    from linch.types import Usage

    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    context = Context()
    context.register("tracer_provider", tracer_provider)
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    run = await run_store.create_run("s1", id="run-bg-close")
    await run_store.save_checkpoint(
        run.id,
        RunCheckpoint(
            phase="turn_complete",
            prompt="continue",
            turn_index=0,
            total_usage=Usage(),
            background_workers={
                "worker-1": {
                    "worker_id": "worker-1",
                    "display_name": "Researcher",
                    "status": "running",
                }
            },
        ),
    )
    agent = _agent(
        provider=ScriptProvider(),
        session_store=session_store,
        run_store=run_store,
        context=context,
    )
    session = await agent.session(id="s1")
    run_record = await run_store.load_run(run.id)
    assert run_record is not None and run_record.checkpoint is not None

    # Drive _run_loop_impl directly so aclose() hits its own yield (a direct
    # aclose runs the generator's finally synchronously; nested async-for
    # wrappers defer it to the async-gen finalizer instead).
    from linch.profiles import _RunBinding

    session._run_bindings[run_record.id] = _RunBinding(session, run_record.id, agent._run_config)
    agen = _run_loop_impl(
        session,
        run_record.checkpoint.prompt,
        RunOptions(),
        run_id=run_record.id,
        run_record=run_record,
        resume_checkpoint=run_record.checkpoint,
    )
    saw_worker = False
    async for event in agen:
        if event.type == "background_worker":
            saw_worker = True
            break
    await agen.aclose()
    await session._run_bindings[run_record.id].release()

    assert saw_worker
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans].count("invoke_agent") == 1
    tracer_provider.shutdown()


# ── End Phase 2 tests ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "has_checkpoint"),
    [
        ("completed", True),
        ("failed", True),
        ("aborted", True),
        ("running", False),
        ("waiting_permission", False),
        ("suspended", False),
    ],
)
async def test_nonresumable_run_raises_without_side_effects(
    status: str, has_checkpoint: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linch import RunNotResumableError
    from linch.run_store import RunCheckpoint
    from linch.types import Usage

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = ScriptProvider()
    agent = _agent(
        provider=provider,
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    run = await run_store.create_run("s1", id=f"rejected-{status}")
    if has_checkpoint:
        await run_store.save_checkpoint(
            run.id,
            RunCheckpoint(phase="turn_complete", prompt="hello", turn_index=0, total_usage=Usage()),
            status=status,
        )
    else:
        run_store._runs[run.id].status = status
    before = await run_store.load_run(run.id)

    async def reject_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("rejected resume must not write to the run store")

    resume_events: list[Any] = []
    with monkeypatch.context() as patch:
        for method in (
            "create_run",
            "save_checkpoint",
            "append_event",
            "append_events",
            "mark_completed",
            "mark_failed",
            "save",
            "delete",
            "prune",
        ):
            patch.setattr(run_store, method, reject_write)
        with pytest.raises(RunNotResumableError) as raised:
            async for event in session.resume(run.id):
                resume_events.append(event)

    assert raised.value.run_id == run.id
    assert raised.value.status == status
    assert run.id in str(raised.value)
    assert status in str(raised.value)
    assert resume_events == []
    assert provider.calls == 0
    assert await run_store.load_run(run.id) == before
    assert await run_store.load_events(run.id) == []
    assert await session_store.load_messages("s1") == []

    # The host explicitly abandons an active row that has no resumable checkpoint.
    if not has_checkpoint:
        await run_store.mark_failed(run.id, error={"message": "no resumable checkpoint"})
    events = await _collect(session.run("new run"))
    assert events[-1].type == "result"
    assert events[-1].subtype == "success"
    assert provider.calls == 1


async def test_unknown_run_resume_still_raises_key_error() -> None:
    provider = ScriptProvider(fail_on_call=True)
    session = await _agent(
        provider=provider,
        session_store=_memory_session_store(),
        run_store=_memory_run_store(),
    ).session(id="s1")

    with pytest.raises(KeyError, match="run not found: missing"):
        await _collect(session.resume("missing"))
    assert provider.calls == 0


async def test_postgres_session_and_run_store_resume_after_restart(
    pg_dsn,
) -> None:
    from linch.run_stores.postgres import PostgresRunStore
    from linch.sessions.postgres import PostgresSessionStore

    counts: dict[str, int] = {}

    session_store = PostgresSessionStore(pg_dsn)
    run_store = PostgresRunStore(pg_dsn)
    try:
        agent = _agent(
            provider=ScriptProvider(tool_names=["A"]),
            tools=_registry(counts, "A"),
            permissions={"mode": "skip-dangerous"},
            session_store=session_store,
            run_store=run_store,
        )
        session = await agent.session(id="s1")
        run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "assistant")
    finally:
        await session_store.close()
        await run_store.close()

    session_store = PostgresSessionStore(pg_dsn)
    run_store = PostgresRunStore(pg_dsn)
    try:
        restarted = _agent(
            provider=ScriptProvider(fail_on_call=True),
            tools=_registry(counts, "A"),
            permissions={"mode": "skip-dangerous"},
            session_store=session_store,
            run_store=run_store,
        )
        resumed = await restarted.session(id="s1")
        events = await _collect_until(
            resumed.resume(run_id),
            lambda event: event.type == "tool_call_end",
        )
    finally:
        await session_store.close()
        await run_store.close()

    assert counts == {"A": 1}
    assert [event.type for event in events] == ["tool_call_start", "tool_call_end"]


# ── Phase 3c: durable HITL approval ─────────────────────────────────────────


async def test_permission_decision_persists_and_resume_skips_callback() -> None:
    """Allow decision persists; resume replays it without re-invoking the callback.

    Scenario:
      1. Run until ToolCallStartEvent — resolve() fired (callback_calls=1) and
         the "tool_executing" checkpoint was saved with the allow decision AND an
         interrupted-before-result placeholder in completed_tool_results.
      2. Restart (fresh Agent, same stores). Resume.
      3. WriteThing is already in completed_tool_results (interrupted placeholder),
         so it is NOT in missing_tool_blocks — no re-execution, no PermissionRequestEvent,
         callback NOT called again. counts stays empty.
    """
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    callback_calls: list[Any] = []

    def allow_callback(request: Any) -> dict[str, str]:
        callback_calls.append(request)
        return {"behavior": "allow"}

    allow_callback.__dict__["resume_policy_id"] = "test-allow"

    agent = _agent(
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": allow_callback},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    # Stop just after ToolCallStartEvent: resolve() has fired and
    # the "tool_executing" checkpoint (with allow decision) is saved.
    run_id, first_events = await _run_until(session, "write", lambda e: e.type == "tool_call_start")
    assert len(callback_calls) == 1

    # Simulate restart: fresh Agent + same stores.
    restarted = _agent(
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": allow_callback},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    resume_events = await _collect(resumed.resume(run_id))
    # No permission_request should be re-emitted — tool was already in completed_tool_results.
    assert not any(e.type == "permission_request" for e in resume_events)
    # Callback was NOT called again — still 1.
    assert len(callback_calls) == 1
    # Tool did NOT re-run: the interrupted-before-result placeholder was used instead.
    # This is the correct behavior — non-idempotent tools must not re-execute on resume.
    assert counts == {}


async def test_persisted_deny_decision_replays_on_resume() -> None:
    """Explicit user-deny persists; resume re-denies the same tool without prompting.

    Scenario:
      1. Run until ToolCallStartEvent — resolve() fired → deny callback called
         and "tool_executing" checkpoint saved with deny decision.
      2. Restart. Resume.
      3. Seam A finds stored deny → no PermissionRequestEvent, callback NOT called
         again. Tool stays denied (never executes).
    """
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    deny_calls: list[Any] = []

    def deny_callback(request: Any) -> dict[str, str]:
        deny_calls.append(request)
        return {"behavior": "deny", "message": "not allowed"}

    deny_callback.__dict__["resume_policy_id"] = "test-deny"

    agent = _agent(
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": deny_callback},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    # Stop just after ToolCallStartEvent: resolve() fired → deny stored in checkpoint.
    run_id, _ = await _run_until(session, "write", lambda e: e.type == "tool_call_start")
    assert len(deny_calls) == 1
    assert counts == {}  # tool was denied in first run

    restarted = _agent(
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": deny_callback},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    resume_events = await _collect(resumed.resume(run_id))
    # No new permission_request on resume — deny was replayed from checkpoint.
    assert not any(e.type == "permission_request" for e in resume_events)
    # Callback was NOT called again — still 1.
    assert len(deny_calls) == 1
    # Tool still never executed.
    assert counts == {}


async def test_permission_decision_cleared_each_turn() -> None:
    """Allow decision from turn N must NOT replay in turn N+1 (stale-decision guard).

    Scenario:
      Turn 0: model calls WriteThing(value='WriteThing'), callback allows → 1 call.
      Turn 1: model calls WriteThing(value='WriteThing') again (same input).
              The per-turn clear must cause Seam A to fall through; callback invoked → 2 calls.
    """

    class TwoTurnProvider:
        """Returns WriteThing tool calls on turns 0 and 1; returns text on turn 2+."""

        id = "two-turn"
        _calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req):
            from linch.types import Usage

            self._calls += 1
            yield {"type": "message_start", "model": req.model}
            if self._calls <= 2:
                yield {"type": "tool_use_start", "id": f"c{self._calls}", "name": "WriteThing"}
                import json

                yield {
                    "type": "tool_use_input_delta",
                    "id": f"c{self._calls}",
                    "json_delta": json.dumps({"value": "WriteThing"}),
                }
                yield {"type": "tool_use_end", "id": f"c{self._calls}"}
                yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            else:
                yield {"type": "text_delta", "text": "done"}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    counts: dict[str, int] = {}
    callback_calls: list[Any] = []

    def allow_callback(request: Any) -> dict[str, str]:
        callback_calls.append(request)
        return {"behavior": "allow"}

    agent = _agent(
        provider=TwoTurnProvider(),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "can_use_tool": allow_callback},
        session_store=_memory_session_store(),
    )
    session = await agent.session(id="s1")
    await _collect(session.run("go"))

    # Callback must have been invoked once per turn — NOT once total due to stale replay.
    assert len(callback_calls) == 2, (
        f"expected callback_calls==2 (once per turn), got {len(callback_calls)}"
    )
    assert counts == {"WriteThing": 2}


# ── Cost-telemetry resume: running_cost must cover the WHOLE run ─────────────


async def test_resume_total_cost_reflects_whole_run_not_just_post_resume() -> None:
    """After crash+resume the final ResultEvent.total_cost_usd must cover the
    pre-crash turn too, consistent with the whole-run total_usage.

    RED before the fix: running_cost is reset to None on resume and never
    restored, so the final figure reflects only the post-resume turn(s) and is
    strictly less than cost_usd(total_usage, model).
    """
    from linch.pricing import cost_usd
    from linch.types import Usage
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn

    model = "claude-opus-4-8"  # priced model
    usage = Usage(input_tokens=1000, output_tokens=500)

    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}

    agent = _agent(
        model=model,
        provider=ScriptedProvider([ToolUseTurn("A", {"value": "A"}, usage=usage)]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    session = await agent.session(id="s1")
    # Stop after the first (priced) assistant tool-use turn — cost has accrued
    # and a checkpoint is saved, then we "crash".
    run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "assistant")

    restarted = _agent(
        model=model,
        provider=ScriptedProvider([TextTurn("done", usage=usage)]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
    )
    resumed = await restarted.session(id="s1")
    events = await _collect(resumed.resume(run_id))

    result = events[-1]
    assert result.type == "result"
    expected = cost_usd(result.total_usage, model)
    assert expected is not None and expected > 0
    assert result.total_cost_usd is not None
    # Whole-run cost must match the whole-run usage — not just post-resume turns.
    assert result.total_cost_usd == expected


async def test_contractless_resume_rejects_before_provider_or_tool_dispatch() -> None:
    import pytest

    from linch._wire import UnsupportedWireVersionError
    from linch.run_store import RunCheckpoint
    from linch.types import Usage

    store = _memory_run_store()
    counts: dict[str, int] = {}
    session = await _agent(
        provider=ScriptProvider(fail_on_call=True),
        tools=_registry(counts, "A"),
        session_store=_memory_session_store(),
        run_store=store,
    ).session(id="s1")
    await store.create_run("s1", id="contractless")
    await store.save_checkpoint(
        "contractless",
        RunCheckpoint(phase="turn_complete", prompt="go", turn_index=0, total_usage=Usage()),
    )
    with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
        await _collect(session.resume("contractless"))
    assert counts == {}
    assert await store.load_events("contractless") == []


async def test_tool_recovery_with_null_cursor_fails_before_side_effects() -> None:
    import pytest

    from linch.errors import ConfigError

    store = _memory_run_store()
    sessions = _memory_session_store()
    counts: dict[str, int] = {}
    session = await _agent(
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=sessions,
        run_store=store,
    ).session(id="s1")
    run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "assistant")
    checkpoint = store._runs[run_id].checkpoint
    assert checkpoint is not None
    checkpoint.tool_batch_event_after_seq = None
    checkpoint.background_workers = {
        "worker": {"worker_id": "worker", "display_name": "worker", "status": "running"}
    }
    resumed = await _agent(
        provider=ScriptProvider(fail_on_call=True),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=sessions,
        run_store=store,
    ).session(id="s1")
    before = await store.load_events(run_id)
    with pytest.raises(ConfigError, match="cursor"):
        await _collect(resumed.resume(run_id))
    assert counts == {}
    assert await store.load_events(run_id) == before
    record = await store.load_run(run_id)
    assert record is not None and record.checkpoint is not None
    assert record.checkpoint.tool_batch_event_after_seq is None
    assert record.checkpoint.background_workers["worker"]["status"] == "running"
