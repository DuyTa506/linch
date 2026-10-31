from __future__ import annotations

import pytest

import linch
from linch.events import event_from_dict, event_to_dict
from linch.loop.reliability import ReliabilityRuntime
from linch.loop.turn_stopping import TurnStoppingDecision, TurnStoppingDispatcher


def test_default_policy_and_opt_out_are_distinct() -> None:
    assert linch.ReliabilityPolicy().max_correction_turns == 2
    assert linch.ReliabilityPolicy().retry_invalid_responses is True
    assert linch.Agent(model="test", reliability=None).reliability is None


async def test_prerequisite_hook_registration_and_run_disposal() -> None:
    from types import SimpleNamespace

    policy = linch.ReliabilityPolicy(prerequisites=(linch.ToolPrerequisite("b", ("a",)),))
    agent = linch.Agent(model="test", reliability=policy, hooks=[object()])
    hook = agent.hooks[0]
    one, two = ReliabilityRuntime(policy), ReliabilityRuntime(policy)
    first, second = one.bind(agent, "one"), two.bind(agent, "two")
    ctx = SimpleNamespace(session=SimpleNamespace(agent=agent), run_id="one", tool_name="b")
    assert hook.on_pre_tool_use(ctx).action == "block"
    one.succeeded.add("a")
    assert hook.on_pre_tool_use(ctx) is None
    ctx.run_id = "two"
    assert hook.on_pre_tool_use(ctx).action == "block"
    replacement = two.bind(agent, "one")
    await first.dispose()
    await first.dispose()
    assert agent.context.get("reliability_runs") == {"one": two, "two": two}
    await replacement.dispose()
    await second.dispose()
    assert agent.context.get("reliability_runs") == {}
    await agent.close()
    assert hook not in agent.hooks


def test_policy_validation_rejects_bad_graph_and_terminal_evidence() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        linch.ReliabilityPolicy(prerequisites=(linch.ToolPrerequisite("b", ("a", "a")),)).validate(
            ("a", "b")
        )
    with pytest.raises(ValueError, match="cycle"):
        linch.ReliabilityPolicy(
            prerequisites=(
                linch.ToolPrerequisite("a", ("b",)),
                linch.ToolPrerequisite("b", ("a",)),
            )
        ).validate(("a", "b"))
    with pytest.raises(ValueError, match="terminal"):
        linch.ReliabilityPolicy(
            completion=linch.CompletionGate(("final",)),
        ).validate(("final",), final_tool_name="final")


@pytest.mark.asyncio
async def test_agent_validates_after_tools_are_loaded() -> None:
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    agent = linch.Agent(
        model="test",
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        reliability=linch.ReliabilityPolicy(prerequisites=(linch.ToolPrerequisite("missing", ()),)),
    )
    with pytest.raises(ValueError, match="unknown"):
        await (await agent.session()).run("hello").__anext__()


def test_reliability_event_round_trips_without_provider_payload() -> None:
    event = linch.ReliabilityEvent("completion", "continue", "missing", 3, 1)
    raw = event_to_dict(event)
    assert raw == {
        "type": "reliability",
        "mechanism": "completion",
        "action": "continue",
        "reason": "missing",
        "turn_index": 3,
        "correction_count": 1,
    }
    assert event_from_dict(raw) == event


@pytest.mark.asyncio
async def test_turn_stopping_registration_is_identity_safe_and_serial() -> None:
    dispatcher = TurnStoppingDispatcher()
    calls: list[str] = []

    async def listener(_event: object) -> TurnStoppingDecision:
        calls.append("called")
        return TurnStoppingDecision("continue")

    first = dispatcher.register(listener)
    second = dispatcher.register(listener)
    await first.dispose()
    result = await dispatcher.dispatch(object())  # type: ignore[arg-type]
    assert result.action == "continue"
    assert calls == ["called"]
    await second.dispose()
    assert not dispatcher.active


