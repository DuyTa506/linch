"""run_subagent isolation wiring (ROADMAP Phase 2.2).

With ``isolation=<IsolationBackend>``, a subagent runs in its own acquired
working directory (ToolContext.cwd), so parallel branches editing the same
relative path don't collide. The scratch dir is released when the child
finishes unless ``isolation_keep=True``. Opt-in: no isolation → child uses
``agent.cwd`` as before (byte-identical).

linch imports happen inside test bodies because sibling tests pop ``linch*``
modules from ``sys.modules``.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any


def _agent(provider: Any, tools: Any) -> Any:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="m",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=tools,
    )


async def test_isolation_overrides_child_cwd_and_cleans_up() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
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
    agent = _agent(provider, tools)
    parent = await agent.session()
    iso = TempDirIsolation()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="go",
            display_name="w",
            subagent_run_id="sa_iso",
            isolation=iso,
            retain=True,
        )
    )

    assert len(recorded) == 1
    iso_cwd = recorded[0]
    assert iso_cwd != agent.cwd
    # keep=False (default) → scratch dir removed after the child finishes.
    assert not Path(iso_cwd).exists()
    # The override is cleared/stale; result still returns the child's text.
    assert result.final_text == "done"


async def test_isolation_keep_preserves_dir_and_artifacts() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools import ToolRegistry, tool
    from linch.tools.isolation import TempDirIsolation

    @tool
    def write_out(ctx: Any) -> str:
        """Write a file into the cwd."""
        (Path(ctx.cwd) / "out.txt").write_text("child-wrote")
        return "ok"

    tools = ToolRegistry()
    tools.register(write_out)
    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="write_out", tool_input={}), TextTurn("done")]
    )
    agent = _agent(provider, tools)
    parent = await agent.session()
    iso = TempDirIsolation()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="go",
            display_name="w",
            subagent_run_id="sa_keep",
            isolation=iso,
            isolation_keep=True,
            retain=True,
        )
    )

    child = agent._sessions[result.child_session_id]
    kept = child.cwd_override
    assert kept is not None
    try:
        # keep=True → dir and the child's artifact survive for the embedder to merge.
        assert (Path(kept) / "out.txt").read_text() == "child-wrote"
    finally:
        shutil.rmtree(kept, ignore_errors=True)


async def test_custom_isolation_backend_slots_in_via_protocol() -> None:
    from linch.evals import ScriptedProvider, TextTurn
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools import ToolRegistry

    class FakeIsolation:
        def __init__(self) -> None:
            self.acquired: list[str] = []
            self.released: list[tuple[str, bool]] = []

        async def acquire(self) -> str:
            path = tempfile.mkdtemp(prefix="fake-iso-")
            self.acquired.append(path)
            return path

        async def release(self, cwd: str, *, keep: bool = False) -> None:
            self.released.append((cwd, keep))
            shutil.rmtree(cwd, ignore_errors=True)

    provider = ScriptedProvider([TextTurn("done")])
    agent = _agent(provider, ToolRegistry())
    parent = await agent.session()
    iso = FakeIsolation()

    await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="go",
            display_name="w",
            subagent_run_id="sa_custom",
            isolation=iso,
        )
    )

    assert len(iso.acquired) == 1
    assert iso.released == [(iso.acquired[0], False)]


async def test_no_isolation_uses_agent_cwd() -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools import ToolRegistry, tool

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
    agent = _agent(provider, tools)
    parent = await agent.session()

    await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="go",
            display_name="w",
            subagent_run_id="sa_noiso",
        )
    )

    assert recorded == [agent.cwd]
