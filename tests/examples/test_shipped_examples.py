"""Exercise corrected shipped examples without provider or database services."""

from pathlib import Path

from tests.support.modules import load_module

ROOT = Path(__file__).resolve().parents[2]


async def test_budget_example_executes_valid_glob_calls(monkeypatch, tmp_path):
    from linch import Agent

    module = load_module(ROOT / "examples/core/budget_capped_agent.py", "budget_example")
    tool_results = []

    class Capture:
        def on_event_emit(self, ctx):
            if ctx.event.type == "tool_call_end":
                tool_results.append(ctx.event)

    def make_agent(**kwargs):
        return Agent(**kwargs, hooks=[Capture()])

    monkeypatch.setattr(module, "Agent", make_agent)
    monkeypatch.chdir(tmp_path)
    await module.main()
    assert len(tool_results) == 3
    assert all(not event.is_error for event in tool_results)


async def test_subagent_loader_displays_explicit_empty_tools(capsys):
    module = load_module(ROOT / "examples/internals/subagent_coordinator.py", "coordinator_example")
    await module.demo_loader()
    line = next(line for line in capsys.readouterr().out.splitlines() if "[summariser]" in line)
    assert line.endswith("tools=[]")


async def test_deep_agent_example_interrupts_before_resuming(monkeypatch, tmp_path, capsys):
    import linch
    import linch.ext.deep_agent as deep_agent
    import linch.filesystem.postgres as postgres_files
    import linch.sessions.postgres as postgres_sessions
    from linch import (
        Agent,
        InMemoryRunStore,
        InMemorySessionStore,
        StateFileBackend,
        empty_tools,
        tool,
    )
    from linch.ext.evals import ScriptedProvider, TextTurn, ToolUseTurn

    module = load_module(ROOT / "examples/core/deep_agent_resume.py", "deep_resume_example")
    calls, agents, resume_statuses = [], [], []

    class TrackingRunStore(InMemoryRunStore):
        async def load_run(self, run_id):
            record = await super().load_run(run_id)
            if len(agents) == 2 and record is not None:
                resume_statuses.append(record.status)
            return record

    session_store, run_store = InMemorySessionStore(), TrackingRunStore()

    @tool(name="echo", description="Return a fixed result.", scope="read")
    async def echo() -> str:
        calls.append("echo")
        return "tool result"

    providers = iter(
        [
            ScriptedProvider([ToolUseTurn("echo", {}), TextTurn("first run completed")]),
            ScriptedProvider([TextTurn("resumed")]),
        ]
    )

    def make_agent(**kwargs):
        if agents:
            assert not calls, "the first run must stop before executing the pending tool"
        agent = Agent(
            model=kwargs["model"],
            provider=kwargs["provider"],
            tools=empty_tools(echo),
            session_store=session_store,
            run_store=run_store,
            permissions={"mode": "skip-dangerous"},
        )
        agents.append(agent)
        return agent

    monkeypatch.setenv("LINCH_POSTGRES_DSN", "unused-offline-dsn")
    monkeypatch.setattr(module, "_provider", lambda: next(providers))
    monkeypatch.setattr(deep_agent, "create_deep_agent", make_agent)
    # Supply the same in-memory stores to both agents, regardless of which
    # supported import path the example uses for the optional integrations.
    monkeypatch.setattr(linch, "PostgresRunStore", lambda dsn: run_store)
    monkeypatch.setattr(linch, "PostgresSessionStore", lambda dsn: session_store, raising=False)
    monkeypatch.setattr(
        linch, "PostgresFileBackend", lambda dsn, **_: StateFileBackend(), raising=False
    )
    monkeypatch.setattr(postgres_sessions, "PostgresSessionStore", lambda dsn: session_store)
    monkeypatch.setattr(postgres_files, "PostgresFileBackend", lambda dsn, **_: StateFileBackend())

    await module.run_durable_planning(str(tmp_path))

    assert len(agents) == 2
    assert resume_statuses[0] == "running"
    # A started tool with no completion receipt is recovered as an interrupted
    # result; resume must not blindly repeat an execution with unknown effects.
    assert calls == []
    assert "(resumed) result: resumed" in capsys.readouterr().out
