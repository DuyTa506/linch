from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from linch.providers.base import BaseProvider
from tests.support.tools import StubTool, tool_registry


class ToolAwareProvider(BaseProvider):
    id = "fake"

    def __init__(self, *, tool_name: str | None = None, fail_on_call: bool = False) -> None:
        self.tool_name = tool_name
        self.fail_on_call = fail_on_call
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        if self.fail_on_call:
            raise AssertionError("provider should not be called")
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self.tool_name and not _last_message_is_tool_result(req.messages):
            yield {"type": "tool_use_start", "id": "call-1", "name": self.tool_name}
            yield {"type": "tool_use_input_delta", "id": "call-1", "json_delta": "{}"}
            yield {"type": "tool_use_end", "id": "call-1"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            return
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


def _counted_tool(name: str) -> tuple[StubTool, list[None]]:
    """Build a support tool while retaining an execution count for resume tests."""
    calls: list[None] = []
    return StubTool(name, on_execute=lambda _input, _ctx: calls.append(None)), calls


class RecordingProvider(ToolAwareProvider):
    """Record the tool catalog exposed to a coordinator worker."""

    def __init__(self) -> None:
        super().__init__()
        self.tool_sets: list[set[str]] = []

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.tool_sets.append({str(tool["name"]) for tool in req.tools})
        async for event in super().stream(req):
            yield event


async def _execute_subagent(agent: Any, payload: dict[str, Any]) -> Any:
    """Execute the coordinator's Subagent tool with a run-scoped context."""

    from linch.tools import ToolContext

    session = await agent.session(id="s1")
    subagent = agent.tools.get("Subagent")
    assert subagent is not None
    return await subagent.execute(
        payload,
        ToolContext(
            cwd=agent.cwd,
            session_id=session.id,
            run_id="test-run",
            session_store=session.store,
            file_read_tracker=session.file_read_tracker,
            filesystem=session.filesystem,
        ),
    )


def _create_agent(provider: Any, tmp_path: Path, **kwargs: Any) -> Any:
    """Create the ephemeral deep agent used by focused configuration tests."""
    from linch.ext.deep_agent import create_deep_agent

    options = {
        "model": "gpt-5",
        "provider": provider,
        "cwd": str(tmp_path),
        "durable": False,
    }
    options.update(kwargs)
    return create_deep_agent(**options)


def _postgres_storage(dsn: str) -> tuple[Any, Any, Any]:
    """Build the explicit Postgres stores and routed filesystem used by persistence tests."""
    from linch import CompositeFileBackend, PostgresRunStore, StateFileBackend
    from linch.filesystem.postgres import PostgresFileBackend
    from linch.sessions.postgres import PostgresSessionStore

    return (
        PostgresSessionStore(dsn),
        PostgresRunStore(dsn),
        CompositeFileBackend(
            default=StateFileBackend(), routes={"/memories/": PostgresFileBackend(dsn)}
        ),
    )


def _last_message_is_tool_result(messages: list[Any]) -> bool:
    if not messages:
        return False
    return any(getattr(block, "type", None) == "tool_result" for block in messages[-1].content)


async def _collect(iterator: Any) -> list[Any]:
    return [event async for event in iterator]


async def _collect_until(iterator: Any, event_type: str) -> list[Any]:
    events: list[Any] = []
    async for event in iterator:
        events.append(event)
        if event.type == event_type:
            break
    return events


def test_deep_agent_is_a_package_with_prompt_and_subagents() -> None:
    from linch.ext.deep_agent import DEEP_AGENT_SUBAGENTS, DEEP_AGENT_SYSTEM_PROMPT

    names = {agent.name for agent in DEEP_AGENT_SUBAGENTS}
    assert {"researcher", "implementer"} <= names
    assert "Planning tools" in DEEP_AGENT_SYSTEM_PROMPT
    assert "Subagents" in DEEP_AGENT_SYSTEM_PROMPT
    assert "Virtual filesystem" in DEEP_AGENT_SYSTEM_PROMPT


def test_deep_agent_preserves_original_agent_defaults(tmp_path: Path) -> None:
    from linch import Agent

    agent = Agent(model="gpt-5", provider=ToolAwareProvider(), cwd=str(tmp_path))

    assert agent.run_store is None


def test_deep_agent_prompt_has_delegation_doctrine() -> None:
    from linch.ext.deep_agent import DEEP_AGENT_SYSTEM_PROMPT

    prompt = DEEP_AGENT_SYSTEM_PROMPT
    assert "Never delegate understanding" in prompt
    assert "VERDICT" in prompt
    assert "in_progress" in prompt
    assert "synthes" in prompt.lower()


def test_deep_agent_prompt_has_phase_orchestration() -> None:
    from linch.ext.deep_agent import DEEP_AGENT_SYSTEM_PROMPT

    prompt = DEEP_AGENT_SYSTEM_PROMPT.lower()
    assert "research" in prompt
    assert "implement" in prompt
    assert "parallel" in prompt


def test_deep_agent_workspace_tools_include_tasks(tmp_path: Path) -> None:
    from linch import Agent
    from linch.ext.deep_agent import DEEP_AGENT_SYSTEM_PROMPT

    agent = _create_agent(ToolAwareProvider(), tmp_path)
    assert isinstance(agent, Agent)
    assert "task" in DEEP_AGENT_SYSTEM_PROMPT.lower()
    names = {tool.name for tool in agent.tools.list()}
    assert {"TaskCreate", "TaskList", "TaskGet", "TaskUpdate"} <= names
    assert {"ls", "read_file", "write_file", "edit_file"} <= names


def test_deep_agent_custom_tools_are_copied_and_task_tools_added(tmp_path: Path) -> None:
    original = tool_registry(StubTool("SearchDocs"))
    agent = _create_agent(ToolAwareProvider(), tmp_path, tools=original)

    assert original.get("TaskCreate") is None
    assert agent.tools is not original
    assert agent.tools.get("SearchDocs") is not None
    assert agent.tools.get("TaskCreate") is not None

    custom = StubTool("TaskCreate")
    preserved = _create_agent(ToolAwareProvider(), tmp_path, tools=tool_registry(custom))
    assert preserved.tools.get("TaskCreate") is custom


def test_deep_agent_merges_system_prompt_config(tmp_path: Path) -> None:
    from linch.config import SystemPromptConfig, SystemPromptSection

    agent = _create_agent(
        ToolAwareProvider(),
        tmp_path,
        system_prompt="Caller prompt.",
        system_prompt_config=SystemPromptConfig(
            sections=[
                SystemPromptSection(
                    name="caller",
                    text="CALLER SECTION",
                    placement="after_defaults",
                    cacheable=False,
                )
            ],
            append="Config append.",
        ),
    )
    texts = [block.text for block in agent.system_blocks]
    combined = "\n".join(texts)

    assert "Deep agent operating policy" in combined
    assert "CALLER SECTION" in combined
    assert "Config append." in combined
    assert "Caller prompt." not in combined
    caller_idx = texts.index("CALLER SECTION")
    assert agent.system_blocks[caller_idx].cacheable is False


async def test_deep_agent_loads_specialized_subagents(tmp_path: Path) -> None:
    from linch.sessions import InMemorySessionStore

    agent = _create_agent(ToolAwareProvider(), tmp_path, session_store=InMemorySessionStore())
    await agent.session(id="s1")
    registry = agent.subagent_registry

    assert registry is not None
    names = {definition.name for definition in registry.list()}
    assert {"researcher", "implementer", "verification", "planner"} <= names


async def test_deep_agent_file_memory_reads_run_scoped_index(tmp_path: Path) -> None:
    from linch import FileMemoryContextBuilder, RunOptions, ScopedFileBackend, StateFileBackend
    from linch.hooks import ContextInjectionHook

    class RecordingProvider(ToolAwareProvider):
        async def stream(self, req):
            self.system = [block.text for block in req.system]
            async for event in super().stream(req):
                yield event

    provider = RecordingProvider()
    agent = _create_agent(provider, tmp_path)
    names = {tool.name for tool in agent.tools.list()}
    builders = [hook.builder for hook in agent.hooks if isinstance(hook, ContextInjectionHook)]
    assert {"read_file", "write_file", "edit_file", "ls"} <= names
    assert any(isinstance(builder, FileMemoryContextBuilder) for builder in builders)
    backend = StateFileBackend({"/users/alice/memories/MEMORY.md": "coffee.md: likes espresso"})
    try:
        session = await agent.session()
        events = await _collect(
            session.run(
                "Hello", RunOptions(deps={"filesystem": ScopedFileBackend(backend, "/users/alice")})
            )
        )
        assert events[-1].subtype == "success"
        assert any("coffee.md: likes espresso" in text for text in provider.system)
    finally:
        await agent.close()


def test_deep_agent_planner_has_no_real_disk_write_tools() -> None:
    from linch.ext.deep_agent import DEEP_AGENT_SUBAGENTS

    planner = next(a for a in DEEP_AGENT_SUBAGENTS if a.name == "planner")
    tools = planner.frontmatter.tools or []
    assert "Write" not in tools
    assert "Edit" not in tools
    assert "Bash" not in tools
    assert "write_file" not in tools
    assert "edit_file" not in tools
    assert "read_file" in tools


async def test_deep_agent_memories_persist_across_agents(pg_dsn, tmp_path: Path) -> None:
    from linch.ext.deep_agent import create_deep_agent

    # Write to /memories via a session
    session_store, run_store, filesystem = _postgres_storage(pg_dsn)
    agent1 = create_deep_agent(
        model="gpt-5",
        provider=ToolAwareProvider(),
        cwd=str(tmp_path),
        session_store=session_store,
        run_store=run_store,
        filesystem=filesystem,
    )
    session1 = await agent1.session(id="s1")
    await session1.filesystem.write("/memories/plan.md", "my plan")
    await agent1.close()

    # A second agent instance on the same cwd should read it back
    session_store, run_store, filesystem = _postgres_storage(pg_dsn)
    agent2 = create_deep_agent(
        model="gpt-5",
        provider=ToolAwareProvider(),
        cwd=str(tmp_path),
        session_store=session_store,
        run_store=run_store,
        filesystem=filesystem,
    )
    session2 = await agent2.session(id="s2")
    content = await session2.filesystem.read("/memories/plan.md")
    await agent2.close()

    assert content == "my plan"


async def test_deep_agent_explicit_postgres_stores(pg_dsn, tmp_path: Path) -> None:
    from linch import PostgresRunStore
    from linch.ext.deep_agent import create_deep_agent
    from linch.sessions.postgres import PostgresSessionStore

    session_store, run_store, filesystem = _postgres_storage(pg_dsn)
    agent = create_deep_agent(
        model="gpt-5",
        provider=ToolAwareProvider(),
        cwd=str(tmp_path),
        session_store=session_store,
        run_store=run_store,
        filesystem=filesystem,
    )
    assert isinstance(agent.session_store, PostgresSessionStore)
    assert isinstance(agent.run_store, PostgresRunStore)
    await agent.close()


async def test_deep_agent_preserves_explicit_composite_filesystem(pg_dsn, tmp_path: Path) -> None:
    from linch import CompositeFileBackend
    from linch.ext.deep_agent import create_deep_agent

    session_store, run_store, filesystem = _postgres_storage(pg_dsn)
    agent = create_deep_agent(
        model="gpt-5",
        provider=ToolAwareProvider(),
        cwd=str(tmp_path),
        session_store=session_store,
        run_store=run_store,
        filesystem=filesystem,
    )
    session = await agent.session(id="s1")
    await agent.close()
    assert isinstance(session.filesystem, CompositeFileBackend)


def test_deep_agent_respects_explicit_stores_and_durable_false(tmp_path: Path) -> None:
    from linch import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    agent = _create_agent(
        ToolAwareProvider(),
        tmp_path,
        session_store=session_store,
        run_store=run_store,
    )

    assert agent.session_store is session_store
    assert agent.run_store is run_store

    ephemeral = _create_agent(ToolAwareProvider(), tmp_path)
    assert ephemeral.run_store is None


async def test_deep_agent_explicit_filesystem_respected(tmp_path: Path) -> None:
    from linch.filesystem.backend import StateFileBackend

    explicit_fs = StateFileBackend()
    agent = _create_agent(ToolAwareProvider(), tmp_path, filesystem=explicit_fs)
    session = await agent.session(id="s1")
    await agent.close()

    assert session.filesystem is explicit_fs


# ── End Phase 0 tests ────────────────────────────────────────────────────────


# ── Phase 3: coordinator mode ─────────────────────────────────────────────────


async def test_coordinator_mode_restricts_parent_tools(tmp_path: Path) -> None:
    """Coordinator parent lacks heavy tools; workers still get full access via SubagentTool."""
    from linch.sessions import InMemorySessionStore

    agent = _create_agent(
        ToolAwareProvider(), tmp_path, coordinator=True, session_store=InMemorySessionStore()
    )
    # session() calls connect_subagents which registers Subagent/SubagentContinue
    await agent.session(id="s1")
    names = {t.name for t in agent.tools.list()}

    # Heavy tools removed from coordinator parent
    assert "Edit" not in names
    assert "Write" not in names
    assert "Bash" not in names
    # Orchestration tools present
    assert "Subagent" in names
    assert "SubagentContinue" in names
    assert "TaskStop" in names
    combined = "\n".join(block.text for block in agent.system_blocks)
    assert "coordinator" in combined.lower()
    assert "task-notification" in combined
    assert "never fabricate" in combined.lower() or "never claim" in combined.lower()
    assert "Research" in combined
    assert "Synthesis" in combined
    assert "Implementation" in combined
    assert "Verification" in combined


async def test_coordinator_worker_retains_heavy_tools(tmp_path: Path) -> None:
    """The coordinator's parent filter must not narrow an implementer child."""
    from linch.sessions import InMemorySessionStore

    provider = RecordingProvider()
    agent = _create_agent(
        provider,
        tmp_path,
        coordinator=True,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
    )
    result = await _execute_subagent(
        agent,
        {
            "description": "implementation",
            "prompt": "Inspect the project and report what should change.",
            "subagent_type": "implementer",
        },
    )

    assert result.is_error is False
    parent_tools = {tool.name for tool in agent.tools.list()}
    assert {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}.isdisjoint(parent_tools)
    assert provider.tool_sets
    assert {"Read", "Write", "Edit", "Bash", "Glob", "Grep"} <= provider.tool_sets[0]

    general = await _execute_subagent(
        agent,
        {"description": "implementation", "prompt": "Inspect the project."},
    )
    assert general.is_error is False
    assert provider.tool_sets
    assert "Skill" in provider.tool_sets[-1]
    assert "SubagentContinue" in provider.tool_sets[-1]
    assert "TaskStop" in provider.tool_sets[-1]


async def test_coordinator_worker_preserves_custom_registry_semantics(tmp_path: Path) -> None:
    from linch.sessions import InMemorySessionStore

    provider = RecordingProvider()
    original = tool_registry(StubTool("SearchDocs"))
    agent = _create_agent(
        provider,
        tmp_path,
        tools=original,
        coordinator=True,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        result_offload=None,
    )
    result = await _execute_subagent(
        agent, {"description": "search", "prompt": "Search the configured corpus."}
    )
    assert result.is_error is False
    assert {tool.name for tool in original.list()} == {"SearchDocs"}
    assert provider.tool_sets
    assert "SearchDocs" in provider.tool_sets[0]
    assert {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}.isdisjoint(provider.tool_sets[0])


def test_coordinator_mode_requires_subagents(tmp_path: Path) -> None:
    import pytest

    from linch.config import FeatureFlags
    from linch.errors import ConfigError

    with pytest.raises(ConfigError, match="requires features.subagents=True"):
        _create_agent(
            ToolAwareProvider(), tmp_path, coordinator=True, features=FeatureFlags(subagents=False)
        )


async def test_task_stop_cancels_background_worker(tmp_path: Path) -> None:
    """TaskStop cancels a running background worker task."""
    import asyncio

    from linch.sessions import InMemorySessionStore
    from linch.subagents.types import AgentDefinition, AgentFrontmatter
    from linch.subagents.workers import WorkerHandle
    from linch.tools.base import ToolContext

    agent = _create_agent(
        ToolAwareProvider(),
        tmp_path,
        coordinator=True,
        session_store=InMemorySessionStore(),
    )
    session = await agent.session(id="s1")

    # Set up a fake running background task
    async def _blocking() -> None:
        await asyncio.sleep(100)

    task = asyncio.create_task(_blocking())
    dummy_def = AgentDefinition(
        name="test",
        file_path="<test>",
        source="built-in",
        frontmatter=AgentFrontmatter(name="test", description="test"),
        body="",
    )
    handle = WorkerHandle(
        worker_id="agent-stop-test",
        child_session_id="child-1",
        display_name="Test Worker",
        definition=dummy_def,
        status="running",
        task=task,
    )
    session.workers["agent-stop-test"] = handle

    task_stop = agent.tools.get("TaskStop")
    assert task_stop is not None
    ctx = ToolContext(
        cwd=agent.cwd,
        session_id=session.id,
        run_id="test-run",
        session_store=session.store,
        signal=None,
        file_read_tracker=session.file_read_tracker,
        deps=None,
        filesystem=None,
    )
    result = await task_stop.execute({"task_id": "agent-stop-test"}, ctx)
    await asyncio.sleep(0)  # let cancellation propagate

    assert not result.is_error
    assert handle.status == "killed"
    assert task.cancelled() or task.done()


def test_non_coordinator_mode_preserves_full_tools(tmp_path: Path) -> None:
    agent = _create_agent(ToolAwareProvider(), tmp_path)
    names = {tool.name for tool in agent.tools.list()}
    assert "Edit" in names
    assert "Bash" in names
    assert "Write" in names


# ── End Phase 3 tests ─────────────────────────────────────────────────────────


async def test_deep_agent_can_resume_with_postgres_stores(pg_dsn, tmp_path: Path) -> None:
    from linch.ext.deep_agent import create_deep_agent

    tool, _ = _counted_tool("Lookup")
    provider = ToolAwareProvider(tool_name="Lookup")
    session_store, run_store, filesystem = _postgres_storage(pg_dsn)
    agent = create_deep_agent(
        model="gpt-5",
        provider=provider,
        cwd=str(tmp_path),
        session_store=session_store,
        run_store=run_store,
        filesystem=filesystem,
        tools=None,
        durable=True,
    )
    agent.tools.replace(tool)  # type: ignore[arg-type]
    session = await agent.session(id="s1")
    events = await _collect_until(session.run("use lookup"), "assistant")
    run_id = next(event.run_id for event in events if event.type == "system")
    await agent.close()

    restarted_tool, restarted_calls = _counted_tool("Lookup")
    session_store, run_store, filesystem = _postgres_storage(pg_dsn)
    restarted = create_deep_agent(
        model="gpt-5",
        provider=ToolAwareProvider(fail_on_call=True),
        cwd=str(tmp_path),
        session_store=session_store,
        run_store=run_store,
        filesystem=filesystem,
        durable=True,
    )
    restarted.tools.replace(restarted_tool)  # type: ignore[arg-type]
    resumed = await restarted.session(id="s1")
    resume_events = await _collect_until(resumed.resume(run_id), "tool_call_end")
    await restarted.close()

    assert [event.type for event in resume_events] == ["tool_call_start", "tool_call_end"]
    assert len(restarted_calls) == 1


# ── Open-loop safety rails (ROADMAP Step 2) ─────────────────────────────────


def test_deep_agent_verifiers_are_wired_as_hook(tmp_path: Path) -> None:
    from linch.hooks import FinalAnswerVerifierHook

    class _V:
        name = "v"

        def verify(self, ctx: Any) -> Any:
            from linch.verification import Verdict

            return Verdict(action="pass")

    agent = _create_agent(ToolAwareProvider(), tmp_path, verifiers=[_V()])

    assert any(isinstance(h, FinalAnswerVerifierHook) for h in agent.hooks)


def test_deep_agent_no_verifiers_adds_no_verifier_hook(tmp_path: Path) -> None:
    from linch.hooks import FinalAnswerVerifierHook

    agent = _create_agent(ToolAwareProvider(), tmp_path)
    assert not any(isinstance(h, FinalAnswerVerifierHook) for h in agent.hooks)


def test_deep_agent_forwards_budget_and_max_turns(tmp_path: Path) -> None:
    from linch.budget import RunBudget

    budget = RunBudget(max_tokens=1000)
    agent = _create_agent(ToolAwareProvider(), tmp_path, budget=budget, max_turns=7)

    assert agent.budget is budget
    assert agent.max_turns == 7


def test_deep_agent_balanced_profile_has_safe_defaults(tmp_path: Path) -> None:
    agent = _create_agent(ToolAwareProvider(), tmp_path)

    assert agent.deep_agent_profile.name == "balanced"
    assert agent.max_turns == 64
    assert agent.budget is not None
    assert agent.budget.max_tokens == 1_000_000
    assert agent.retain_subagents is True


def test_deep_agent_unbounded_profile_requires_explicit_name(tmp_path: Path) -> None:
    agent = _create_agent(ToolAwareProvider(), tmp_path, profile="unbounded")

    assert agent.max_turns == float("inf")
    assert agent.budget is None


async def test_deep_agent_explicitly_enables_required_feature_profile(tmp_path: Path) -> None:
    from linch.filesystem.backend import CompositeFileBackend
    from linch.sessions import InMemorySessionStore

    agent = _create_agent(ToolAwareProvider(), tmp_path, session_store=InMemorySessionStore())

    assert agent.features.skills
    assert agent.features.subagents
    assert agent.features.filesystem
    assert not agent.features.mcp
    session = await agent.session(id="profile")
    assert session.filesystem is not None
    assert not isinstance(session.filesystem, CompositeFileBackend)
    assert agent.subagent_registry is not None
    assert agent.tools.get("Skill") is not None


def test_deep_agent_loop_guard_on_by_default(tmp_path: Path) -> None:
    agent = _create_agent(ToolAwareProvider(), tmp_path)

    assert agent.loop_guard is not None