@pytest.mark.asyncio
async def test_turn_stopping_accept_is_non_vetoing() -> None:
    dispatcher = TurnStoppingDispatcher()

    async def accept(_event: object) -> TurnStoppingDecision:
        return TurnStoppingDecision("accept")

    async def continue_(_event: object) -> TurnStoppingDecision:
        return TurnStoppingDecision("continue")

    dispatcher.register(accept)
    dispatcher.register(continue_)
    assert (await dispatcher.dispatch(object())).action == "continue"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_batch_success_and_checkpoint_state() -> None:
    from linch.types import ToolResultBlock, ToolUseBlock

    policy = linch.ReliabilityPolicy(prerequisites=(linch.ToolPrerequisite("b", ("a",)),))
    runtime = ReliabilityRuntime(policy)
    runtime.observe_results(
        [ToolUseBlock(id="a", name="a", input={})],
        {"a": ToolResultBlock(tool_use_id="a", content="ok")},
    )
    state = runtime.checkpoint_state()
    assert state == {"v": 1, "corrections": 0, "succeeded": ["a"]}
    assert ReliabilityRuntime(policy).restore(state).succeeded == {"a"}


@pytest.mark.asyncio
async def test_invalid_text_feedback_is_canonical_and_hides_rejected_text() -> None:
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore

    agent = linch.Agent(
        model="test",
        provider=ScriptedProvider(
            [TextTurn(text='<tool_call>{"secret": "raw"}</tool_call>'), TextTurn(text="done")]
        ),
        session_store=InMemorySessionStore(),
    )
    session = await agent.session()
    events = [event async for event in session.run("hello")]

    feedback = [
        event for event in events if event.type == "user" and event.subtype == "notification"
    ]
    assert len(feedback) == 1
    text = feedback[0].message.content[0].text
    assert "[Linch]" in text
    assert "nothing was executed" in text.lower()
    assert "raw" not in str(session.full_history)


@pytest.mark.asyncio
async def test_native_rejection_retries_once_and_never_runs_sibling_tools() -> None:
    import json
    from collections.abc import AsyncIterator

    from linch.events import ModelCallEvent, ReliabilityEvent, ResultEvent, event_to_dict
    from linch.providers import BaseProvider
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.types import Usage

    class Provider(BaseProvider):
        id = "native-reliability"

        def __init__(self) -> None:
            self.calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: object) -> AsyncIterator[dict[str, object]]:
            self.calls += 1
            yield {"type": "message_start", "model": "test"}
            if self.calls == 1:
                yield {"type": "tool_use_start", "id": "bad", "name": "never"}
                yield {
                    "type": "tool_use_input_delta",
                    "id": "bad",
                    "json_delta": '{"secret":"raw"',
                }
                yield {"type": "tool_use_end", "id": "bad"}
                yield {"type": "tool_use_start", "id": "sibling", "name": "never"}
                yield {
                    "type": "tool_use_input_delta",
                    "id": "sibling",
                    "json_delta": json.dumps({}),
                }
                yield {"type": "tool_use_end", "id": "sibling"}
                stop_reason = "tool_use"
            else:
                yield {"type": "text_delta", "text": "done"}
                stop_reason = "end_turn"
            yield {"type": "message_end", "stop_reason": stop_reason, "usage": Usage()}

    provider = Provider()
    run_store = InMemoryRunStore()
    agent = linch.Agent(
        model="test",
        provider=provider,
        session_store=InMemorySessionStore(),
        run_store=run_store,
        reliability=linch.ReliabilityPolicy(max_correction_turns=1),
        include_partial_messages=True,
    )
    session = await agent.session()
    events = [event async for event in session.run("hello")]
    run_id = next(event.run_id for event in events if event.type == "system")
    rows = await run_store.load_events(run_id)

    assert provider.calls == 2
    assert not any(event.type.startswith("tool_call_") for event in events)
    assert sum(isinstance(event, ReliabilityEvent) for event in events) == 1
    assert next(event for event in events if isinstance(event, ResultEvent)).subtype == "success"
    assert "raw" not in str(session.full_history)
    assert "raw" not in str([event_to_dict(row.event) for row in rows])
    assert (
        sum(
            isinstance(row.event, ModelCallEvent)
            and row.event.record_kind == "receipt"
            and row.event.record["kind"] == "primary"
            for row in rows
        )
        == 2
    )


