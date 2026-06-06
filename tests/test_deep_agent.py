from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from linch.providers.base import BaseProvider


class FakeProvider(BaseProvider):
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


class FakeTool:
    description = "Fake tool."
    input_schema = {"type": "object", "properties": {}}
    scope: Any = "read"
    parallel_safe = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    async def execute(self, input: dict[str, Any], ctx: Any):
        from linch.tools import ToolResult

        self.calls += 1
        return ToolResult(content=f"{self.name}:{self.calls}")

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name


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


def test_create_deep_agent_is_public_and_returns_agent(tmp_path: Path) -> None:
    from linch import DEEP_AGENT_SYSTEM_PROMPT, Agent, create_deep_agent

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
    )

    assert isinstance(agent, Agent)
    assert "task" in DEEP_AGENT_SYSTEM_PROMPT.lower()


def test_deep_agent_preserves_original_agent_defaults(tmp_path: Path) -> None:
    from linch import Agent

    agent = Agent(model="gpt-5", provider=FakeProvider(), cwd=str(tmp_path))

    assert agent.run_store is None


def test_deep_agent_default_tools_include_tasks(tmp_path: Path) -> None:
    from linch import create_deep_agent

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        durable=False,
    )
    names = {tool.name for tool in agent.tools.list()}

    assert {"TaskCreate", "TaskList", "TaskGet", "TaskUpdate"} <= names
    assert {"ls", "read_file", "write_file", "edit_file"} <= names


def test_deep_agent_is_a_package_with_prompt_and_subagents() -> None:
    from linch.deep_agent import DEEP_AGENT_SUBAGENTS, DEEP_AGENT_SYSTEM_PROMPT

    names = {agent.name for agent in DEEP_AGENT_SUBAGENTS}

    assert {"researcher", "implementer"} <= names
    assert "Planning tools" in DEEP_AGENT_SYSTEM_PROMPT
    assert "Subagents" in DEEP_AGENT_SYSTEM_PROMPT
    assert "Virtual filesystem" in DEEP_AGENT_SYSTEM_PROMPT


def test_deep_agent_custom_tools_are_copied_and_task_tools_added(tmp_path: Path) -> None:
    from linch import create_deep_agent
    from linch.tools.registry import empty_tools

    original = empty_tools(FakeTool("SearchDocs"))
    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        tools=original,
        durable=False,
    )

    assert original.get("TaskCreate") is None
    assert agent.tools is not original
    assert agent.tools.get("SearchDocs") is not None
    assert agent.tools.get("TaskCreate") is not None


def test_deep_agent_does_not_overwrite_custom_task_tool(tmp_path: Path) -> None:
    from linch import create_deep_agent
    from linch.tools.registry import empty_tools

    custom = FakeTool("TaskCreate")
    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        tools=empty_tools(custom),
        durable=False,
    )

    assert agent.tools.get("TaskCreate") is custom


def test_deep_agent_merges_system_prompt_config(tmp_path: Path) -> None:
    from linch import create_deep_agent
    from linch.config import SystemPromptConfig, SystemPromptSection

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        durable=False,
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
    from linch import create_deep_agent
    from linch.sessions import InMemorySessionStore

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        durable=False,
        session_store=InMemorySessionStore(),
    )
    await agent.session(id="s1")
    registry = agent.subagent_registry

    assert registry is not None
    names = {definition.name for definition in registry.list()}
    assert {"researcher", "implementer", "verification"} <= names


def test_deep_agent_memory_store_wires_tools_and_context_builder(tmp_path: Path) -> None:
    from linch import InMemoryKeywordMemoryStore, MemoryContextBuilder, create_deep_agent

    store = InMemoryKeywordMemoryStore()
    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        durable=False,
        memory_store=store,
        memory_namespace="deep",
    )
    names = {tool.name for tool in agent.tools.list()}
    builders = (
        agent.context_builder
        if isinstance(agent.context_builder, list)
        else [agent.context_builder]
    )

    assert {"SearchMemory", "UpsertMemory"} <= names
    assert any(isinstance(builder, MemoryContextBuilder) for builder in builders)


