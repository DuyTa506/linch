"""End-to-end event traces for the loop-split refactor.

These are deliberately characterization tests: they exercise the public loop
entry points with a normalized fake provider, real sessions, the real tool
scheduler, and real durable checkpoints.  The exact event order and the most
useful fields are the contract the phase split must preserve.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any


class _TraceProvider:
    id = "characterization"

    def __init__(
        self,
        turns: list[tuple[str, Any]],
        *,
        usage: Any = None,
        context_window: int = 100_000,
        summary_text: str | None = None,
    ) -> None:
        self.turns = turns
        self.usage = usage
        self._context_window = context_window
        self.summary_text = summary_text
        self.calls = 0
        self.main_calls = 0
        self.summary_calls = 0
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return self._context_window

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.errors import ProviderError
        from linch.types import Usage

        self.calls += 1
        self.requests.append(req)
        if self.summary_text is not None and not req.tools:
            self.summary_calls += 1
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": self.summary_text}
            yield {
                "type": "message_end",
                "stop_reason": "end_turn",
                "usage": self.usage or Usage(input_tokens=3, output_tokens=2),
            }
            return

        index = min(self.main_calls, len(self.turns) - 1)
        kind, payload = self.turns[index]
        self.main_calls += 1
        if kind == "error":
            raise ProviderError(str(payload), retryable=False)

        yield {"type": "message_start", "model": req.model}
        if kind == "text":
            yield {"type": "text_delta", "text": str(payload)}
            stop_reason = "end_turn"
        elif kind == "tools":
            for tool_id, name, tool_input in payload:
                yield {"type": "tool_use_start", "id": tool_id, "name": name}
                import json

                yield {
                    "type": "tool_use_input_delta",
                    "id": tool_id,
                    "json_delta": json.dumps(tool_input),
                }
                yield {"type": "tool_use_end", "id": tool_id}
            stop_reason = "tool_use"
        else:
            raise AssertionError(f"unknown scripted provider turn: {kind!r}")
        yield {
            "type": "message_end",
            "stop_reason": stop_reason,
            "usage": self.usage or Usage(),
        }


class _TraceTool:
    description = "A characterization tool."
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    def __init__(
        self,
        name: str,
        *,
        scope: str = "read",
        parallel: bool = False,
        on_execute: Callable[[], Any] | None = None,
    ) -> None:
        self.name = name
        self.scope = scope
        self.parallel = parallel
        self._on_execute = on_execute
        self.calls = 0

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return f"{self.name}({input.get('value', '')})"

    def resources(self, input: dict[str, Any]) -> list[Any]:
        return []

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        from linch.tools import ToolResult

        self.calls += 1
        if self._on_execute is not None:
            result = self._on_execute()
            if hasattr(result, "__await__"):
                await result
        return ToolResult(content=f"{self.name}:{input.get('value', '')}")


def _tools(*tools: Any) -> Any:
    from linch.tools import ToolRegistry

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _agent(provider: Any, *, tools: Any = None, run_store: Any = None, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore

    kwargs.setdefault("cwd", ".")
    kwargs.setdefault("permissions", {"mode": "skip-dangerous"})
    kwargs.setdefault("session_store", InMemorySessionStore())
    kwargs.setdefault("features", FeatureFlags(skills=False, subagents=False, mcp=False))
    if tools is None:
        tools = _tools()
    if run_store is not None:
        kwargs["run_store"] = run_store
    return Agent(model="test-model", provider=provider, tools=tools, **kwargs)


async def _collect(iterator: Any) -> list[Any]:
    return [event async for event in iterator]


async def _run_direct(agent: Any, session: Any, prompt: str, opts: Any = None) -> list[Any]:
    from linch import RunOptions
    from linch.loop import run_loop

    return await _collect(run_loop(session, prompt, opts or RunOptions()))


async def _resume_direct(agent: Any, session: Any, run_id: str, opts: Any = None) -> list[Any]:
    from linch import RunOptions
    from linch.loop import resume_loop

    return await _collect(resume_loop(session, run_id, opts or RunOptions()))


async def test_text_only_trace_pins_event_types_and_terminal_fields() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.types import Usage

    provider = _TraceProvider([("text", "done")], usage=Usage(input_tokens=2, output_tokens=3))
    agent = _agent(provider, run_store=InMemoryRunStore())
    session = await agent.session(id="text-only")

    events = await _run_direct(agent, session, "hello")

    assert [event.type for event in events] == [
        "system",
        "user",
        "assistant",
        "usage",
        "result",
    ]
    system, prompt, assistant, usage, result = events
    assert system.session_id == "text-only"
    assert system.run_id
    assert system.model == "test-model"
    assert system.tools == []
    assert prompt.subtype == "prompt"
    assert assistant.stop_reason == "end_turn"
    assert assistant.message.content[0].text == "done"
    assert usage.usage == Usage(input_tokens=2, output_tokens=3)
    assert usage.cumulative == Usage(input_tokens=2, output_tokens=3)
    assert result.subtype == "success"
    assert result.stop_reason == "end_turn"
    assert result.final_text == "done"
    assert result.total_usage == Usage(input_tokens=2, output_tokens=3)


async def test_tool_then_answer_trace_pins_tool_brackets_and_continuation() -> None:
    from linch.run_store import InMemoryRunStore

    tool = _TraceTool("Echo")
    provider = _TraceProvider(
        [
            ("tools", [("call-1", "Echo", {"value": "hello"})]),
            ("text", "done"),
        ]
    )
    agent = _agent(
        provider,
        tools=_tools(tool),
        run_store=InMemoryRunStore(),
    )
    session = await agent.session(id="tool-then-answer")

    events = await _run_direct(agent, session, "use Echo")

    assert [event.type for event in events] == [
        "system",
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
    first_assistant = events[2]
    start, end = events[4:6]
    tool_result_message = events[6]
    final_assistant = events[7]
    result = events[-1]
    assert first_assistant.stop_reason == "tool_use"
    assert start.tool_use_id == "call-1"
    assert start.tool_name == "Echo"
    assert start.input == {"value": "hello"}
    assert start.summary == "Echo(hello)"
    assert end.tool_use_id == "call-1"
    assert end.tool_name == "Echo"
    assert end.result == "Echo:hello"
    assert end.is_error is False
    assert end.tool_result.content == "Echo:hello"
    assert tool_result_message.subtype == "tool_result"
    assert tool_result_message.message.content[0].tool_use_id == "call-1"
    assert final_assistant.stop_reason == "end_turn"
    assert final_assistant.message.content[0].text == "done"
    assert result.subtype == "success"
    assert result.final_text == "done"
    assert tool.calls == 1


async def test_parallel_read_trace_pins_provider_order_and_concurrent_execution() -> None:
    from linch.run_store import InMemoryRunStore

    active = [0, 0]

    async def overlap() -> None:
        active[0] += 1
        active[1] = max(active[1], active[0])
        await asyncio.sleep(0)
        active[0] -= 1

    read_a = _TraceTool("ReadA", parallel=True, on_execute=overlap)
    read_b = _TraceTool("ReadB", parallel=True, on_execute=overlap)
    provider = _TraceProvider(
        [
            (
                "tools",
                [
                    ("call-a", "ReadA", {"value": "a"}),
                    ("call-b", "ReadB", {"value": "b"}),
                ],
            ),
            ("text", "done"),
        ]
    )
    agent = _agent(
        provider,
        tools=_tools(read_a, read_b),
        run_store=InMemoryRunStore(),
    )
    session = await agent.session(id="parallel-read")

    events = await _run_direct(agent, session, "read both")

    assert [event.type for event in events] == [
        "system",
        "user",
        "assistant",
        "usage",
        "tool_call_start",
        "tool_call_start",
        "tool_call_end",
        "tool_call_end",
        "user",
        "assistant",
        "usage",
        "result",
    ]
    starts = events[4:6]
    ends = events[6:8]
    assert [(event.tool_use_id, event.tool_name) for event in starts] == [
        ("call-a", "ReadA"),
        ("call-b", "ReadB"),
    ]
    assert [(event.tool_use_id, event.tool_name) for event in ends] == [
        ("call-a", "ReadA"),
        ("call-b", "ReadB"),
    ]
    assert [event.result for event in ends] == ["ReadA:a", "ReadB:b"]
    assert active[1] == 2
    assert read_a.calls == 1 and read_b.calls == 1


async def test_permission_pause_then_resume_trace_uses_resume_loop() -> None:
    from linch import RunOptions
    from linch.loop import resume_loop, run_loop
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    tool = _TraceTool("WriteThing", scope="write")

    def allow(_request: Any) -> dict[str, str]:
        return {"behavior": "allow"}

    allow.__dict__["resume_policy_id"] = "characterization-allow"
    provider = _TraceProvider(
        [
            ("tools", [("call-write", "WriteThing", {"value": "x"})]),
            ("text", "done"),
        ]
    )
    agent = _agent(
        provider,
        tools=_tools(tool),
        run_store=run_store,
        session_store=session_store,
        permissions={"mode": "default", "canUseTool": allow},
    )
    session = await agent.session(id="permission")

    first_iterator = run_loop(session, "write", RunOptions())
    first_events: list[Any] = []
    async for event in first_iterator:
        first_events.append(event)
        if event.type == "permission_request":
            break
    await first_iterator.aclose()

    assert [event.type for event in first_events] == [
        "system",
        "user",
        "assistant",
        "usage",
        "permission_request",
    ]
    permission = first_events[-1]
    assert permission.requests[0].tool_use_id == "call-write"
    assert permission.requests[0].tool_name == "WriteThing"
    assert permission.requests[0].input == {"value": "x"}
    assert tool.calls == 0
    run_id = first_events[0].run_id

    resumed_agent = _agent(
        _TraceProvider([("text", "done")]),
        tools=_tools(_TraceTool("WriteThing", scope="write")),
        run_store=run_store,
        session_store=session_store,
        permissions={"mode": "default", "canUseTool": allow},
    )
    resumed_session = await resumed_agent.session(id="permission")
    resumed_events = await _collect(resume_loop(resumed_session, run_id, RunOptions()))

    assert [event.type for event in resumed_events] == [
        "permission_request",
        "tool_call_start",
        "tool_call_end",
        "user",
        "assistant",
        "usage",
        "result",
    ]
    assert resumed_events[0].requests[0].tool_use_id == "call-write"
    assert resumed_events[1].input == {"value": "x"}
    assert resumed_events[2].result == "WriteThing:x"
    assert resumed_events[3].subtype == "tool_result"
    assert resumed_events[-1].subtype == "success"


async def test_structured_output_retry_trace_pins_verification_feedback() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.types import OutputSchema, Usage

    provider = _TraceProvider(
        [("text", "not json"), ("text", '{"answer": 42}')],
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    agent = _agent(
        provider,
        run_store=InMemoryRunStore(),
        output_schema=OutputSchema(
            name="answer",
            schema={
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
            },
        ),
        structured_output_retries=1,
    )
    session = await agent.session(id="structured-retry")

    events = await _run_direct(agent, session, "return JSON")

    assert [event.type for event in events] == [
        "system",
        "user",
        "assistant",
        "usage",
        "verification",
        "user",
        "assistant",
        "usage",
        "result",
    ]
    verification = events[4]
    feedback = events[5]
    result = events[-1]
    assert verification.verifier == "output_schema"
    assert verification.action == "retry"
    assert verification.attempt == 1
    assert "failed structured-output validation" in feedback.message.content[0].text
    assert result.subtype == "success"
    assert result.final_text == '{"answer": 42}'
    assert result.structured_output == {"answer": 42}
    assert result.structured_error is None


async def test_budget_stop_trace_pins_warning_and_exhaustion_tail() -> None:
    from linch import RunBudget, RunOptions
    from linch.run_store import InMemoryRunStore
    from linch.types import Usage

    tool = _TraceTool("Read")
    provider = _TraceProvider(
        [
            ("tools", [("call-1", "Read", {"value": "one"})]),
            ("tools", [("call-2", "Read", {"value": "two"})]),
        ],
        usage=Usage(input_tokens=600),
    )
    agent = _agent(provider, tools=_tools(tool), run_store=InMemoryRunStore())
    session = await agent.session(id="budget")

    events = await _run_direct(
        agent,
        session,
        "keep working",
        RunOptions(budget=RunBudget(max_tokens=1_000)),
    )

    assert [event.type for event in events] == [
        "system",
        "user",
        "assistant",
        "usage",
        "tool_call_start",
        "tool_call_end",
        "user",
        "assistant",
        "usage",
        "budget",
        "tool_call_start",
        "tool_call_end",
        "user",
        "budget",
        "error",
        "result",
    ]
    assert events[9].kind == "warning"
    assert events[9].spent_tokens == 1_200
    assert events[13].kind == "exceeded"
    assert events[13].max_tokens == 1_000
    assert events[14].error["name"] == "BudgetExceededError"
    assert events[-1].subtype == "error"
    assert events[-1].total_usage == Usage(input_tokens=1_200)
    assert provider.main_calls == 2


async def test_abort_trace_pins_tool_result_before_aborted_terminal() -> None:
    from linch.run_store import InMemoryRunStore

    session_ref: list[Any] = []

    def abort_session() -> None:
        session_ref[0].abort()

    tool = _TraceTool("Abort", on_execute=abort_session)
    provider = _TraceProvider(
        [
            ("tools", [("call-abort", "Abort", {"value": "now"})]),
            ("text", "should not be requested"),
        ]
    )
    agent = _agent(provider, tools=_tools(tool), run_store=InMemoryRunStore())
    session = await agent.session(id="abort")
    session_ref.append(session)

    events = await _run_direct(agent, session, "abort after tool")

    assert [event.type for event in events] == [
        "system",
        "user",
        "assistant",
        "usage",
        "tool_call_start",
        "tool_call_end",
        "user",
        "result",
    ]
    assert events[5].is_error is False
    assert events[6].subtype == "tool_result"
    assert events[-1].subtype == "aborted"
    assert events[-1].stop_reason == "error"
    assert provider.main_calls == 1


async def test_provider_error_trace_pins_error_and_result_fields() -> None:
    from linch.run_store import InMemoryRunStore

    provider = _TraceProvider([("error", "provider exploded")])
    agent = _agent(provider, run_store=InMemoryRunStore())
    session = await agent.session(id="provider-error")

    events = await _run_direct(agent, session, "hello")

    assert [event.type for event in events] == ["system", "user", "error", "result"]
    error, result = events[2:]
    assert error.error == {
        "name": "ProviderError",
        "message": "provider exploded",
        "retryable": False,
    }
    assert result.subtype == "error"
    assert result.stop_reason == "error"
    assert result.usage_complete is False
    assert provider.main_calls == 1


async def test_compaction_trace_pins_auxiliary_usage_and_projection_fields() -> None:
    from linch import DetailedCompaction
    from linch.run_store import InMemoryRunStore
    from linch.types import Message, TextBlock, Usage

    provider = _TraceProvider(
        [("text", "done")],
        usage=Usage(input_tokens=3, output_tokens=2),
        context_window=100,
        summary_text="summary of earlier work",
    )
    agent = _agent(
        provider,
        tools=_tools(_TraceTool("Echo")),
        run_store=InMemoryRunStore(),
        compaction=DetailedCompaction(keep_recent_turns=1),
        max_output_tokens=1,
        token_estimator=lambda messages, model: len(messages) * 30,
    )
    session = await agent.session(id="compaction")
    await session.append(
        [
            Message(role="user", content=[TextBlock(text="old request")]),
            Message(role="assistant", content=[TextBlock(text="old answer")]),
            Message(role="user", content=[TextBlock(text="second request")]),
            Message(role="assistant", content=[TextBlock(text="second answer")]),
        ]
    )

    events = await _run_direct(agent, session, "new request")

    assert [event.type for event in events] == [
        "system",
        "user",
        "usage",
        "compaction",
        "assistant",
        "usage",
        "result",
    ]
    auxiliary_usage = events[2]
    compaction = events[3]
    result = events[-1]
    assert auxiliary_usage.source == "compaction"
    assert auxiliary_usage.model == "test-model"
    assert auxiliary_usage.usage == Usage(input_tokens=3, output_tokens=2)
    assert compaction.strategy == "detailed-continuation-keep-recent-10"
    assert compaction.messages_before == 5
    assert compaction.messages_after == 3
    assert compaction.tokens_before == 150
    assert compaction.tokens_after == 90
    assert result.subtype == "success"
    assert result.final_text == "done"
    assert result.total_usage == Usage(input_tokens=6, output_tokens=4)
    assert provider.summary_calls == 1
    assert provider.main_calls == 1