@pytest.mark.asyncio
async def test_completion_gate_retries_after_real_tool_success() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.events import ReliabilityEvent, ResultEvent
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry, tool

    calls = 0

    @tool
    async def prepare() -> str:
        nonlocal calls
        calls += 1
        return "prepared"

    tools = ToolRegistry()
    tools.register(prepare)
    agent = linch.Agent(
        model="test",
        provider=ScriptedProvider(
            [
                TextTurn(text="premature"),
                ToolUseTurn(tool_name="prepare", tool_input={}),
                TextTurn(text="final"),
            ]
        ),
        tools=tools,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        reliability=linch.ReliabilityPolicy(completion=linch.CompletionGate(("prepare",))),
    )
    agent.on_turn_stopping(lambda _event: TurnStoppingDecision("accept"))
    session = await agent.session()
    events = [event async for event in session.run("hello")]

    assert calls == 1
    assert sum(isinstance(event, ReliabilityEvent) for event in events) == 1
    assert any(
        event.type == "user" and "Completion requirements are missing" in str(event.message)
        for event in events
    )
    assert next(event for event in events if isinstance(event, ResultEvent)).subtype == "success"


@pytest.mark.asyncio
async def test_prerequisite_blocks_same_batch_and_failed_predecessor() -> None:
    from collections.abc import AsyncIterator

    from linch.events import ToolCallEndEvent
    from linch.providers import BaseProvider
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry, ToolResult, tool
    from linch.types import Usage

    calls: list[str] = []

    @tool(name="first")
    async def first() -> ToolResult:
        calls.append("first")
        return ToolResult(content="failed", is_error=True)

    @tool(name="second")
    async def second() -> str:
        calls.append("second")
        return "should not run"

    class Provider(BaseProvider):
        id = "same-batch-reliability"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: object) -> AsyncIterator[dict[str, object]]:
            yield {"type": "message_start", "model": "test"}
            if not any(
                getattr(message, "role", None) == "user"
                and any(getattr(block, "type", None) == "tool_result" for block in message.content)
                for message in req.messages
            ):
                for tool_id, name in (("a", "first"), ("b", "second")):
                    yield {"type": "tool_use_start", "id": tool_id, "name": name}
                    yield {"type": "tool_use_input_delta", "id": tool_id, "json_delta": "{}"}
                    yield {"type": "tool_use_end", "id": tool_id}
                stop_reason = "tool_use"
            else:
                yield {"type": "text_delta", "text": "done"}
                stop_reason = "end_turn"
            yield {"type": "message_end", "stop_reason": stop_reason, "usage": Usage()}

    tools = ToolRegistry()
    tools.register(first)
    tools.register(second)
    agent = linch.Agent(
        model="test",
        provider=Provider(),
        tools=tools,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        reliability=linch.ReliabilityPolicy(
            prerequisites=(linch.ToolPrerequisite("second", ("first",)),)
        ),
    )
    session = await agent.session()
    events = [event async for event in session.run("hello")]
    ends = [event for event in events if isinstance(event, ToolCallEndEvent)]

    assert calls == ["first"]
    assert len(ends) == 2
    assert ends[0].is_error and ends[1].is_error


@pytest.mark.asyncio
async def test_completion_gate_vetoes_final_tool_before_terminal_ack() -> None:
    from linch.evals import ScriptedProvider, ToolUseTurn
    from linch.events import ReliabilityEvent, ResultEvent, ToolCallEndEvent
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry, tool

    calls: list[str] = []

    @tool
    async def prepare() -> str:
        calls.append("prepare")
        return "ready"

    @tool(name="emit_answer")
    async def emit_answer() -> str:
        calls.append("emit")
        return "never executes"

    tools = ToolRegistry()
    tools.register(prepare)
    tools.register(emit_answer)
    agent = linch.Agent(
        model="test",
        provider=ScriptedProvider(
            [
                ToolUseTurn(tool_name="emit_answer", tool_input={}),
                ToolUseTurn(tool_name="prepare", tool_input={}),
                ToolUseTurn(tool_name="emit_answer", tool_input={}),
            ]
        ),
        tools=tools,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        final_tool_name="emit_answer",
        reliability=linch.ReliabilityPolicy(completion=linch.CompletionGate(("prepare",))),
    )
    session = await agent.session()
    events = [event async for event in session.run("hello")]

    assert calls == ["prepare"]
    assert not any(
        isinstance(event, ToolCallEndEvent) and event.tool_name == "emit_answer" for event in events
    )
    assert sum(isinstance(event, ReliabilityEvent) for event in events) == 1
    assert next(event for event in events if isinstance(event, ResultEvent)).subtype == "success"


