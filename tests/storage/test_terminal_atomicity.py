"""A process loss at a terminal boundary must not split the reconnect log."""

from __future__ import annotations

from typing import Any

import pytest


class _ProcessDeath(BaseException):
    pass


class _CrashWrites:
    """Keep dying through finally, just as a process cannot perform cleanup writes."""

    def __init__(self, store: Any, point: str = "", after: bool = False) -> None:
        self.store, self.point, self.after = store, point, after
        self.dead = False
        self.armed = False
        self.turn_complete = 0
        self.terminals = 0
        self.peer: _CrashWrites | None = None

    def __getattr__(self, name: str) -> Any:
        if name in {"save", "delete", "prune", "save_provider_snapshot", "update_meta"}:

            async def write(*args: Any, **kw: Any) -> Any:
                return await self._write(name, *args, **kw)

            return write
        return getattr(self.store, name)

    async def _write(self, name: str, *args: Any, **kw: Any) -> Any:
        if self.dead or (self.peer is not None and self.peer.dead):
            raise _ProcessDeath("cleanup after process death")
        checkpoint = args[1] if name.startswith("save_checkpoint") else None
        point = name
        if checkpoint is not None and checkpoint.phase == "turn_complete" and self.armed:
            self.turn_complete += 1
            point = f"turn_complete:{self.turn_complete}"
        if name == "append_messages" and any(
            "Please provide your final answer now" in getattr(block, "text", "")
            for message in args[1]
            for block in message.content
        ):
            point = "reminder"
        hit = self.armed and point == self.point
        if hit and not self.after:
            self.dead = True
            raise _ProcessDeath(point)
        result = await getattr(self.store, name)(*args, **kw)
        status = kw.get("status")
        if name in {"mark_failed", "mark_completed"} or status in {"completed", "aborted"}:
            self.terminals += 1
        if (
            checkpoint is not None
            and checkpoint.phase == "tool_results_appended"
            and checkpoint.turn_index == 1
        ):
            self.armed = True
        if hit:
            self.dead = True
            raise _ProcessDeath(point)
        return result

    async def save_checkpoint(self, *args: Any, **kw: Any) -> Any:
        return await self._write("save_checkpoint", *args, **kw)

    async def save_checkpoint_with_events(self, *args: Any, **kw: Any) -> Any:
        return await self._write("save_checkpoint_with_events", *args, **kw)

    async def append_events(self, *args: Any, **kw: Any) -> Any:
        return await self._write("append_events", *args, **kw)

    async def append_event(self, *args: Any, **kw: Any) -> Any:
        return await self._write("append_event", *args, **kw)

    async def mark_failed(self, *args: Any, **kw: Any) -> Any:
        return await self._write("mark_failed", *args, **kw)

    async def mark_completed(self, *args: Any, **kw: Any) -> Any:
        return await self._write("mark_completed", *args, **kw)

    async def append_messages(self, *args: Any, **kw: Any) -> Any:
        return await self._write("append_messages", *args, **kw)


