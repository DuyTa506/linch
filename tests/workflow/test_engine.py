"""Workflow engine integration tests.

linch imports happen inside test functions / provider methods (not at module
level) because tests/loop/test_hardening.py pops all ``linch*`` modules from
``sys.modules``.
"""

from __future__ import annotations

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


async def test_run_id_without_run_store_raises_config_error() -> None:
    from linch.errors import ConfigError

    agent = _make_agent(CountingTextProvider())

    async def flow(wf: Any) -> None:
        return None

    with pytest.raises(ConfigError, match="run_store"):
        await agent.run_workflow(flow, run_id="wf-1")


def test_workflow_event_round_trips_event_dict() -> None:
    from linch.events import WorkflowEvent, event_from_dict, event_to_dict

    event = WorkflowEvent(
        kind="agent_end",
        title="summarizer",
        call_key="abc123",
        occurrence=2,
        subagent_type="_default",
        result_text="done",
    )

    restored = event_from_dict(event_to_dict(event))

    assert restored == event
