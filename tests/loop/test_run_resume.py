from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


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


class CountingTool:
    description = "Counts executions."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    parallel = False

    def __init__(self, name: str, counts: dict[str, int], *, scope: Any = "read") -> None:
        self.name = name
        self.counts = counts
        self.scope: Any = scope

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: Any):
        from linch.tools import ToolResult

        self.counts[self.name] = self.counts.get(self.name, 0) + 1
        return ToolResult(content=f"{self.name}:{self.counts[self.name]}")


def _agent(**kwargs):
    from linch import Agent

    return Agent(**kwargs)


def _memory_session_store():
    from linch.sessions import InMemorySessionStore

    return InMemorySessionStore()


def _memory_run_store():
    from linch.run_store import InMemoryRunStore

    return InMemoryRunStore()


def _registry(counts: dict[str, int], *names: str, scope: str = "read"):
    from linch.tools import ToolRegistry

    registry = ToolRegistry()
    for name in names:
        registry.register(CountingTool(name, counts, scope=scope))
    return registry


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
        model="gpt-5",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "hello", lambda event: event.type == "user")

    restarted = _agent(
        model="gpt-5",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        cwd=".",
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
        model="gpt-5",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        include_partial_messages=True,
        cwd=".",
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(
        session,
        "hello",
        lambda event: event.type == "partial_assistant",
    )

    restarted = _agent(
        model="gpt-5",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        include_partial_messages=True,
        cwd=".",
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
        model="gpt-5",
        provider=ScriptProvider(tool_names=["A"]),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "assistant")

    restarted = _agent(
        model="gpt-5",
        provider=ScriptProvider(fail_on_call=True),
        tools=_registry(counts, "A"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    resumed = await restarted.session(id="s1")
    events = await _collect_until(
        resumed.resume(run_id),
        lambda event: event.type == "tool_call_end",
    )

    assert counts == {"A": 1}
    assert [event.type for event in events] == ["tool_call_start", "tool_call_end"]


async def test_resume_after_one_tool_completed_runs_only_missing_tool() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["A", "B"]),
        tools=_registry(counts, "A", "B"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    session = await agent.session(id="s1")
    run_id, _ = await _run_until(
        session,
        "use tools",
        lambda event: event.type == "tool_call_end" and event.tool_name == "A",
    )

    restarted = _agent(
        model="gpt-5",
        provider=ScriptProvider(fail_on_call=True),
        tools=_registry(counts, "A", "B"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    resumed = await restarted.session(id="s1")
    events = await _collect_until(
        resumed.resume(run_id),
        lambda event: event.type == "tool_call_end",
    )

    assert counts == {"A": 1, "B": 1}
    assert [event.tool_name for event in events if event.type == "tool_call_end"] == ["B"]


async def test_tool_batch_checkpoint_is_not_saved_after_each_tool_end() -> None:
    from linch.run_store import InMemoryRunStore

    class CountingRunStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.saved_phases: list[str] = []

        async def save_checkpoint(self, run_id, checkpoint, *, status="running"):
            self.saved_phases.append(checkpoint.phase)
            return await super().save_checkpoint(run_id, checkpoint, status=status)

    session_store = _memory_session_store()
    run_store = CountingRunStore()
    counts: dict[str, int] = {}
    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["A", "B"]),
        tools=_registry(counts, "A", "B"),
        permissions={"mode": "skip-dangerous"},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    session = await agent.session(id="s1")

    events = await _collect(session.run("use tools"))

    assert events[-1].type == "result"
    assert counts == {"A": 1, "B": 1}
    assert run_store.saved_phases.count("tool_batch_pending") == 1


async def test_permission_pending_resume_reemits_before_tool_execution() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}

    def allow(_request) -> dict[str, str]:
        return {"behavior": "allow"}

    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "canUseTool": allow},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
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
        permissions={"mode": "default", "canUseTool": allow},
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
        model="gpt-5",
        provider=ScriptProvider(),
        session_store=session_store,
        cwd=".",
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
        model="gpt-5",
        provider=CapturingProvider(),
        session_store=session_store,
        cwd=".",
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
        cwd=".",
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


# ── End Phase 2 tests ────────────────────────────────────────────────────────


async def test_completed_run_resume_is_noop() -> None:
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    provider = ScriptProvider()
    agent = _agent(
        model="gpt-5",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    session = await agent.session(id="s1")
    events = await _collect(session.run("hello"))
    run_id = next(event.run_id for event in events if event.type == "system")

    restarted = _agent(
        model="gpt-5",
        provider=provider,
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    resumed = await restarted.session(id="s1")
    resume_events = await _collect(resumed.resume(run_id))

    assert resume_events == []
    assert provider.calls == 1


async def test_sqlite_session_and_run_store_resume_after_restart(tmp_path) -> None:
    from linch.run_store import SqliteRunStore
    from linch.sessions import SqliteSessionStore

    session_path = tmp_path / "sessions.db"
    run_path = tmp_path / "runs.db"
    counts: dict[str, int] = {}

    session_store = SqliteSessionStore(session_path)
    run_store = SqliteRunStore(run_path)
    try:
        agent = _agent(
            model="gpt-5",
            provider=ScriptProvider(tool_names=["A"]),
            tools=_registry(counts, "A"),
            permissions={"mode": "skip-dangerous"},
            session_store=session_store,
            run_store=run_store,
            cwd=".",
        )
        session = await agent.session(id="s1")
        run_id, _ = await _run_until(session, "use tool", lambda event: event.type == "assistant")
    finally:
        await session_store.close()
        await run_store.close()

    session_store = SqliteSessionStore(session_path)
    run_store = SqliteRunStore(run_path)
    try:
        restarted = _agent(
            model="gpt-5",
            provider=ScriptProvider(fail_on_call=True),
            tools=_registry(counts, "A"),
            permissions={"mode": "skip-dangerous"},
            session_store=session_store,
            run_store=run_store,
            cwd=".",
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
         the "tool_executing" checkpoint was saved with the allow decision.
      2. Restart (fresh Agent, same stores). Resume.
      3. Seam A finds stored allow → no PermissionRequestEvent, callback NOT called
         again. Tool executes once on resume (counts["WriteThing"]=1).
    """
    session_store = _memory_session_store()
    run_store = _memory_run_store()
    counts: dict[str, int] = {}
    callback_calls: list[Any] = []

    def allow_callback(request: Any) -> dict[str, str]:
        callback_calls.append(request)
        return {"behavior": "allow"}

    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "canUseTool": allow_callback},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    session = await agent.session(id="s1")
    # Stop just after ToolCallStartEvent: resolve() has fired and
    # the "tool_executing" checkpoint (with allow decision) is saved.
    run_id, first_events = await _run_until(session, "write", lambda e: e.type == "tool_call_start")
    assert len(callback_calls) == 1

    # Simulate restart: fresh Agent + same stores.
    restarted = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "canUseTool": allow_callback},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    resumed = await restarted.session(id="s1")
    resume_events = await _collect(resumed.resume(run_id))
    # No permission_request should be re-emitted — decision was replayed from checkpoint.
    assert not any(e.type == "permission_request" for e in resume_events)
    # Callback was NOT called again — still 1.
    assert len(callback_calls) == 1
    # Tool executed once on resume (not on first run — we stopped before completion).
    assert counts == {"WriteThing": 1}


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

    agent = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "canUseTool": deny_callback},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
    )
    session = await agent.session(id="s1")
    # Stop just after ToolCallStartEvent: resolve() fired → deny stored in checkpoint.
    run_id, _ = await _run_until(session, "write", lambda e: e.type == "tool_call_start")
    assert len(deny_calls) == 1
    assert counts == {}  # tool was denied in first run

    restarted = _agent(
        model="gpt-5",
        provider=ScriptProvider(tool_names=["WriteThing"]),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "canUseTool": deny_callback},
        session_store=session_store,
        run_store=run_store,
        cwd=".",
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
        model="gpt-5",
        provider=TwoTurnProvider(),
        tools=_registry(counts, "WriteThing", scope="write"),
        permissions={"mode": "default", "canUseTool": allow_callback},
        session_store=_memory_session_store(),
        cwd=".",
    )
    session = await agent.session(id="s1")
    await _collect(session.run("go"))

    # Callback must have been invoked once per turn — NOT once total due to stale replay.
    assert len(callback_calls) == 2, (
        f"expected callback_calls==2 (once per turn), got {len(callback_calls)}"
    )
    assert counts == {"WriteThing": 2}