class _RepeatingProvider:
    id = "terminal-crash-provider"

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, request: Any) -> Any:
        from linch.types import Usage

        yield {"type": "message_start", "model": request.model}
        if request.tools:
            # A fresh provider after restart produces the same deterministic tool IDs.
            index = sum(message.role == "assistant" for message in request.messages)
            yield {"type": "tool_use_start", "id": f"call-{index}", "name": "Repeat"}
            yield {"type": "tool_use_input_delta", "id": f"call-{index}", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": f"call-{index}"}
        else:
            yield {"type": "text_delta", "text": "final answer"}
        yield {
            "type": "message_end",
            "stop_reason": "tool_use" if request.tools else "end_turn",
            "usage": Usage(),
        }


def _agent(sessions: Any, runs: Any, force_final: bool) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.tools import ToolResult
    from linch.tools.registry import empty_tools

    class Repeat:
        name = "Repeat"
        description = "repeat"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True
        tags: tuple[str, ...] = ()

        def validate(self, raw: Any) -> Any:
            return raw

        def summarize(self, raw: Any) -> str:
            return "repeat"

        def resources(self, raw: Any) -> list[Any]:
            return []

        async def execute(self, raw: Any, ctx: Any) -> Any:
            return ToolResult(content="same result")

    return Agent(
        model="test-model",
        provider=_RepeatingProvider(),
        tools=empty_tools(Repeat()),
        session_store=sessions,
        run_store=runs,
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        loop_guard={"max_identical_tool_calls": 2, "force_final_answer": force_final},
    )


def _normalized(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<nondeterministic>"
            if item is not None
            and key
            in {
                "run_id",
                "session_id",
                "model_call_id",
                "duration_ms",
                "elapsed_ms",
                "started_at",
                "completed_at",
                "timestamp",
                "logical_call_id",
                "parent_logical_call_id",
                "delivery_attempt_id",
                "budget_owner_id",
                "projection_id",
            }
            else _normalized(item)
            for key, item in value.items()
        }
    return value


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("after", [False, True], ids=["before", "after_commit"])
@pytest.mark.parametrize(
    ("force_final", "point"),
    [
        (False, "mark_failed"),
        (True, "reminder"),
        (True, "turn_complete:1"),
        (True, "turn_complete:2"),
    ],
)
async def test_guard_terminal_crash_preserves_exact_log(pg_dsn, kind, after, force_final, point):
    from linch import RunNotResumableError
    from linch.events import PromptCacheAdvisoryEvent, event_to_dict
    from linch.run_store import InMemoryRunStore
    from linch.run_stores.postgres import PostgresRunStore
    from linch.sessions import InMemorySessionStore
    from linch.sessions.postgres import PostgresSessionStore

    def stores():
        if kind == "memory":
            return InMemorySessionStore(), InMemoryRunStore()
        return PostgresSessionStore(pg_dsn), PostgresRunStore(pg_dsn)

    async def scenario(crash: bool):
        sessions, runs = stores()
        run_writes = _CrashWrites(runs, point if crash else "", after)
        session_writes = _CrashWrites(sessions, point if crash else "", after)
        # Session reminder write follows the run-store boundary.
        session_writes.armed = True
        session_writes.peer, run_writes.peer = run_writes, session_writes
        agent = _agent(session_writes, run_writes, force_final)
        session = await agent.session()
        run_id = ""
        try:
            try:
                async for event in session.run("repeat"):
                    if event.type == "system":
                        run_id = event.run_id
            except _ProcessDeath:
                assert crash
            else:
                assert not crash, f"crash point {point} was never reached"
            if crash:
                assert run_writes.dead or session_writes.dead
                # Keep both wrappers dead during unwinding, then use fresh agent/store handles.
                if kind == "postgres":
                    await sessions.close()
                    await runs.close()
                    sessions, runs = stores()
                resumed = _CrashWrites(runs)
                resumed_agent = _agent(sessions, resumed, force_final)
                session = await resumed_agent.session(id=session.id)
                record = await runs.load_run(run_id)
                if record.status in {"completed", "failed", "aborted"}:
                    with pytest.raises(RunNotResumableError):
                        _ = [event async for event in session.resume(run_id)]
                else:
                    _ = [event async for event in session.resume(run_id)]
                assert run_writes.terminals + resumed.terminals == 1
            else:
                assert run_writes.terminals == 1
            rows = await runs.load_events(run_id)
            assert [row.seq for row in rows] == list(range(1, len(rows) + 1))
            assert (await runs.load_run(run_id)).status == (
                "completed" if force_final else "failed"
            )
            # Advisories compare process-local requests; a restarted session compares afresh.
            return [
                _normalized(event_to_dict(row.event))
                for row in rows
                if not isinstance(row.event, PromptCacheAdvisoryEvent)
            ]
        finally:
            await sessions.close()
            await runs.close()

    assert await scenario(True) == await scenario(False)


async def test_cancelled_reminder_append_cannot_flush_guard_without_checkpoint():
    import asyncio

    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class CancelReminder(InMemorySessionStore):
        cancelled = False

        async def append_messages(self, session_id, messages):
            if not self.cancelled and any(
                "Please provide your final answer now" in getattr(block, "text", "")
                for message in messages
                for block in message.content
            ):
                self.cancelled = True
                raise asyncio.CancelledError()
            return await super().append_messages(session_id, messages)

    sessions, runs = CancelReminder(), InMemoryRunStore()
    session = await _agent(sessions, runs, True).session()
    run_id = ""
    with pytest.raises(asyncio.CancelledError):
        async for event in session.run("repeat"):
            if event.type == "system":
                run_id = event.run_id
    record = await runs.load_run(run_id)
    assert record.checkpoint.phase == "tool_results_appended"
    rows = await runs.load_events(run_id)
    assert sum(row.event.type == "loop_guard" for row in rows) == 0
    assert sum(row.event.type == "user" and row.event.subtype == "tool_result" for row in rows) == 1