@pytest.mark.asyncio
async def test_background_ack_does_not_unlock_prerequisite() -> None:
    import asyncio

    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.events import ToolCallEndEvent
    from linch.sessions import InMemorySessionStore
    from linch.tools import ToolRegistry, tool

    finished = asyncio.Event()
    calls: list[str] = []

    @tool
    async def background() -> str:
        calls.append("background")
        finished.set()
        return "background result"

    @tool
    async def dependent() -> str:
        calls.append("dependent")
        return "should not run"

    tools = ToolRegistry()
    tools.register(background)
    tools.register(dependent)
    agent = linch.Agent(
        model="test",
        provider=ScriptedProvider(
            [
                ToolUseTurn(tool_name="background", tool_input={"run_in_background": True}),
                ToolUseTurn(tool_name="dependent", tool_input={}),
                TextTurn(text="done"),
            ]
        ),
        tools=tools,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        enable_background_tools=True,
        reliability=linch.ReliabilityPolicy(
            prerequisites=(linch.ToolPrerequisite("dependent", ("background",)),)
        ),
    )
    session = await agent.session()
    events = [event async for event in session.run("hello")]
    await asyncio.wait_for(finished.wait(), timeout=1)

    dependent_end = next(
        event
        for event in events
        if isinstance(event, ToolCallEndEvent) and event.tool_name == "dependent"
    )
    assert calls == ["background"]
    assert dependent_end.is_error


@pytest.mark.asyncio
async def test_two_agents_keep_reliability_counters_isolated() -> None:
    import asyncio

    from linch.evals import ScriptedProvider, TextTurn
    from linch.events import ReliabilityEvent
    from linch.sessions import InMemorySessionStore

    policy = linch.ReliabilityPolicy(max_correction_turns=1)
    agents = [
        linch.Agent(
            model="test",
            provider=ScriptedProvider(
                [TextTurn(text="<tool_call>bad</tool_call>"), TextTurn(text=tag)]
            ),
            session_store=InMemorySessionStore(),
            reliability=policy,
        )
        for tag in ("a", "b")
    ]
    sessions = [await agent.session() for agent in agents]

    async def collect(session: object) -> list[object]:
        return [event async for event in session.run("hello")]  # type: ignore[attr-defined]

    traces = await asyncio.gather(*(collect(session) for session in sessions))

    assert [sum(isinstance(event, ReliabilityEvent) for event in trace) for trace in traces] == [
        1,
        1,
    ]


@pytest.mark.parametrize("policy", [None, linch.ReliabilityPolicy(retry_invalid_responses=False)])
async def test_disabled_invalid_listener_preserves_partial_events(policy):
    from linch.evals import ScriptedProvider, TextTurn
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    runs = InMemoryRunStore()
    agent = linch.Agent(
        model="test",
        provider=ScriptedProvider([TextTurn(text="<tool_call>x</tool_call>")]),
        session_store=InMemorySessionStore(),
        run_store=runs,
        reliability=policy,
        include_partial_messages=True,
    )
    session = await agent.session()
    events = [event async for event in session.run("go")]
    rows = await runs.load_events(next(event.run_id for event in events if event.type == "system"))
    assert [event_to_dict(event) for event in events if event.type == "partial_assistant"] == [
        event_to_dict(row.event) for row in rows if row.event.type == "partial_assistant"
    ]
    assert not any(event.type == "reliability" for event in events)


@pytest.mark.parametrize("invalid", [False, True])
async def test_generic_continue_preserves_message_tuple(invalid):
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore
    from linch.types import Message, TextBlock

    messages = (
        Message(role="user", content=[TextBlock(text="first"), TextBlock(text="second")]),
        Message(role="user", content=[TextBlock(text="third")]),
    )
    agent = linch.Agent(
        model="test",
        reliability=None,
        provider=ScriptedProvider(
            [TextTurn(text="" if invalid else "candidate"), TextTurn(text="done")]
        ),
        session_store=InMemorySessionStore(),
    )

    def listener(event):
        return (
            TurnStoppingDecision("continue", feedback=messages) if event.turn_index == 0 else None
        )

    agent.on_turn_stopping(listener)
    session = await agent.session()
    _ = [event async for event in session.run("go")]
    assert list(session.full_history)[-3:-1] == list(messages)


