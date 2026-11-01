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


async def test_isolation_keeps_filesystem_bash_and_execution_in_one_world(tmp_path: Path) -> None:
    from linch import LocalExecutionBackend, filesystem_tools, workspace_tools
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools.isolation import TempDirIsolation

    (tmp_path / "app.py").write_text("parent", encoding="utf-8")
    observed: list[tuple[str, str]] = []

    def inspect_execution(_input: dict[str, Any], ctx: Any) -> None:
        execution = ctx.execution
        observed.append((ctx.cwd, str(getattr(getattr(execution, "fs", None), "root", ""))))

    from tests.support.tools import StubTool

    probe = StubTool("inspect_execution", on_execute=inspect_execution)
    provider = ScriptedProvider(
        [
            ToolUseTurn("write_file", {"path": "/app.py", "content": "child"}),
            ToolUseTurn("Bash", {"command": "cat app.py"}),
            ToolUseTurn("inspect_execution", {}),
            TextTurn("done"),
        ]
    )
    execution = LocalExecutionBackend(cwd=tmp_path)
    agent = make_agent(
        provider,
        model="m",
        cwd=str(tmp_path),
        tools=workspace_tools(extra=[*filesystem_tools(), probe]),
        execution_backend=execution,
    )
    parent = await agent.session()
    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="go",
            display_name="w",
            subagent_run_id="sa_iso_world",
            isolation=TempDirIsolation(source=str(tmp_path)),
        )
    )

    assert not result.errored
    assert result.final_text == "done"
    assert provider.requests[2].messages[-1].content[0].content == "child"
    assert observed and observed[0][0] != str(tmp_path)
    assert Path(observed[0][1]).resolve() == Path(observed[0][0]).resolve()
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "parent"


async def test_retained_isolated_continuation_remaps_dependency_filesystem(tmp_path: Path) -> None:
    from linch import RunOptions
    from linch.filesystem import filesystem_tools
    from linch.filesystem.disk import DiskFileBackend
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import (
        ContinueSubagentArgs,
        RunSubagentArgs,
        continue_subagent,
        run_subagent,
    )
    from linch.subagents.workers import WorkerHandle
    from linch.tools.isolation import TempDirIsolation

    parent_filesystem = DiskFileBackend(root=tmp_path / "parent-fs")
    provider = ScriptedProvider(
        [
            ToolUseTurn("write_file", {"path": "/first.txt", "content": "first"}),
            TextTurn("first"),
            ToolUseTurn("write_file", {"path": "/second.txt", "content": "second"}),
            TextTurn("second"),
        ]
    )
    agent = make_agent(
        provider,
        model="m",
        tools=tool_registry(*filesystem_tools()),
        filesystem=parent_filesystem,
    )
    parent = await agent.session()
    isolation = TempDirIsolation(source=str(tmp_path))
    options = RunOptions(deps={"filesystem": parent_filesystem})

    first = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="write the first file",
            display_name="w",
            subagent_run_id="sa_keep_continue",
            isolation=isolation,
            isolation_keep=True,
            retain=True,
            run_options=options,
        )
    )
    child = agent._sessions[first.child_session_id]
    kept = child.cwd_override
    assert kept is not None
    handle = WorkerHandle(
        worker_id="agent-kept",
        child_session_id=first.child_session_id,
        display_name="w",
        definition=DEFAULT_AGENT,
        status="completed",
    )

    try:
        second = await continue_subagent(
            ContinueSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                handle=handle,
                message="write the second file",
                subagent_run_id="sa_keep_continue_2",
                run_options=options,
            )
        )
        assert not second.errored
        assert await parent_filesystem.exists("/second.txt") is False
        child_filesystem = child.filesystem
        assert child_filesystem is not None
        assert await child_filesystem.read("/second.txt") == "second"
    finally:
        shutil.rmtree(kept, ignore_errors=True)


