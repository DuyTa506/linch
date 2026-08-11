"""Workflow engine integration tests.

linch imports happen inside test functions / provider methods (not at module
level) because tests/loop/test_hardening.py pops all ``linch*`` modules from
``sys.modules``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest


class CountingTextProvider:
    """Returns 'result-N' per call; optionally raises on a given call number."""

    id = "fake"

    def __init__(self, fail_on_call: int | None = None, tokens_per_turn: int = 100) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call
        self.tokens_per_turn = tokens_per_turn

    def context_window(self, model: str) -> int:
        return 10_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("provider blew up")
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": f"result-{self.calls}"}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(input_tokens=self.tokens_per_turn),
        }


class SchemaNameProvider:
    """Returns JSON derived from the requested output schema name."""

    id = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.schema_names: list[str | None] = []

    def context_window(self, model: str) -> int:
        return 10_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        self.calls += 1
        schema_name = req.output_schema.name if req.output_schema is not None else None
        self.schema_names.append(schema_name)
        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": json.dumps({"schema": schema_name})}
        yield {
            "type": "message_end",
            "stop_reason": "end_turn",
            "usage": Usage(input_tokens=10),
        }


def _make_agent(provider: Any, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        **kwargs,
    )


async def test_run_workflow_returns_function_value() -> None:
    agent = _make_agent(CountingTextProvider())

    async def flow(wf: Any) -> dict[str, int]:
        await wf.phase("noop")
        return {"answer": 42}

    result = await agent.run_workflow(flow)

    assert result == {"answer": 42}


async def test_wf_agent_runs_subagent_and_returns_final_text() -> None:
    provider = CountingTextProvider()
    agent = _make_agent(provider)
    seen: list[Any] = []

    async def flow(wf: Any) -> str:
        return await wf.agent("summarize the repo", label="summarizer")

    result = await agent.run_workflow(flow, on_event=seen.append)

    assert result == "result-1"
    kinds = [e.kind for e in seen if e.type == "workflow"]
    assert kinds == ["agent_start", "agent_end"]
    assert any(e.type == "subagent_event" for e in seen)
    end = [e for e in seen if e.type == "workflow" and e.kind == "agent_end"][0]
    assert end.result_text == "result-1"
    assert end.title == "summarizer"


async def test_wf_agent_returns_json_string_for_structured_output() -> None:
    from linch import OutputSchema

    provider = SchemaNameProvider()
    agent = _make_agent(provider)
    seen: list[Any] = []
    schema = OutputSchema(
        name="alpha",
        schema={
            "type": "object",
            "properties": {"schema": {"type": "string"}},
            "required": ["schema"],
        },
    )

    async def flow(wf: Any) -> str:
        return await wf.agent("extract", output_schema=schema)

    result = await agent.run_workflow(flow, on_event=seen.append)

    assert json.loads(result) == {"schema": "alpha"}
    end = [e for e in seen if e.type == "workflow" and e.kind == "agent_end"][0]
    assert end.result_text == '{"schema":"alpha"}'
    assert end.structured_output == {"schema": "alpha"}


async def test_wf_agent_structured_output_replays_from_journal() -> None:
    from linch import InMemoryRunStore, OutputSchema

    provider = SchemaNameProvider()
    agent = _make_agent(provider, run_store=InMemoryRunStore())
    seen: list[Any] = []
    schema = OutputSchema(
        name="alpha",
        schema={
            "type": "object",
            "properties": {"schema": {"type": "string"}},
            "required": ["schema"],
        },
    )

    async def flow(wf: Any) -> str:
        return await wf.agent("extract", output_schema=schema)

    first = await agent.run_workflow(flow, run_id="wf-structured", on_event=seen.append)
    second = await agent.run_workflow(flow, run_id="wf-structured", on_event=seen.append)

    assert first == second == '{"schema":"alpha"}'
    assert provider.calls == 1
    replay = [e for e in seen if e.type == "workflow" and e.kind == "agent_replayed"][0]
    assert replay.result_text == '{"schema":"alpha"}'
    assert replay.structured_output == {"schema": "alpha"}


async def test_wf_agent_run_options_are_part_of_replay_key() -> None:
    from linch import InMemoryRunStore, OutputSchema

    provider = SchemaNameProvider()
    agent = _make_agent(provider, run_store=InMemoryRunStore())
    schema_a = OutputSchema(name="alpha", schema={"type": "object"})
    schema_b = OutputSchema(name="beta", schema={"type": "object"})

    async def flow_a(wf: Any) -> str:
        return await wf.agent("same prompt", output_schema=schema_a)

    async def flow_b(wf: Any) -> str:
        return await wf.agent("same prompt", output_schema=schema_b)

    first = await agent.run_workflow(flow_a, run_id="wf-key")
    second = await agent.run_workflow(flow_b, run_id="wf-key")

    assert json.loads(first) == {"schema": "alpha"}
    assert json.loads(second) == {"schema": "beta"}
    assert provider.calls == 2


async def test_wf_agent_tool_filter_is_part_of_replay_key() -> None:
    from linch import InMemoryRunStore

    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=InMemoryRunStore())

    async def flow_with_defaults(wf: Any) -> str:
        return await wf.agent("same prompt")

    async def flow_with_no_tools(wf: Any) -> str:
        return await wf.agent("same prompt", tools=[])

    first = await agent.run_workflow(flow_with_defaults, run_id="wf-tool-key")
    second = await agent.run_workflow(flow_with_no_tools, run_id="wf-tool-key")
    replay = await agent.run_workflow(flow_with_no_tools, run_id="wf-tool-key")

    assert first == "result-1"
    assert second == replay == "result-2"
    assert provider.calls == 2


async def test_wf_agent_inherited_subagent_tool_filter_is_part_of_replay_key() -> None:
    from linch import InMemoryRunStore
    from linch.subagents.registry import AgentRegistry
    from linch.subagents.types import AgentDefinition, AgentFrontmatter

    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=InMemoryRunStore())
    definition = AgentDefinition(
        name="reviewer",
        file_path="<test>",
        source="disk",
        frontmatter=AgentFrontmatter(
            name="reviewer",
            description="Review the result.",
            tools=["Read"],
        ),
        body="Review the result.",
    )
    agent.subagent_registry = AgentRegistry([definition])
    agent._subagents_loaded = True

    async def flow(wf: Any) -> str:
        return await wf.agent("same prompt", name="reviewer")

    first = await agent.run_workflow(flow, run_id="wf-inherited-tool-key")
    definition.frontmatter.tools = ["Grep"]
    second = await agent.run_workflow(flow, run_id="wf-inherited-tool-key")

    assert first == "result-1"
    assert second == "result-2"
    assert provider.calls == 2


async def test_wf_agent_passes_isolation_to_subagent() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.tools import ToolRegistry, tool
    from linch.tools.isolation import TempDirIsolation

    recorded: list[str] = []

    @tool
    def record_cwd(ctx: Any) -> str:
        """Record the execution cwd."""
        recorded.append(ctx.cwd)
        return "ok"

    tools = ToolRegistry()
    tools.register(record_cwd)
    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="record_cwd", tool_input={}), TextTurn("done")]
    )
    agent = _make_agent(provider, tools=tools)
    iso = TempDirIsolation()

    async def flow(wf: Any) -> str:
        return await wf.agent("go", isolation=iso)

    await agent.run_workflow(flow)

    assert len(recorded) == 1
    assert recorded[0] != agent.cwd


async def test_wf_agent_unknown_name_raises_config_error() -> None:
    from linch.errors import ConfigError

    agent = _make_agent(CountingTextProvider())

    async def flow(wf: Any) -> str:
        return await wf.agent("do it", name="no-such-agent")

    with pytest.raises(ConfigError, match="no-such-agent"):
        await agent.run_workflow(flow)


async def test_wf_agent_child_error_raises_workflow_error() -> None:
    from linch.workflow import WorkflowError

    agent = _make_agent(CountingTextProvider(fail_on_call=1))

    async def flow(wf: Any) -> str:
        return await wf.agent("doomed task")

    with pytest.raises(WorkflowError, match="provider blew up"):
        await agent.run_workflow(flow)


async def test_budget_shared_across_workflow_children() -> None:
    from linch import RunBudget

    provider = CountingTextProvider(tokens_per_turn=100)
    agent = _make_agent(provider)
    budget = RunBudget(max_tokens=10_000)

    async def flow(wf: Any) -> list[str]:
        first = await wf.agent("task one")
        second = await wf.agent("task two")
        assert wf.budget is budget
        return [first, second]

    result = await agent.run_workflow(flow, budget=budget)

    assert result == ["result-1", "result-2"]
    assert budget.spent_tokens == 200


async def test_resume_replays_unchanged_prefix(tmp_path: Path) -> None:
    from linch import SqliteRunStore
    from linch.workflow import WorkflowError

    store_path = str(tmp_path / "runs.db")

    async def flow(wf: Any) -> list[str]:
        one = await wf.agent("step one")
        two = await wf.agent("step two")
        three = await wf.agent("step three")
        return [one, two, three]

    # First attempt: provider dies on the third subagent call.
    provider1 = CountingTextProvider(fail_on_call=3)
    agent1 = _make_agent(provider1, run_store=SqliteRunStore(store_path))
    with pytest.raises(WorkflowError):
        await agent1.run_workflow(flow, run_id="wf-resume-1")
    assert provider1.calls == 3

    # Resume: calls 1-2 replay from the journal; only call 3 hits the provider.
    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=SqliteRunStore(store_path))
    seen: list[Any] = []
    result = await agent2.run_workflow(flow, run_id="wf-resume-1", on_event=seen.append)

    assert provider2.calls == 1
    replays = [e for e in seen if e.type == "workflow" and e.kind == "agent_replayed"]
    assert len(replays) == 2
    assert result == ["result-1", "result-2", "result-1"]


async def test_changed_prompt_invalidates_cache(tmp_path: Path) -> None:
    from linch import SqliteRunStore

    store_path = str(tmp_path / "runs.db")

    async def flow_v1(wf: Any) -> list[str]:
        return [await wf.agent("step one"), await wf.agent("step two")]

    provider1 = CountingTextProvider()
    agent1 = _make_agent(provider1, run_store=SqliteRunStore(store_path))
    await agent1.run_workflow(flow_v1, run_id="wf-edit-1")
    assert provider1.calls == 2

    # Edited workflow: step one unchanged (replays), step two reworded (runs).
    async def flow_v2(wf: Any) -> list[str]:
        return [await wf.agent("step one"), await wf.agent("step two, reworded")]

    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=SqliteRunStore(store_path))
    result = await agent2.run_workflow(flow_v2, run_id="wf-edit-1")

    assert provider2.calls == 1
    assert result == ["result-1", "result-1"]


async def test_resume_replays_prefix_predating_tool_aware_fingerprint(tmp_path: Path) -> None:
    """A run persisted before the tools-aware fingerprint (no fingerprint version
    stamped in its meta) must still replay its unchanged prefix on resume, not
    silently re-execute it under the new, differently-keyed formula."""
    from linch import SqliteRunStore
    from linch.events import WorkflowEvent
    from linch.subagents.registry import AgentRegistry
    from linch.subagents.types import AgentDefinition, AgentFrontmatter
    from linch.workflow.context import _run_options_fingerprint
    from linch.workflow.journal import call_key

    store_path = str(tmp_path / "runs.db")
    store = SqliteRunStore(store_path)

    # Simulate a run persisted by pre-upgrade code: create_run() never stamped a
    # fingerprint version, and the "reviewer" subagent's tools frontmatter was
    # never folded into the key (the legacy formula only fingerprints run_options).
    run = await store.create_run("host-session", id="wf-legacy-resume")
    assert "journal_fingerprint_version" not in run.meta
    legacy_key = call_key("reviewer", "same prompt", _run_options_fingerprint(None))
    await store.append_event(
        "wf-legacy-resume",
        WorkflowEvent(
            kind="agent_end",
            title="agent",
            call_key=legacy_key,
            occurrence=0,
            result_text="result-1",
        ),
    )

    definition = AgentDefinition(
        name="reviewer",
        file_path="<test>",
        source="disk",
        frontmatter=AgentFrontmatter(name="reviewer", description="Review.", tools=["Read"]),
        body="Review the result.",
    )
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)
    agent.subagent_registry = AgentRegistry([definition])
    agent._subagents_loaded = True

    async def flow(wf: Any) -> str:
        return await wf.agent("same prompt", name="reviewer")

    result = await agent.run_workflow(flow, run_id="wf-legacy-resume")

    assert provider.calls == 0
    assert result == "result-1"


async def test_run_id_without_run_store_raises_config_error() -> None:
    from linch.errors import ConfigError

    agent = _make_agent(CountingTextProvider())

    async def flow(wf: Any) -> None:
        return None

    with pytest.raises(ConfigError, match="run_store"):
        await agent.run_workflow(flow, run_id="wf-1")


async def test_workflow_timeout_error_is_a_retryable_workflow_error() -> None:
    from linch import WorkflowError, WorkflowTimeoutError

    assert issubclass(WorkflowTimeoutError, WorkflowError)
    assert WorkflowTimeoutError("x").retryable is True


async def test_agent_timeout_cancels_the_child_and_leaks_no_tasks() -> None:
    from linch.workflow import WorkflowTimeoutError

    class BlockingProvider(CountingTextProvider):
        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            await asyncio.Event().wait()
            yield {}  # pragma: no cover - never reached

    agent = _make_agent(BlockingProvider())

    async def flow(wf: Any) -> str:
        return await wf.agent("hangs forever", timeout_ms=50)

    with pytest.raises(WorkflowTimeoutError, match="timed out"):
        await agent.run_workflow(flow)

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    current = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if not t.done() and t is not current] == []


async def test_run_workflow_signal_stops_agent_and_step_calls() -> None:
    from linch.abort import AbortContext
    from linch.errors import AbortError

    signal = AbortContext()
    signal.abort()
    provider = CountingTextProvider()
    agent = _make_agent(provider)
    ran: list[str] = []

    async def flow(wf: Any) -> str:
        return await wf.agent("never runs")

    with pytest.raises(AbortError):
        await agent.run_workflow(flow, signal=signal)
    assert provider.calls == 0

    async def step_flow(wf: Any) -> str:
        return await wf.step("never runs", lambda: ran.append("x") or "x")

    with pytest.raises(AbortError):
        await agent.run_workflow(step_flow, signal=signal)
    assert ran == []


async def test_wf_agent_retry_reruns_a_failed_subagent_and_journals_once() -> None:
    from linch.providers.retry import RetryOptions
    from linch.run_store import InMemoryRunStore

    provider = CountingTextProvider(fail_on_call=1)
    store = InMemoryRunStore()
    agent = _make_agent(provider, run_store=store)

    async def flow(wf: Any) -> str:
        return await wf.agent(
            "flaky",
            retry=RetryOptions(max_attempts=3, base_delay_ms=0, max_delay_ms=0, jitter=0.0),
        )

    result = await agent.run_workflow(flow, run_id="wf-retry-1")

    assert result == "result-2"
    assert provider.calls == 2
    stored = await store.load_events("wf-retry-1")
    ends = [s.event for s in stored if getattr(s.event, "kind", None) == "agent_end"]
    assert len(ends) == 1  # invariant 18: only the winning attempt is journaled
    assert ends[0].occurrence == 0


async def test_wf_agent_retry_exhaustion_journals_nothing_and_reruns_on_resume() -> None:
    from linch.providers.retry import RetryOptions
    from linch.run_store import InMemoryRunStore
    from linch.workflow import WorkflowError

    class AlwaysFailingProvider(CountingTextProvider):
        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            self.calls += 1
            raise RuntimeError("provider blew up")
            yield {}  # pragma: no cover - unreachable, makes this an async generator

    store = InMemoryRunStore()
    always_failing = AlwaysFailingProvider()

    async def flow(wf: Any) -> str:
        return await wf.agent(
            "flaky",
            retry=RetryOptions(max_attempts=2, base_delay_ms=0, max_delay_ms=0, jitter=0.0),
        )

    agent1 = _make_agent(always_failing, run_store=store)
    with pytest.raises(WorkflowError):
        await agent1.run_workflow(flow, run_id="wf-retry-2")
    assert always_failing.calls == 2

    stored = await store.load_events("wf-retry-2")
    assert [s.event for s in stored if getattr(s.event, "kind", None) == "agent_end"] == []

    agent2 = _make_agent(CountingTextProvider(), run_store=store)
    assert await agent2.run_workflow(flow, run_id="wf-retry-2") == "result-1"


async def test_interrupt_suspends_without_marking_the_run_failed() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.workflow import WorkflowSuspended

    store = InMemoryRunStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    seen: list[Any] = []

    async def flow(wf: Any) -> str:
        return await wf.interrupt("approve", {"diff": "one line"})

    with pytest.raises(WorkflowSuspended) as excinfo:
        await agent.run_workflow(flow, run_id="wf-hitl-1", on_event=seen.append)

    assert excinfo.value.key == "approve"
    assert excinfo.value.payload == {"diff": "one line"}

    record = await store.load_run("wf-hitl-1")
    assert record is not None
    assert record.status == "suspended"  # not "failed"

    requests = [e for e in seen if e.type == "workflow" and e.kind == "interrupt_requested"]
    assert len(requests) == 1
    assert requests[0].structured_output == {"payload": {"diff": "one line"}}

    # The host session must still be released on the suspend path.
    assert agent._sessions == {}


async def test_workflow_suspended_escapes_a_user_except_exception() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.workflow import WorkflowSuspended

    agent = _make_agent(CountingTextProvider(), run_store=InMemoryRunStore())
    swallowed: list[str] = []

    async def flow(wf: Any) -> str:
        try:
            return await wf.interrupt("approve")
        except Exception:  # must NOT catch a suspend
            swallowed.append("caught")
            return "swallowed"

    with pytest.raises(WorkflowSuspended):
        await agent.run_workflow(flow, run_id="wf-hitl-2")
    assert swallowed == []


async def test_resume_supplies_the_interrupt_value_and_completes() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.workflow import WorkflowSuspended

    store = InMemoryRunStore()
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)

    async def flow(wf: Any) -> list[Any]:
        first = await wf.agent("draft it")
        approved = await wf.interrupt("approve", {"draft": first})
        return [first, approved]

    with pytest.raises(WorkflowSuspended):
        await agent.run_workflow(flow, run_id="wf-hitl-3")
    assert provider.calls == 1

    result = await agent.run_workflow(flow, run_id="wf-hitl-3", resume={"approve": True})

    assert result == ["result-1", True]
    assert provider.calls == 1  # the agent prefix replayed

    record = await store.load_run("wf-hitl-3")
    assert record is not None
    assert record.status == "completed"

    # A later resume needs no resume mapping: the answer itself is journaled.
    seen: list[Any] = []
    assert await agent.run_workflow(flow, run_id="wf-hitl-3", on_event=seen.append) == [
        "result-1",
        True,
    ]
    assert [e for e in seen if e.type == "workflow" and e.kind == "interrupt_replayed"]


async def test_interrupt_inside_parallel_cancels_siblings_and_suspends() -> None:
    """Documented limitation: a fan-out cannot collect several interrupts."""
    from linch.run_store import InMemoryRunStore
    from linch.workflow import WorkflowSuspended

    agent = _make_agent(CountingTextProvider(), run_store=InMemoryRunStore())
    cleaned: list[str] = []

    async def flow(wf: Any) -> list[Any]:
        async def sibling() -> str:
            try:
                await asyncio.sleep(1.0)
                return "done"
            finally:
                cleaned.append("sibling")

        return await wf.parallel([lambda: wf.interrupt("approve"), sibling])

    with pytest.raises(WorkflowSuspended):
        await agent.run_workflow(flow, run_id="wf-hitl-4")

    assert cleaned == ["sibling"]


async def test_deadline_stops_the_workflow_and_marks_the_run_failed() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.workflow import WorkflowTimeoutError

    store = InMemoryRunStore()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    cleaned: list[str] = []

    async def flow(wf: Any) -> str:
        async def slow() -> str:
            try:
                await asyncio.sleep(1.0)
                return "never"
            finally:
                cleaned.append("slow")

        return await wf.step("slow", slow)

    with pytest.raises(WorkflowTimeoutError, match="deadline"):
        await agent.run_workflow(flow, run_id="wf-deadline-1", deadline_ms=20)

    assert cleaned == ["slow"]  # the in-flight step was cancelled, not orphaned
    record = await store.load_run("wf-deadline-1")
    assert record is not None
    assert record.status == "failed"
    assert agent._sessions == {}


async def test_deadline_leaves_the_journaled_prefix_resumable() -> None:
    from linch.run_store import InMemoryRunStore
    from linch.workflow import WorkflowTimeoutError

    store = InMemoryRunStore()
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)
    side_effects: list[str] = []

    async def flow(wf: Any, *, stall: bool) -> str:
        await wf.step("publish", lambda: side_effects.append("published") or "ok")
        if stall:
            await asyncio.sleep(1.0)
        return await wf.agent("summarize")

    async def stalling(wf: Any) -> str:
        return await flow(wf, stall=True)

    async def finishing(wf: Any) -> str:
        return await flow(wf, stall=False)

    with pytest.raises(WorkflowTimeoutError):
        await agent.run_workflow(stalling, run_id="wf-deadline-2", deadline_ms=30)

    assert await agent.run_workflow(finishing, run_id="wf-deadline-2") == "result-1"
    assert side_effects == ["published"]  # replayed, not re-executed


async def test_no_deadline_never_reaches_wait_for(monkeypatch: Any) -> None:
    agent = _make_agent(CountingTextProvider())

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("wait_for must not be called without a deadline")

    monkeypatch.setattr(asyncio, "wait_for", explode)

    async def flow(wf: Any) -> str:
        return "ok"

    assert await agent.run_workflow(flow) == "ok"


class ConcurrencyTrackingProvider(CountingTextProvider):
    """Records how many subagent runs were in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.high_water = 0

    async def stream(self, req: Any) -> Any:
        self.active += 1
        self.high_water = max(self.high_water, self.active)
        try:
            await asyncio.sleep(0.01)
            async for chunk in super().stream(req):
                yield chunk
        finally:
            self.active -= 1


