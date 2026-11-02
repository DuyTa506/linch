from __future__ import annotations

from typing import Any, get_type_hints

import pytest

from linch.execution import (
    ExecutionBackend,
    LocalExecutionBackend,
    RemoteExecutionBackend,
    ShellBackend,
)
from linch.filesystem import StateFileBackend
from linch.providers.base import BaseProvider
from linch.tools.base import ToolContext, ToolResult
from linch.tools.builtin import BashTool
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry

_HOST_POSTURE_LINE = "Bash runs in the user's environment with full permissions"


class _Provider(BaseProvider):
    id = "execution-test"

    def __init__(self) -> None:
        self.close_calls = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any):
        if False:
            yield req

    async def aclose(self) -> None:
        self.close_calls += 1


def _agent(tmp_path: Any = ".", *, provider: Any = None, **kwargs: Any) -> Any:
    from tests.support.agents import make_agent

    options = {
        "model": "test",
        "provider": provider or _Provider(),
        "cwd": str(tmp_path),
        "result_offload": None,
        "run_store": None,
        "permissions": None,
    }
    options.update(kwargs)
    return make_agent(options.pop("provider"), **options)


@pytest.mark.parametrize("kind", ["local", "remote", "confined"])
def test_execution_backend_transports(kind: str) -> None:
    if kind == "local":
        backend = LocalExecutionBackend()
        assert isinstance(backend, ExecutionBackend)
        assert isinstance(backend.shell, ShellBackend)
        assert backend.fs is not None
        return
    shell = _FakeShell()
    fs = StateFileBackend()
    backend = RemoteExecutionBackend(
        shell=shell,
        fs=fs,
        confinement=(
            {"boundary": "remote-sandbox", "network": "none"} if kind == "confined" else None
        ),
    )
    assert isinstance(backend, ExecutionBackend)
    assert backend.shell is shell
    assert backend.fs is fs
    assert backend.sandboxed is (kind == "confined")
    if kind == "remote":
        assert getattr(backend, "security_posture", None) in (None, "unverified")


def test_public_execution_annotations_resolve_at_runtime() -> None:
    from linch import Agent

    targets = (
        Agent.__init__,
        Agent.session,
        ShellBackend.run,
        ExecutionBackend,
        LocalExecutionBackend.__init__,
        RemoteExecutionBackend.__init__,
    )
    for target in targets:
        assert get_type_hints(target)


@pytest.mark.asyncio
async def test_local_shell_runs_command() -> None:
    backend = LocalExecutionBackend()
    result = await backend.shell.run("echo hello", cwd=".", timeout_s=10.0)
    assert result.returncode == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_local_fs_and_shell_share_workspace(tmp_path: Any) -> None:
    backend = LocalExecutionBackend(cwd=tmp_path)
    await backend.fs.write("/a.txt", "content")
    assert await backend.fs.read("/a.txt") == "content"
    result = await backend.shell.run("cat a.txt", cwd=str(tmp_path), timeout_s=10.0)
    assert result.stdout == "content"


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
@pytest.mark.asyncio
async def test_local_world_virtual_write_tools_honor_path_rule(
    tmp_path: Any, tool_name: str
) -> None:
    from linch import FeatureFlags, LocalExecutionBackend, PathRule, ToolCallEndEvent
    from linch.filesystem import filesystem_tools
    from linch.tools.registry import workspace_tools

    secret = tmp_path / "secrets" / "deploy.key"
    secret.parent.mkdir()
    secret.write_text("ORIGINAL", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name=tool_name,
                tool_input=(
                    {"path": "/secrets/deploy.key", "content": "PWNED"}
                    if tool_name == "write_file"
                    else {
                        "path": "/secrets/deploy.key",
                        "old_string": "ORIGINAL",
                        "new_string": "PWNED",
                    }
                ),
            ),
            TextTurn("done"),
        ]
    )
    agent = _agent(
        tmp_path,
        provider=provider,
        tools=workspace_tools(extra=filesystem_tools()),
        features=FeatureFlags(filesystem=True),
        execution_backend=LocalExecutionBackend(cwd=tmp_path),
        permissions={
            "mode": "skip-dangerous",
            "rules": [PathRule(["secrets/**"], "deny")],
        },
        read_before_write=False,
    )
    session = await agent.session()
    events = [event async for event in session.run("go")]
    await agent.close()

    end = next(
        event
        for event in events
        if isinstance(event, ToolCallEndEvent) and event.tool_name == tool_name
    )
    assert end.is_error
    assert secret.read_text(encoding="utf-8") == "ORIGINAL"


