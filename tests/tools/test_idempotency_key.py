"""ToolContext.idempotency_key (ROADMAP Phase 3.4).

Durable tool execution is at-least-once: a side effect that runs before its
completion record is durable re-runs on resume. Linch supplies a stable
``idempotency_key`` derived from the run ID and the tool-use ID so a mutating
integration can deduplicate the interrupted intent. These tests pin the
derivation, its run/call scoping, and — the crux — that the key a tool sees on a
resumed re-execution matches the original run's identity, not a fresh one.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


class ScriptProvider:
    id = "script"

    def __init__(self, tool_names: list[str]) -> None:
        self.tool_names = list(tool_names)

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        if _last_is_tool_result(req.messages) or not self.tool_names:
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
            return
        for idx, name in enumerate(self.tool_names, start=1):
            tool_id = f"call-{idx}"
            yield {"type": "tool_use_start", "id": tool_id, "name": name}
            yield {
                "type": "tool_use_input_delta",
                "id": tool_id,
                "json_delta": json.dumps({}),
            }
            yield {"type": "tool_use_end", "id": tool_id}
        yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}


def _last_is_tool_result(messages: list[Any]) -> bool:
    if not messages:
        return False
    return any(getattr(block, "type", None) == "tool_result" for block in messages[-1].content)


class RecordingTool:
    description = "Records the idempotency key seen at execution."
    input_schema = {"type": "object", "properties": {}}
    parallel = False

    def __init__(self, name: str, seen: list[str]) -> None:
        self.name = name
        self.seen = seen
        self.scope: Any = "read"

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: Any):
        from linch.tools import ToolResult

        self.seen.append(ctx.idempotency_key)
        return ToolResult(content=self.name)


def _registry(*tools: Any):
    from linch.tools import ToolRegistry

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _agent(provider: Any, tools: Any, run_store: Any = None, **kwargs: Any):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="gpt-5",
        provider=provider,
        tools=tools,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        run_store=run_store if run_store is not None else InMemoryRunStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        cwd=".",
        **kwargs,
    )


async def _run(session: Any, prompt: str = "go") -> tuple[str, list[Any]]:
    run_id = ""
    events: list[Any] = []
    async for event in session.run(prompt):
        events.append(event)
        if event.type == "system":
            run_id = event.run_id
    return run_id, events


async def test_idempotency_key_is_run_id_and_tool_use_id() -> None:
    seen: list[str] = []
    agent = _agent(ScriptProvider(["Do"]), _registry(RecordingTool("Do", seen)))
    session = await agent.session(id="s1")

    run_id, events = await _run(session)
    starts = [e for e in events if e.type == "tool_call_start"]

    assert len(seen) == 1
    assert len(starts) == 1
    assert seen[0] == f"{run_id}:{starts[0].tool_use_id}"


async def test_idempotency_key_is_run_scoped_and_call_scoped() -> None:
    seen: list[str] = []
    agent = _agent(
        ScriptProvider(["A", "B"]),
        _registry(RecordingTool("A", seen), RecordingTool("B", seen)),
    )

    run1, _ = await _run(await agent.session(id="s1"))
    keys_run1 = list(seen)
    seen.clear()
    run2, _ = await _run(await agent.session(id="s2"))
    keys_run2 = list(seen)

    # Two distinct tool calls in one run → two distinct keys (call-scoped).
    assert keys_run1 == [f"{run1}:call-1", f"{run1}:call-2"]
    # A different run → different keys (run-scoped); no key is shared across runs.
    assert keys_run2 == [f"{run2}:call-1", f"{run2}:call-2"]
    assert set(keys_run1).isdisjoint(keys_run2)


async def test_idempotency_key_stable_across_resume() -> None:
    """Crash after the ``tool_executing`` checkpoint but before the start event is
    durable, then resume: the re-executed tool must see a key built from the
    *original* run's identity (run_id + tool_use_id), not a fresh run."""
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    class DropStartStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.drop_starts = True

        async def append_event(self, run_id: str, event: Any) -> int:
            if self.drop_starts and getattr(event, "type", None) == "tool_call_start":
                return 0
            return await super().append_event(run_id, event)

    session_store = InMemorySessionStore()
    run_store = DropStartStore()
    seen: list[str] = []

    def build_agent() -> Any:
        from linch import Agent
        from linch.config import FeatureFlags

        return Agent(
            model="gpt-5",
            provider=ScriptProvider(["Do"]),
            tools=_registry(RecordingTool("Do", seen)),
            permissions={"mode": "skip-dangerous"},
            session_store=session_store,
            run_store=run_store,
            features=FeatureFlags(skills=False, subagents=False, mcp=False),
            result_offload=None,
            cwd=".",
        )

    # Run until the tool starts, then "crash" (stop consuming) before it executes.
    agent = build_agent()
    session = await agent.session(id="s1")
    run_id = ""
    async for event in session.run("go"):
        if event.type == "system":
            run_id = event.run_id
        if event.type == "tool_call_start":
            break
    assert run_id
    assert seen == []  # stopped before the tool ran

    # Restart the process: a fresh agent resumes the same run_id.
    run_store.drop_starts = False
    resumed = await build_agent().session(id="s1")
    resume_events = [e async for e in resumed.resume(run_id)]

    assert resume_events[-1].type == "result"
    # The tool ran exactly once, on resume, keyed to the ORIGINAL run identity.
    assert seen == [f"{run_id}:call-1"]