async def test_max_agent_concurrency_caps_live_subagent_runs() -> None:
    provider = ConcurrencyTrackingProvider()
    agent = _make_agent(provider)

    async def flow(wf: Any) -> list[str]:
        return await wf.parallel([lambda i=i: wf.agent(f"task {i}") for i in range(6)])

    results = await agent.run_workflow(flow, max_concurrency=8, max_agent_concurrency=2)

    assert len(results) == 6
    assert provider.high_water == 2


async def test_max_agent_concurrency_unset_leaves_the_fan_out_uncapped() -> None:
    provider = ConcurrencyTrackingProvider()
    agent = _make_agent(provider)

    async def flow(wf: Any) -> list[str]:
        return await wf.parallel([lambda i=i: wf.agent(f"task {i}") for i in range(6)])

    await agent.run_workflow(flow, max_concurrency=6)

    assert provider.high_water == 6


async def test_max_agent_concurrency_does_not_gate_the_replay_path() -> None:
    """A replayed prefix makes no provider call, so it must not queue behind the gate."""
    from linch.run_store import InMemoryRunStore

    store = InMemoryRunStore()
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)

    async def flow(wf: Any) -> list[str]:
        return await wf.parallel([lambda i=i: wf.agent(f"task {i}") for i in range(4)])

    first = await agent.run_workflow(flow, run_id="wf-gate-1", max_agent_concurrency=1)
    second = await agent.run_workflow(flow, run_id="wf-gate-1", max_agent_concurrency=1)

    assert second == first
    assert provider.calls == 4