class _FakeShell:
    async def run(self, command: str, *, cwd: str, timeout_s: float, signal: Any = None) -> Any:
        from linch.tools.execution import ExecResult

        return ExecResult(stdout=f"remote:{command}", stderr="", returncode=0, timed_out=False)


@pytest.mark.parametrize("remote", [True, False])
@pytest.mark.asyncio
async def test_bash_tool_backend_selection(remote: bool) -> None:
    backend = RemoteExecutionBackend(shell=_FakeShell(), fs=StateFileBackend()) if remote else None
    tool = BashTool(execution=backend)
    ctx = ToolContext(cwd=".", session_id="s", run_id="r", session_store=None)
    command = "ls" if remote else "echo hi"
    result = await tool.execute({"command": command, "timeout_ms": 5000}, ctx)
    assert ("remote:ls" if remote else "hi") in result.content


async def test_agent_owns_context_and_registers_pipeline(tmp_path: Any) -> None:
    from linch import Context
    from linch.tools.pipeline import ToolPipeline

    context = Context(label="injected")
    pipeline = ToolPipeline()
    agent = _agent(tmp_path, context=context, tool_pipeline=pipeline)

    assert agent.context is not context
    assert agent.context.events is context.events
    assert agent.tool_pipeline is pipeline
    assert agent.context.tool_pipeline is pipeline

    await agent.close()
    assert agent.context.get("tool_pipeline") is None


async def test_context_disposal_is_retryable_agent_teardown(tmp_path: Any) -> None:
    from linch import Context

    parent_context = Context(label="parent")
    provider = _Provider()
    agent = _agent(tmp_path, provider=provider, context=parent_context)
    dispose_calls = 0
    real_dispose = agent.context.dispose

    async def _flaky_dispose() -> None:
        nonlocal dispose_calls
        dispose_calls += 1
        if dispose_calls == 1:
            raise RuntimeError("transient context teardown")
        await real_dispose()

    agent.context.dispose = _flaky_dispose  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="transient context teardown"):
        await agent.close()
    assert provider.close_calls == 1
    assert agent.context.get("tool_pipeline") is agent.tool_pipeline

    await agent.close()
    assert provider.close_calls == 1
    assert dispose_calls == 2
    assert agent.context.get("tool_pipeline") is None


async def test_agent_unified_world_wires_bash_context_and_session_fs(tmp_path: Any) -> None:
    from linch import FeatureFlags, workspace_tools

    fs = StateFileBackend()
    world = RemoteExecutionBackend(shell=_FakeShell(), fs=fs)
    agent = _agent(
        tmp_path,
        tools=workspace_tools(),
        features=FeatureFlags(filesystem=True),
        execution_backend=world,
    )
    session = await agent.session()

    assert agent.execution_backend is world
    assert agent.context.execution is world
    assert session.filesystem is fs
    bash = agent.tools.get("Bash")
    assert bash is not None
    result = await bash.execute(
        {"command": "pwd", "timeout_ms": 1000},
        ToolContext(
            cwd=str(tmp_path),
            session_id=session.id,
            run_id="r",
            session_store=session.store,
            filesystem=session.filesystem,
        ),
    )
    assert isinstance(result, ToolResult)
    assert result.content == "remote:pwd\n[Exited 0]"


async def test_explicit_filesystem_conflict_with_world_fails_closed(tmp_path: Any) -> None:
    from linch import ConfigError, FeatureFlags

    world_fs = StateFileBackend()
    explicit_fs = StateFileBackend()
    world = RemoteExecutionBackend(shell=_FakeShell(), fs=world_fs)
    with pytest.raises(ConfigError, match=r"filesystem=.*conflicts.*execution_backend.fs"):
        _agent(
            tmp_path,
            features=FeatureFlags(filesystem=True),
            execution_backend=world,
            filesystem=explicit_fs,
        )
    assert world.fs is world_fs