async def test_deep_agent_durable_defaults_create_sqlite_stores(tmp_path: Path) -> None:
    from linch import SqliteRunStore, create_deep_agent
    from linch.sessions import SqliteSessionStore

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
    )

    assert isinstance(agent.session_store, SqliteSessionStore)
    assert isinstance(agent.run_store, SqliteRunStore)
    await agent.close()


def test_deep_agent_respects_explicit_stores_and_durable_false(tmp_path: Path) -> None:
    from linch import InMemoryRunStore, create_deep_agent
    from linch.sessions import InMemorySessionStore

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        session_store=session_store,
        run_store=run_store,
        durable=False,
    )

    assert agent.session_store is session_store
    assert agent.run_store is run_store

    ephemeral = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        durable=False,
    )
    assert ephemeral.run_store is None


# ── Phase 0: deeper prompt, planner subagent, /memories filesystem ──────────


def test_deep_agent_prompt_has_delegation_doctrine() -> None:
    from linch.deep_agent import DEEP_AGENT_SYSTEM_PROMPT

    prompt = DEEP_AGENT_SYSTEM_PROMPT
    assert "Never delegate understanding" in prompt
    assert "VERDICT" in prompt
    # one in_progress discipline
    assert "in_progress" in prompt
    # synthesis (parent reads + synthesizes worker output)
    assert "synthes" in prompt.lower()


def test_deep_agent_prompt_has_phase_orchestration() -> None:
    from linch.deep_agent import DEEP_AGENT_SYSTEM_PROMPT

    prompt = DEEP_AGENT_SYSTEM_PROMPT.lower()
    assert "research" in prompt
    assert "implement" in prompt
    assert "parallel" in prompt


def test_deep_agent_planner_subagent_in_roster() -> None:
    from linch.deep_agent import DEEP_AGENT_SUBAGENTS

    names = {a.name for a in DEEP_AGENT_SUBAGENTS}
    assert "planner" in names


def test_deep_agent_planner_has_no_real_disk_write_tools() -> None:
    from linch.deep_agent import DEEP_AGENT_SUBAGENTS

    planner = next(a for a in DEEP_AGENT_SUBAGENTS if a.name == "planner")
    tools = planner.frontmatter.tools or []
    assert "Write" not in tools
    assert "Edit" not in tools
    assert "Bash" not in tools
    assert "write_file" in tools or "read_file" in tools  # can use virtual FS


async def test_deep_agent_specialized_subagents_includes_planner(tmp_path: Path) -> None:
    from linch import create_deep_agent
    from linch.sessions import InMemorySessionStore

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        durable=False,
        session_store=InMemorySessionStore(),
    )
    await agent.session(id="s1")
    names = {d.name for d in agent.subagent_registry.list()}

    assert {"researcher", "implementer", "verification", "planner"} <= names


async def test_deep_agent_durable_creates_composite_filesystem(tmp_path: Path) -> None:
    from linch import create_deep_agent
    from linch.filesystem.backend import CompositeFileBackend

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
    )
    session = await agent.session(id="s1")
    await agent.close()

    assert isinstance(session.filesystem, CompositeFileBackend)


async def test_deep_agent_memories_persist_across_agents(tmp_path: Path) -> None:
    from linch import create_deep_agent

    # Write to /memories via a session
    agent1 = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
    )
    session1 = await agent1.session(id="s1")
    await session1.filesystem.write("/memories/plan.md", "my plan")
    await agent1.close()

    # A second agent instance on the same cwd should read it back
    agent2 = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
    )
    session2 = await agent2.session(id="s2")
    content = await session2.filesystem.read("/memories/plan.md")
    await agent2.close()

    assert content == "my plan"


async def test_deep_agent_durable_false_no_persistent_filesystem(tmp_path: Path) -> None:
    from linch import create_deep_agent
    from linch.filesystem.backend import CompositeFileBackend

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        durable=False,
    )
    session = await agent.session(id="s1")

    assert not isinstance(session.filesystem, CompositeFileBackend)