def _recording_store() -> Any:
    """An InMemoryRunStore that remembers every load_events watermark."""
    from linch.run_store import InMemoryRunStore

    class RecordingStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.after_seqs: list[int] = []

        async def load_events(self, run_id: str, *, after_seq: int = 0) -> Any:
            self.after_seqs.append(after_seq)
            return await super().load_events(run_id, after_seq=after_seq)

    return RecordingStore()


async def test_snapshot_is_off_by_default() -> None:
    store = _recording_store()
    agent = _make_agent(CountingTextProvider(), run_store=store)

    async def flow(wf: Any) -> list[Any]:
        return [await wf.step(f"s{i}", lambda i=i: i) for i in range(4)]

    assert await agent.run_workflow(flow, run_id="wf-snap-0") == [0, 1, 2, 3]

    record = await store.load_run("wf-snap-0")
    assert record is not None
    assert record.checkpoint is not None
    assert record.checkpoint.extension_state == {}


async def test_snapshot_lets_resume_skip_the_folded_event_prefix() -> None:
    from linch.workflow import WorkflowSuspended

    store = _recording_store()
    provider = CountingTextProvider()
    agent = _make_agent(provider, run_store=store)
    side_effects: list[str] = []

    async def flow(wf: Any) -> Any:
        for i in range(4):
            await wf.step(f"s{i}", lambda i=i: side_effects.append(f"s{i}") or i)
        await wf.agent("summarize")
        return await wf.interrupt("ship")

    with pytest.raises(WorkflowSuspended):
        await agent.run_workflow(flow, run_id="wf-snap-1", journal_snapshot_every=2)
    assert side_effects == ["s0", "s1", "s2", "s3"]

    saved = await store.load_run("wf-snap-1")
    assert saved is not None
    assert saved.checkpoint is not None
    snapshot = saved.checkpoint.extension_state["linch.workflow"]
    assert snapshot["after_seq"] > 0
    assert len(snapshot["records"]) == 4  # the 4 steps; the agent came later

    store.after_seqs.clear()
    resumed = await agent.run_workflow(flow, run_id="wf-snap-1", resume={"ship": True})

    assert resumed is True
    assert store.after_seqs == [snapshot["after_seq"]]  # tail only
    assert side_effects == ["s0", "s1", "s2", "s3"]  # nothing re-executed
    assert provider.calls == 1  # the agent_end in the tail replayed