@pytest.mark.parametrize("backend", ["state-filesystem", "object-deps", "custom-execution"])
async def test_isolation_rejects_worlds_it_cannot_remap(tmp_path: Path, backend: str) -> None:
    from types import SimpleNamespace

    from linch import ConfigError, RunOptions
    from linch.config import FeatureFlags
    from linch.filesystem import StateFileBackend
    from linch.filesystem.disk import DiskFileBackend
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools.execution import LocalBackend
    from linch.tools.isolation import TempDirIsolation

    provider = ScriptedProvider([TextTurn("must not run")])
    options = None
    kwargs: dict[str, Any] = {}
    if backend == "state-filesystem":
        kwargs["filesystem"] = StateFileBackend()
    elif backend == "object-deps":
        options = RunOptions(deps=SimpleNamespace(filesystem=DiskFileBackend(root=tmp_path)))
    else:
        kwargs["execution_backend"] = SimpleNamespace(
            shell=LocalBackend(), fs=DiskFileBackend(root=tmp_path)
        )
    agent = make_agent(provider, run_store=None, features=FeatureFlags(filesystem=True), **kwargs)
    parent = await agent.session()
    scratch = tmp_path / "scratch"
    try:
        with pytest.raises(ConfigError, match="filesystem|execution"):
            await run_subagent(
                RunSubagentArgs(
                    parent_session=parent,
                    parent_agent=agent,
                    definition=DEFAULT_AGENT,
                    prompt="go",
                    display_name="worker",
                    subagent_run_id="unsupported-world",
                    isolation=TempDirIsolation(root=str(scratch)),
                    run_options=options,
                )
            )
        assert provider.requests == []
        assert set(agent._sessions) == {parent.id}
        assert not any(scratch.iterdir())
    finally:
        await agent.close()


async def test_isolation_preserves_custom_bash_tool(tmp_path: Path) -> None:
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools.isolation import TempDirIsolation

    calls = []
    custom_bash = StubTool(
        "Bash", output="restricted", on_execute=lambda raw, ctx: calls.append(raw)
    )
    agent = make_agent(
        ScriptedProvider([ToolUseTurn("Bash", {"command": "true"}), TextTurn("done")]),
        tools=tool_registry(custom_bash),
        run_store=None,
    )
    parent = await agent.session()
    try:
        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="go",
                display_name="worker",
                subagent_run_id="restricted-bash",
                isolation=TempDirIsolation(root=str(tmp_path)),
            )
        )
        assert not result.errored
        assert calls == [{"command": "true"}]
    finally:
        await agent.close()


async def test_isolation_rejects_standalone_nonlocal_bash(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from linch import ConfigError
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools.builtin import BashTool
    from linch.tools.isolation import TempDirIsolation

    provider = ScriptedProvider([ToolUseTurn("Bash", {"command": "true"}), TextTurn("done")])
    agent = make_agent(
        provider,
        tools=tool_registry(BashTool(execution=SimpleNamespace(shell=object()))),
        run_store=None,
    )
    parent = await agent.session()
    try:
        with pytest.raises(ConfigError, match="shell"):
            await run_subagent(
                RunSubagentArgs(
                    parent_session=parent,
                    parent_agent=agent,
                    definition=DEFAULT_AGENT,
                    prompt="go",
                    display_name="worker",
                    subagent_run_id="nonlocal-bash",
                    isolation=TempDirIsolation(root=str(tmp_path)),
                )
            )
        assert provider.requests == []
    finally:
        await agent.close()


async def test_isolation_remaps_mapping_dependencies(tmp_path: Path) -> None:
    from types import MappingProxyType

    from linch import RunOptions
    from linch.filesystem.disk import DiskFileBackend
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.tools.isolation import TempDirIsolation

    filesystem = DiskFileBackend(root=tmp_path / "parent")
    deps = MappingProxyType({"filesystem": filesystem})

    async def write_through_deps(raw, ctx):
        await ctx.deps["filesystem"].write("/note", "child")

    agent = make_agent(
        ScriptedProvider([ToolUseTurn("write_note", {}), TextTurn("done")]),
        tools=tool_registry(StubTool("write_note", on_execute=write_through_deps)),
        run_store=None,
    )
    parent = await agent.session()
    isolation = TempDirIsolation(root=str(tmp_path / "scratch"))
    try:
        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=agent,
                definition=DEFAULT_AGENT,
                prompt="go",
                display_name="worker",
                subagent_run_id="mapping-deps",
                isolation=isolation,
                isolation_keep=True,
                retain=True,
                run_options=RunOptions(deps=deps),
            )
        )
        assert not result.errored
        assert not await filesystem.exists("/note")
        child = agent._sessions[result.child_session_id]
        assert child.cwd_override is not None
        assert (Path(child.cwd_override) / "note").read_text() == "child"
        assert deps["filesystem"] is filesystem
    finally:
        for child in agent._sessions.values():
            if child.cwd_override:
                shutil.rmtree(child.cwd_override, ignore_errors=True)
        await agent.close()