@pytest.mark.parametrize("posture", ["unverified", "docker", "explicit", "local"])
def test_remote_prompt_postures(tmp_path: Any, posture: str) -> None:
    from linch import workspace_tools
    from linch.tools.execution import DockerBackend

    if posture == "local":
        world = LocalExecutionBackend(cwd=str(tmp_path))
        assert world.security_posture == "host"
        agent = _agent(tmp_path, tools=workspace_tools(), execution_backend=world)
        assert agent._bash_security_mode() == "host"
        combined = "\n".join(block.text for block in agent.system_blocks)
        assert _HOST_POSTURE_LINE in combined
        assert "has not declared sandbox confinement" not in combined
        assert "declares sandbox confinement" not in combined
        return
    if posture == "unverified":
        world = RemoteExecutionBackend(shell=_FakeShell(), fs=StateFileBackend())
        expected = ("has not declared sandbox confinement",)
        absent = ("declares sandbox confinement", _HOST_POSTURE_LINE)
    elif posture == "docker":
        shell = DockerBackend(image="alpine", network="none")
        assert shell.confinement
        assert shell.confinement["kind"] == "docker"
        assert shell.confinement["image"] == "alpine"
        assert shell.confinement["network"] == "none"
        world = RemoteExecutionBackend(
            shell=shell, fs=StateFileBackend(), confinement=shell.confinement
        )
        expected = ("declares sandbox confinement",)
        absent = ("has not declared sandbox confinement", _HOST_POSTURE_LINE)
    else:
        world = RemoteExecutionBackend(
            shell=_FakeShell(), fs=StateFileBackend(), confinement={"boundary": "remote-sandbox"}
        )
        expected = (
            "declares sandbox confinement",
            "not an independently verified security boundary",
        )
        absent = ("has not declared sandbox confinement", _HOST_POSTURE_LINE)
    combined = "\n".join(
        block.text
        for block in _agent(
            tmp_path, tools=workspace_tools(), execution_backend=world
        ).system_blocks
    )
    assert all(text in combined for text in expected)
    assert all(text not in combined for text in absent)


async def test_subagent_inherits_parent_execution_filesystem(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linch import FeatureFlags
    from linch.subagents import runner
    from linch.subagents.runner import RunSubagentArgs, RunSubagentResult
    from linch.subagents.types import AgentDefinition, AgentFrontmatter

    fs = StateFileBackend()
    agent = _agent(
        tmp_path,
        features=FeatureFlags(filesystem=True),
        execution_backend=RemoteExecutionBackend(shell=_FakeShell(), fs=fs),
    )
    parent = await agent.session(id="parent")
    seen: dict[str, Any] = {}

    async def _fake_drive(child_session: Any, prompt: str, **kwargs: Any) -> Any:
        seen["filesystem"] = child_session.filesystem
        return RunSubagentResult(
            child_session_id=child_session.id,
            final_text="done",
            aborted=False,
            errored=False,
        )

    monkeypatch.setattr(runner, "_drive_child", _fake_drive)
    definition = AgentDefinition(
        name="worker",
        file_path="built-in",
        source="built-in",
        frontmatter=AgentFrontmatter(name="worker", description="worker"),
        body="work",
    )
    await runner.run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=definition,
            prompt="go",
            display_name="worker",
            subagent_run_id="sa-1",
        )
    )

    assert seen["filesystem"] is fs


async def test_scheduler_injects_execution_world_into_custom_tool(tmp_path: Any) -> None:
    seen: list[Any] = []

    async def capture(_input: dict[str, object], ctx: ToolContext) -> None:
        seen.append(ctx.execution)

    registry = tool_registry(StubTool("capture_execution", on_execute=capture, parallel=True))
    world = RemoteExecutionBackend(shell=_FakeShell(), fs=StateFileBackend())
    agent = _agent(
        tmp_path,
        provider=ScriptedProvider([ToolUseTurn("capture_execution", {}), TextTurn("done")]),
        tools=registry,
        execution_backend=world,
    )

    _ = [event async for event in (await agent.session()).run("capture")]
    assert seen == [world]


def test_local_world_must_match_agent_cwd(tmp_path: Any) -> None:
    from linch import ConfigError

    world = LocalExecutionBackend(cwd=tmp_path / "world-a")
    with pytest.raises(ConfigError, match=r"LocalExecutionBackend cwd.*Agent cwd"):
        _agent(tmp_path / "world-b", execution_backend=world)


def test_local_world_rejects_unrelated_in_memory_filesystem(tmp_path: Any) -> None:
    from linch.filesystem.backend import StateFileBackend

    with pytest.raises(ValueError, match=r"DiskFileBackend rooted at cwd"):
        LocalExecutionBackend(cwd=tmp_path, fs=StateFileBackend())