async def test_snapshot_survives_a_suspend() -> None:
    from linch.workflow import WorkflowSuspended

    store = _recording_store()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    side_effects: list[str] = []

    async def flow(wf: Any) -> Any:
        await wf.step("publish", lambda: side_effects.append("published") or "ok")
        await wf.step("notify", lambda: side_effects.append("notified") or "ok")
        return await wf.interrupt("approve")

    with pytest.raises(WorkflowSuspended):
        await agent.run_workflow(flow, run_id="wf-snap-2", journal_snapshot_every=2)

    saved = await store.load_run("wf-snap-2")
    assert saved is not None
    assert saved.status == "suspended"
    assert saved.checkpoint is not None
    # The suspend checkpoint must carry the snapshot forward, not wipe it.
    assert len(saved.checkpoint.extension_state["linch.workflow"]["records"]) == 2

    assert await agent.run_workflow(flow, run_id="wf-snap-2", resume={"approve": "yes"}) == "yes"
    assert side_effects == ["published", "notified"]


async def test_an_oversized_snapshot_is_skipped_not_stored(monkeypatch: Any) -> None:
    """A journal too big to checkpoint falls back to folding the event log."""
    from linch.workflow import WorkflowSuspended
    from linch.workflow import context as wf_context

    store = _recording_store()
    agent = _make_agent(CountingTextProvider(), run_store=store)
    monkeypatch.setattr(wf_context, "_SNAPSHOT_MAX_BYTES", 512)

    async def flow(wf: Any) -> Any:
        await wf.step("big", lambda: "x" * 2048)
        return await wf.interrupt("approve")

    with pytest.raises(WorkflowSuspended):
        await agent.run_workflow(flow, run_id="wf-snap-3", journal_snapshot_every=1)

    saved = await store.load_run("wf-snap-3")
    assert saved is not None
    assert saved.checkpoint is not None
    assert "linch.workflow" not in saved.checkpoint.extension_state

    store.after_seqs.clear()
    assert await agent.run_workflow(flow, run_id="wf-snap-3", resume={"approve": "yes"}) == "yes"
    assert store.after_seqs == [0]  # no watermark, so the whole log is folded