async def test_invalid_fail_surfaces_listener_error():
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore

    agent = linch.Agent(
        model="test",
        reliability=None,
        provider=ScriptedProvider([TextTurn(text="")]),
        session_store=InMemorySessionStore(),
    )
    agent.on_turn_stopping(
        lambda event: TurnStoppingDecision("fail", error=LookupError("specific failure"))
    )
    events = [event async for event in (await agent.session()).run("go")]
    assert any(
        event.type == "error" and event.error["message"] == "specific failure" for event in events
    )


@pytest.mark.parametrize("invalid", [True, False])
async def test_shared_budget_stops_after_two_corrections(invalid):
    from linch.evals import ScriptedProvider, TextTurn
    from linch.sessions import InMemorySessionStore
    from linch.tools import tool
    from linch.tools.registry import empty_tools

    @tool
    async def required() -> str:
        return "ready"

    agent = linch.Agent(
        model="test",
        tools=empty_tools(required),
        provider=ScriptedProvider([TextTurn(text="" if invalid else "early")] * 3),
        session_store=InMemorySessionStore(),
        reliability=linch.ReliabilityPolicy(completion=linch.CompletionGate(("required",))),
    )
    events = [event async for event in (await agent.session()).run("go")]
    corrections = [event for event in events if event.type == "reliability"]
    assert [(event.action, event.correction_count) for event in corrections] == [
        ("continue", 1),
        ("continue", 2),
        ("fail", 2),
    ]
    assert events[-1].type == "result" and events[-1].subtype == "error"


@pytest.mark.parametrize(
    "text,stop",
    [
        ("ordinary", "end_turn"),
        ("Example: <tool_call>test</tool_call>", "end_turn"),
        ("```\n<tool_call>test</tool_call>\n```", "end_turn"),
        ("", "refusal"),
        ("partial", "max_tokens"),
    ],
)
async def test_valid_non_envelope_routes_have_no_reliability_effect(text, stop):
    from linch.evals import ScriptedProvider, TextTurn
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    runs = InMemoryRunStore()
    agent = linch.Agent(
        model="test",
        provider=ScriptedProvider([TextTurn(text=text, stop_reason=stop)]),
        session_store=InMemorySessionStore(),
        run_store=runs,
    )
    events = [event async for event in (await agent.session()).run("go")]
    record = await runs.load_run(next(event.run_id for event in events if event.type == "system"))
    assert not any(event.type == "reliability" for event in events)
    assert "linch.reliability" not in record.checkpoint.extension_state


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"v": 2, "corrections": 0, "succeeded": []},
        {"v": True, "corrections": 0, "succeeded": []},
        {"v": 1, "corrections": True, "succeeded": []},
        {"v": 1, "corrections": 3, "succeeded": []},
        {"v": 1, "corrections": -1, "succeeded": []},
        {"v": 1, "corrections": 0, "succeeded": ["unknown"]},
        {"v": 1, "corrections": 0, "succeeded": ["b", "a"]},
        {"v": 1, "corrections": 0, "succeeded": ["a", "a"]},
    ],
)
def test_checkpoint_state_rejects_corruption(raw):
    policy = linch.ReliabilityPolicy(completion=linch.CompletionGate(("a", "b")))
    with pytest.raises(ValueError, match="checkpoint state"):
        ReliabilityRuntime(policy).restore(raw)


def test_canonical_policy_ignores_edge_order_and_rejects_duplicate_rules():
    from linch.reliability import canonical_reliability_policy

    first = linch.ReliabilityPolicy(prerequisites=(linch.ToolPrerequisite("b", ("a", "c")),))
    second = linch.ReliabilityPolicy(
        prerequisites=(linch.ToolPrerequisite("b", ("c",)), linch.ToolPrerequisite("b", ("a",)))
    )
    assert canonical_reliability_policy(first) == canonical_reliability_policy(second)
    with pytest.raises(ValueError, match="duplicate"):
        linch.ReliabilityPolicy(prerequisites=first.prerequisites * 2).validate(("a", "b", "c"))


async def test_unused_evidence_and_disabled_listener_stay_empty():
    runtime = ReliabilityRuntime(linch.ReliabilityPolicy(retry_invalid_responses=False))
    runtime.observe_results([], {})
    assert runtime.checkpoint_state() == {}
    assert linch.Agent(model="test", reliability=runtime.policy).hooks == []