async def test_owned_world_close_retries_and_deduplicates_hook_fs(tmp_path: Any) -> None:
    class _CloseableFs(StateFileBackend):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    class _FlakyWorld:
        def __init__(self) -> None:
            self.shell = _FakeShell()
            self.fs = _CloseableFs()
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("world close failed")
            await self.fs.aclose()

    world = _FlakyWorld()
    provider = _Provider()
    agent = _agent(
        tmp_path,
        provider=provider,
        execution_backend=world,
        hooks=[world.fs],
    )

    with pytest.raises(RuntimeError, match="world close failed"):
        await agent.close()
    assert world.close_calls == 1
    assert world.fs.close_calls == 0
    assert provider.close_calls == 0

    await agent.close()
    assert world.close_calls == 2
    assert world.fs.close_calls == 1
    assert provider.close_calls == 1


@pytest.mark.parametrize("inherited", [False, True])
def test_shell_only_execution_world_rejected_even_with_explicit_filesystem(
    tmp_path: Any, inherited: bool
) -> None:
    from linch import Context
    from linch.errors import ConfigError

    shell = _FakeShell()
    parent = Context()
    if inherited:
        parent.register("execution", shell)
    with pytest.raises(ConfigError, match="shell.*fs"):
        _agent(
            tmp_path,
            context=parent if inherited else None,
            execution_backend=None if inherited else shell,
            filesystem=StateFileBackend(),
        )


async def test_agents_share_parent_context_without_transferring_world_ownership(
    tmp_path: Any,
) -> None:
    from linch import Context

    class _SharedWorld:
        def __init__(self) -> None:
            self.shell = _FakeShell()
            self.fs = StateFileBackend()
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    parent = Context(label="host")
    world = _SharedWorld()
    parent.register("execution", world)
    first = _agent(tmp_path, context=parent, model="first")
    second = _agent(tmp_path, context=parent, model="second")

    assert first.context is not second.context
    assert first.context.execution is world
    assert second.context.execution is world
    await first.close()
    assert world.close_calls == 0
    assert parent.execution is world
    assert second.context.execution is world
    await second.close()
    assert world.close_calls == 0
    assert parent.execution is world


def test_inherited_world_error_names_the_context_not_the_kwarg(tmp_path: Any) -> None:
    from linch import ConfigError, Context

    parent = Context(label="host")
    parent.register("execution", object())  # neither {shell, fs} nor a shell backend
    with pytest.raises(ConfigError, match=r"inherited from the supplied Context"):
        _agent(tmp_path, context=parent)


def test_directly_passed_world_error_does_not_claim_inheritance(tmp_path: Any) -> None:
    from linch import ConfigError

    with pytest.raises(ConfigError) as excinfo:
        _agent(tmp_path, execution_backend=object())  # type: ignore[arg-type]
    assert "inherited from the supplied Context" not in str(excinfo.value)


def test_remote_backend_resume_confinement() -> None:
    with pytest.raises(ValueError, match="confinement"):
        RemoteExecutionBackend(
            shell=_FakeShell(),
            fs=StateFileBackend(),
            confinement={"boundary": "sandbox"},
            resume_policy_id="custom",
            resume_policy_config={"confinement": "caller-value", "other": 1},
        )
    world = RemoteExecutionBackend(
        shell=_FakeShell(),
        fs=StateFileBackend(),
        confinement={"boundary": "sandbox"},
        resume_policy_id="custom",
        resume_policy_config={"other": 1},
    )
    assert world.resume_policy_config == {"other": 1, "confinement": {"boundary": "sandbox"}}


def test_resume_policy_descriptor() -> None:
    from linch.execution.backend import resume_policy_descriptor

    class _Misconfigured:
        resume_policy_id = "test.world"
        resume_policy_version = "1"

        @property
        def resume_policy_config(self) -> dict[str, object]:
            raise ValueError("durable world with env requires resume_fingerprint_key")

    with pytest.raises(ValueError, match="requires resume_fingerprint_key"):
        resume_policy_descriptor(_Misconfigured())

    class _NoIdentity:
        pass

    class _NoConfig:
        resume_policy_id = "test.world"
        resume_policy_config = None

    assert resume_policy_descriptor(_NoIdentity()) is None
    assert resume_policy_descriptor(_NoConfig()) is None