def test_workflow_event_round_trips_every_kind() -> None:
    """Every declared kind must survive the codec — a kind added to the Literal
    without a matching decoder entry silently degrades to ``phase``."""
    from linch.events import (
        WORKFLOW_EVENT_KINDS,
        WorkflowEvent,
        event_from_dict,
        event_to_dict,
    )

    for kind in WORKFLOW_EVENT_KINDS:
        event = WorkflowEvent(
            kind=kind,  # type: ignore[arg-type]
            title="summarizer",
            call_key="abc123",
            occurrence=2,
            subagent_type="_default",
            result_text="done",
        )

        assert event_from_dict(event_to_dict(event)) == event


async def test_step_replays_on_resume_without_reexecuting() -> None:
    from linch.run_store import InMemoryRunStore

    calls: list[int] = []

    async def flow(wf: Any) -> int:
        async def work() -> int:
            calls.append(1)
            return 42

        return await wf.step("work", work)

    agent = _make_agent(CountingTextProvider(), run_store=InMemoryRunStore())

    assert await agent.run_workflow(flow, run_id="wf-step-1") == 42
    assert calls == [1]

    seen: list[Any] = []
    assert await agent.run_workflow(flow, run_id="wf-step-1", on_event=seen.append) == 42

    assert calls == [1]
    replays = [e for e in seen if e.type == "workflow" and e.kind == "step_replayed"]
    assert len(replays) == 1