async def test_force_final_cannot_bypass_completion():
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.sessions import InMemorySessionStore
    from linch.tools import tool
    from linch.tools.registry import empty_tools

    @tool
    async def repeat() -> str:
        return "repeat"

    @tool
    async def required() -> str:
        return "ready"

    agent = linch.Agent(
        model="test",
        tools=empty_tools(repeat, required),
        provider=ScriptedProvider(
            [ToolUseTurn(tool_name="repeat", tool_input={})] + [TextTurn(text="early")] * 3
        ),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        loop_guard={"max_identical_tool_calls": 1, "force_final_answer": True},
        reliability=linch.ReliabilityPolicy(completion=linch.CompletionGate(("required",))),
    )
    events = [event async for event in (await agent.session()).run("go")]
    assert any(event.type == "loop_guard" for event in events)
    assert [event.correction_count for event in events if event.type == "reliability"] == [1, 2, 2]
    assert events[-1].type == "result" and events[-1].subtype == "error"


def test_host_control_message_keeps_system_reminder_wrapping():
    from linch.loop.checkpoint import _primary_control_message
    from linch.types import Message

    control = _primary_control_message(
        Message(role="assistant", content=[]), "[Linch] host feedback"
    )
    assert control.content[0].text == "<system-reminder>\n[Linch] host feedback\n</system-reminder>"


async def test_inferred_terminal_tool_cannot_be_completion_evidence():
    from linch.evals import ScriptedProvider, ToolUseTurn
    from linch.sessions import InMemorySessionStore
    from linch.tools import tool
    from linch.tools.registry import empty_tools
    from linch.types import OutputSchema

    @tool
    async def answer() -> str:
        raise AssertionError("terminal tool must never execute")

    provider = ScriptedProvider([ToolUseTurn("answer", {})])
    assert provider.capabilities("test").structured_output_terminal_tool
    agent = linch.Agent(
        model="test",
        provider=provider,
        tools=empty_tools(answer),
        session_store=InMemorySessionStore(),
        output_schema=OutputSchema("answer", {"type": "object"}),
        reliability=linch.ReliabilityPolicy(completion=linch.CompletionGate(("answer",))),
    )
    with pytest.raises(ValueError, match="terminal"):
        _ = [event async for event in (await agent.session()).run("go")]
    assert provider._index == 0


async def test_same_agent_concurrent_runs_keep_prerequisites_isolated():
    import asyncio

    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.sessions import InMemorySessionStore
    from linch.tools import tool
    from linch.tools.registry import empty_tools

    barrier, prepared = asyncio.Barrier(2), asyncio.Event()
    calls = []

    @tool
    async def prepare() -> str:
        return "ready"

    @tool
    async def dependent() -> str:
        calls.append("dependent")
        return "done"

    class Provider:
        id = "concurrent-reliability"
        streams = {
            "one": ScriptedProvider(
                [ToolUseTurn("prepare", {}), ToolUseTurn("dependent", {}), TextTurn("done")]
            ),
            "two": ScriptedProvider([ToolUseTurn("dependent", {}), TextTurn("done")]),
        }
        counts = {"one": 0, "two": 0}

        def context_window(self, model):
            return 100_000

        async def stream(self, request):
            tag = next(
                block.text
                for message in request.messages
                for block in message.content
                if getattr(block, "text", None) in {"one", "two"}
            )
            self.counts[tag] += 1
            if self.counts[tag] == 1:
                await barrier.wait()
                assert len(agent.context.get("reliability_runs")) == 2
            if tag == "one" and self.counts[tag] == 2:
                prepared.set()
            if tag == "two":
                await prepared.wait()
            async for event in self.streams[tag].stream(request):
                yield event

    agent = linch.Agent(
        model="test",
        provider=Provider(),
        tools=empty_tools(prepare, dependent),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        reliability=linch.ReliabilityPolicy(
            prerequisites=(linch.ToolPrerequisite("dependent", ("prepare",)),)
        ),
    )
    sessions = [await agent.session(), await agent.session()]

    async def collect(session, prompt):
        return [event async for event in session.run(prompt)]

    one, two = await asyncio.wait_for(
        asyncio.gather(collect(sessions[0], "one"), collect(sessions[1], "two")), 5
    )
    assert calls == ["dependent"], [
        event for event in one + two if event.type in {"error", "tool_call_end"}
    ]
    assert any(event.type == "tool_call_end" and event.is_error for event in two)
    assert one[-1].subtype == two[-1].subtype == "success"
    assert agent.context.get("reliability_runs") == {}
