"""Golden characterization of the loop's externally-observable order.

These tests pin two orderings that no other single test captures end to end:

1. the full ordered sequence of event ``type`` values a caller receives, and
2. the full ordered sequence of checkpoint ``phase`` values persisted to the
   run store,

for the two representative loop shapes — a text-only turn (terminal) and a
single tool-use turn (continuation). They are the safety net the roadmap
requires *before* any structural refactor of ``_run_loop_impl``: a split that
preserves behavior leaves these traces byte-identical, so an accidental
reordering, a dropped event, or an extra checkpoint save trips exactly here.

If you change loop-visible behavior on purpose, update the golden lists in the
same commit and say why — that is the intended tripwire, not a flake.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from linch.run_store import InMemoryRunStore


class _ScriptProvider:
    id = "script"

    def __init__(self, *, tool_names: list[str] | None = None) -> None:
        self.tool_names = list(tool_names or [])

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        last_is_tool_result = bool(req.messages) and any(
            getattr(block, "type", None) == "tool_result" for block in req.messages[-1].content
        )
        if last_is_tool_result or not self.tool_names:
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


class _EchoTool:
    description = "Echoes its name."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    parallel = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.scope: Any = "read"

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: Any):
        from linch.tools import ToolResult

        return ToolResult(content=self.name)


class _PhaseRecordingRunStore(InMemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved_phases: list[str] = []

    async def save_checkpoint(self, run_id, checkpoint, *, status="running"):
        self.saved_phases.append(checkpoint.phase)
        return await super().save_checkpoint(run_id, checkpoint, status=status)


def _agent(**kwargs):
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    kwargs.setdefault("session_store", InMemorySessionStore())
    kwargs.setdefault("cwd", ".")
    return Agent(**kwargs)


def _registry(*names: str):
    from linch.tools import ToolRegistry

    registry = ToolRegistry()
    for name in names:
        registry.register(_EchoTool(name))
    return registry


async def _collect(iterator) -> list[Any]:
    return [event async for event in iterator]


async def test_text_only_run_pins_event_and_checkpoint_order() -> None:
    run_store = _PhaseRecordingRunStore()
    agent = _agent(model="gpt-5", provider=_ScriptProvider(), run_store=run_store)
    session = await agent.session(id="s1")

    events = await _collect(session.run("hello"))

    assert [event.type for event in events] == [
        "system",
        "skills_loaded",
        "user",
        "assistant",
        "usage",
        "result",
    ]
    assert run_store.saved_phases == [
        "started",
        "user_appended",
        "provider_pending",
        "assistant_appended",
        "completed",
    ]


async def test_single_tool_run_pins_event_and_checkpoint_order() -> None:
    run_store = _PhaseRecordingRunStore()
    agent = _agent(
        model="gpt-5",
        provider=_ScriptProvider(tool_names=["A"]),
        tools=_registry("A"),
        permissions={"mode": "skip-dangerous"},
        run_store=run_store,
    )
    session = await agent.session(id="s1")

    events = await _collect(session.run("use tool"))

    assert [event.type for event in events] == [
        "system",
        "skills_loaded",
        "user",
        "assistant",
        "usage",
        "tool_call_start",
        "tool_call_end",
        "user",
        "assistant",
        "usage",
        "result",
    ]
    assert run_store.saved_phases == [
        "started",
        "user_appended",
        "provider_pending",
        "assistant_appended",
        "tool_batch_pending",
        "tool_executing",
        "tool_results_appended",
        "turn_complete",
        "provider_pending",
        "assistant_appended",
        "completed",
    ]