async def test_step_and_agent_prefix_replay_together_after_failure(tmp_path: Path) -> None:
    """The double-execution hole: a resumed workflow must re-run neither its
    journaled agent calls nor its journaled code steps."""
    from linch import SqliteRunStore
    from linch.workflow import WorkflowError

    store_path = str(tmp_path / "runs.db")
    side_effects: list[str] = []

    async def flow(wf: Any) -> list[Any]:
        async def publish() -> str:
            side_effects.append("published")
            return "ok"

        first = await wf.agent("step one")
        published = await wf.step("publish", publish)
        second = await wf.agent("step two")
        return [first, published, second]

    provider1 = CountingTextProvider(fail_on_call=2)
    agent1 = _make_agent(provider1, run_store=SqliteRunStore(store_path))
    with pytest.raises(WorkflowError):
        await agent1.run_workflow(flow, run_id="wf-mixed-1")
    assert side_effects == ["published"]

    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=SqliteRunStore(store_path))
    result = await agent2.run_workflow(flow, run_id="wf-mixed-1")

    assert side_effects == ["published"]  # ran exactly once across both attempts
    assert provider2.calls == 1
    assert result == ["result-1", "ok", "result-1"]


async def test_step_in_a_loop_replays_per_occurrence() -> None:
    from linch.run_store import InMemoryRunStore

    calls: list[int] = []

    async def flow(wf: Any) -> list[int]:
        out: list[int] = []
        for i in range(3):

            async def tick(i: int = i) -> int:
                calls.append(i)
                return i * 10

            out.append(await wf.step("tick", tick))
        return out

    agent = _make_agent(CountingTextProvider(), run_store=InMemoryRunStore())

    assert await agent.run_workflow(flow, run_id="wf-loop-1") == [0, 10, 20]
    assert calls == [0, 1, 2]

    seen: list[Any] = []
    assert await agent.run_workflow(flow, run_id="wf-loop-1", on_event=seen.append) == [0, 10, 20]

    assert calls == [0, 1, 2]
    replays = [e for e in seen if e.type == "workflow" and e.kind == "step_replayed"]
    assert [e.occurrence for e in replays] == [0, 1, 2]


async def test_adding_a_step_does_not_invalidate_the_existing_agent_journal(
    tmp_path: Path,
) -> None:
    """Step keys live in their own hash domain, so introducing wf.step must not
    perturb the call_keys of agent calls already journaled by an in-flight run."""
    from linch import SqliteRunStore

    store_path = str(tmp_path / "runs.db")

    async def flow_v1(wf: Any) -> list[str]:
        return [await wf.agent("step one"), await wf.agent("step two")]

    provider1 = CountingTextProvider()
    agent1 = _make_agent(provider1, run_store=SqliteRunStore(store_path))
    await agent1.run_workflow(flow_v1, run_id="wf-add-step-1")
    assert provider1.calls == 2

    async def flow_v2(wf: Any) -> list[Any]:
        note = await wf.step("note", lambda: "added later")
        return [note, await wf.agent("step one"), await wf.agent("step two")]

    provider2 = CountingTextProvider()
    agent2 = _make_agent(provider2, run_store=SqliteRunStore(store_path))
    result = await agent2.run_workflow(flow_v2, run_id="wf-add-step-1")

    assert provider2.calls == 0
    assert result == ["added later", "result-1", "result-2"]