async def test_deep_agent_explicit_filesystem_respected(tmp_path: Path) -> None:
    from linch import create_deep_agent
    from linch.filesystem.backend import StateFileBackend

    explicit_fs = StateFileBackend()
    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        filesystem=explicit_fs,
    )
    session = await agent.session(id="s1")
    await agent.close()

    assert session.filesystem is explicit_fs


# ── End Phase 0 tests ────────────────────────────────────────────────────────


# ── Phase 3: coordinator mode ─────────────────────────────────────────────────


def test_coordinator_mode_prompt_injected(tmp_path: Path) -> None:
    from linch import create_deep_agent

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        coordinator=True,
        durable=False,
    )
    combined = "\n".join(block.text for block in agent.system_blocks)

    # Coordinator-specific content
    assert "coordinator" in combined.lower()
    assert "task-notification" in combined
    assert "never fabricate" in combined.lower() or "never claim" in combined.lower()
    # Phase table
    assert "Research" in combined
    assert "Synthesis" in combined
    assert "Implementation" in combined
    assert "Verification" in combined


async def test_coordinator_mode_restricts_parent_tools(tmp_path: Path) -> None:
    """Coordinator parent lacks heavy tools; workers still get full access via SubagentTool."""
    from linch import create_deep_agent
    from linch.sessions import InMemorySessionStore

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        coordinator=True,
        durable=False,
        session_store=InMemorySessionStore(),
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


async def test_coordinator_mode_task_stop_registered(tmp_path: Path) -> None:
    from linch import create_deep_agent
    from linch.sessions import InMemorySessionStore

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        coordinator=True,
        durable=False,
        session_store=InMemorySessionStore(),
    )
    await agent.session(id="s1")

    assert agent.tools.get("TaskStop") is not None


def test_coordinator_mode_requires_subagents(tmp_path: Path) -> None:
    import pytest

    from linch import create_deep_agent
    from linch.config import FeatureFlags
    from linch.errors import ConfigError

    with pytest.raises(ConfigError, match="requires features.subagents=True"):
        create_deep_agent(
            model="gpt-5",
            provider=FakeProvider(),
            cwd=str(tmp_path),
            coordinator=True,
            durable=False,
            features=FeatureFlags(subagents=False),
        )


async def test_task_stop_cancels_background_worker(tmp_path: Path) -> None:
    """TaskStop cancels a running background worker task."""
    import asyncio

    from linch import create_deep_agent
    from linch.sessions import InMemorySessionStore
    from linch.subagents.types import AgentDefinition, AgentFrontmatter
    from linch.subagents.workers import WorkerHandle
    from linch.tools.base import ToolContext

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        coordinator=True,
        durable=False,
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
    """Normal deep agent retains Bash/Edit/Write."""
    from linch import create_deep_agent

    agent = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(),
        cwd=str(tmp_path),
        durable=False,
    )
    names = {t.name for t in agent.tools.list()}
    assert "Edit" in names
    assert "Bash" in names
    assert "Write" in names


# ── End Phase 3 tests ─────────────────────────────────────────────────────────


async def test_deep_agent_can_resume_with_durable_defaults(tmp_path: Path) -> None:
    from linch import create_deep_agent

    tool = FakeTool("Lookup")
    provider = FakeProvider(tool_name="Lookup")
    agent = create_deep_agent(
        model="gpt-5",
        provider=provider,
        cwd=str(tmp_path),
        tools=None,
        durable=True,
    )
    agent.tools.replace(tool)
    session = await agent.session(id="s1")
    events = await _collect_until(session.run("use lookup"), "assistant")
    run_id = next(event.run_id for event in events if event.type == "system")
    await agent.close()

    restarted_tool = FakeTool("Lookup")
    restarted = create_deep_agent(
        model="gpt-5",
        provider=FakeProvider(fail_on_call=True),
        cwd=str(tmp_path),
        durable=True,
    )
    restarted.tools.replace(restarted_tool)
    resumed = await restarted.session(id="s1")
    resume_events = await _collect_until(resumed.resume(run_id), "tool_call_end")
    await restarted.close()

    assert [event.type for event in resume_events] == ["tool_call_start", "tool_call_end"]
    assert restarted_tool.calls == 1
