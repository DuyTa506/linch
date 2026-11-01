from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


def _agent(provider: Any, tools: Any) -> Any:
    return make_agent(provider, model="m", tools=tools)


@pytest.mark.parametrize("isolated", [True, False])
async def test_isolation_scope(isolated: bool) -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    recorded: list[str] = []
    tools = tool_registry(
        StubTool("record_cwd", on_execute=lambda _input, ctx: recorded.append(ctx.cwd))
    )
    provider = ScriptedProvider(
        [ToolUseTurn(tool_name="record_cwd", tool_input={}), TextTurn("done")]
    )
    agent = _agent(provider, tools)
    parent = await agent.session()
    kwargs: dict[str, Any] = {}
    if isolated:
        from linch.tools.isolation import TempDirIsolation

        kwargs = {"isolation": TempDirIsolation(), "retain": True}

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="go",
            display_name="w",
            subagent_run_id="sa_iso",
            **kwargs,
        )
    )

    assert len(recorded) == 1
    if isolated:
        iso_cwd = recorded[0]
        assert iso_cwd != agent.cwd
        assert not Path(iso_cwd).exists()
    else:
        assert recorded == [agent.cwd]
    assert result.final_text == "done"


async def test_isolation_keep_preserves_dir_and_artifacts() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools.isolation import TempDirIsolation

    def write_out(_input: dict[str, Any], ctx: Any) -> None:
        (Path(ctx.cwd) / "out.txt").write_text("child-wrote")

    tools = tool_registry(StubTool("write_out", on_execute=write_out))
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
        assert (Path(kept) / "out.txt").read_text() == "child-wrote"
    finally:
        shutil.rmtree(kept, ignore_errors=True)


async def test_custom_isolation_backend_slots_in_via_protocol() -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

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
    agent = _agent(provider, tool_registry())
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
