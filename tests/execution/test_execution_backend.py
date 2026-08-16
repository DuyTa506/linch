"""Phase 3: the unified ExecutionBackend seam (shell + filesystem as one world)."""

from __future__ import annotations

from typing import Any, get_type_hints

import pytest

from linch.execution import (
    ExecutionBackend,
    LocalExecutionBackend,
    RemoteExecutionBackend,
    ShellBackend,
)
from linch.providers.base import BaseProvider
from linch.tools.base import ToolContext, ToolResult
from linch.tools.builtin import BashTool

# The three Bash-posture prompt branches are mutually exclusive, so each
# posture test pins the other two as absent rather than asserting one string.
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


def test_local_backend_satisfies_protocol() -> None:
    backend = LocalExecutionBackend()
    assert isinstance(backend, ExecutionBackend)
    assert isinstance(backend.shell, ShellBackend)
    assert backend.fs is not None


def test_public_execution_annotations_resolve_at_runtime() -> None:
    from linch import Agent, AgentOptions

    targets = (
        AgentOptions,
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


class _FakeShell:
    async def run(self, command: str, *, cwd: str, timeout_s: float, signal: Any = None) -> Any:
        from linch.tools.execution import ExecResult

        return ExecResult(stdout=f"remote:{command}", stderr="", returncode=0, timed_out=False)


class _FakeFs:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        return self._store[path]

    async def write(self, path: str, content: str) -> None:
        self._store[path] = content

    async def ls(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self._store if p.startswith(prefix))

    async def edit(self, path: str, old: str, new: str, *, replace_all: bool = False) -> int:
        self._store[path] = self._store[path].replace(old, new)
        return 1

    async def exists(self, path: str) -> bool:
        return path in self._store

    async def delete(self, path: str) -> None:
        self._store.pop(path, None)


def test_remote_backend_bundles_supplied_transports() -> None:
    shell = _FakeShell()
    fs = _FakeFs()
    backend = RemoteExecutionBackend(shell=shell, fs=fs)
    assert isinstance(backend, ExecutionBackend)
    assert backend.shell is shell
    assert backend.fs is fs
    assert backend.sandboxed is False


def test_remote_backend_requires_confinement_metadata_to_claim_sandbox() -> None:
    backend = RemoteExecutionBackend(
        shell=_FakeShell(),
        fs=_FakeFs(),
        confinement={"boundary": "remote-sandbox", "network": "none"},
    )
    assert backend.sandboxed is True


@pytest.mark.asyncio
async def test_bash_tool_sources_shell_from_execution_backend() -> None:
    backend = RemoteExecutionBackend(shell=_FakeShell(), fs=_FakeFs())
    tool = BashTool(execution=backend)
    ctx = ToolContext(cwd=".", session_id="s", run_id="r", session_store=None)
    result = await tool.execute({"command": "ls", "timeout_ms": 1000}, ctx)
    assert "remote:ls" in result.content


@pytest.mark.asyncio
async def test_bash_tool_default_backend_unchanged() -> None:
    tool = BashTool()
    ctx = ToolContext(cwd=".", session_id="s", run_id="r", session_store=None)
    result = await tool.execute({"command": "echo hi", "timeout_ms": 5000}, ctx)
    assert "hi" in result.content


def test_bash_tool_rejects_backend_and_execution_together() -> None:
    with pytest.raises(ValueError, match="either backend= or execution="):
        BashTool(
            backend=_FakeShell(), execution=RemoteExecutionBackend(shell=_FakeShell(), fs=_FakeFs())
        )


async def test_agent_owns_context_and_registers_pipeline(tmp_path: Any) -> None:
    from linch import Agent, Context
    from linch.sessions import InMemorySessionStore
    from linch.tools.pipeline import ToolPipeline

    context = Context(label="injected")
    pipeline = ToolPipeline()
    agent = Agent(
        model="test",
        provider=_Provider(),
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
        context=context,
        tool_pipeline=pipeline,
        result_offload=None,
    )

    assert agent.context is not context
    assert agent.context.events is context.events
    assert agent.tool_pipeline is pipeline
    assert agent.context.tool_pipeline is pipeline

    await agent.close()
    assert agent.context.get("tool_pipeline") is None


async def test_context_disposal_is_retryable_agent_teardown(tmp_path: Any) -> None:
    from linch import Agent, Context
    from linch.sessions import InMemorySessionStore

    parent_context = Context(label="parent")
    provider = _Provider()
    agent = Agent(
        model="test",
        provider=provider,
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
        context=parent_context,
        result_offload=None,
    )
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
    from linch import Agent, FeatureFlags, workspace_tools
    from linch.sessions import InMemorySessionStore

    fs = _FakeFs()
    world = RemoteExecutionBackend(shell=_FakeShell(), fs=fs)
    agent = Agent(
        model="test",
        provider=_Provider(),
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
        tools=workspace_tools(),
        features=FeatureFlags(filesystem=True),
        execution_backend=world,
        result_offload=None,
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
    assert result.content == "remote:pwd"


async def test_explicit_filesystem_conflict_with_world_fails_closed(tmp_path: Any) -> None:
    from linch import Agent, ConfigError, FeatureFlags
    from linch.sessions import InMemorySessionStore

    world_fs = _FakeFs()
    explicit_fs = _FakeFs()
    world = RemoteExecutionBackend(shell=_FakeShell(), fs=world_fs)
    with pytest.raises(ConfigError, match=r"filesystem=.*conflicts.*execution_backend.fs"):
        Agent(
            model="test",
            provider=_Provider(),
            session_store=InMemorySessionStore(),
            cwd=str(tmp_path),
            features=FeatureFlags(filesystem=True),
            execution_backend=world,
            filesystem=explicit_fs,
            result_offload=None,
        )
    assert world.fs is world_fs


def test_remote_prompt_does_not_claim_sandbox_without_metadata(tmp_path: Any) -> None:
    from linch import Agent, workspace_tools

    world = RemoteExecutionBackend(shell=_FakeShell(), fs=_FakeFs())
    agent = Agent(
        model="test",
        provider=_Provider(),
        cwd=str(tmp_path),
        tools=workspace_tools(),
        execution_backend=world,
        result_offload=None,
    )
    combined = "\n".join(block.text for block in agent.system_blocks)
    assert "has not declared sandbox confinement" in combined
    # Pin the branch: neither of the other two postures may also be described.
    assert "declares sandbox confinement" not in combined
    assert _HOST_POSTURE_LINE not in combined


def test_docker_backend_declares_its_own_confinement() -> None:
    """Linch's own Docker sandbox must declare confinement, not read as unverified."""
    from linch.tools.execution import DockerBackend

    backend = DockerBackend(image="alpine", network="none")
    confinement = backend.confinement
    assert confinement, "DockerBackend must declare non-empty confinement metadata"
    assert confinement["kind"] == "docker"
    assert confinement["image"] == "alpine"
    assert confinement["network"] == "none"


def test_docker_backend_prompt_reports_sandbox_confinement(tmp_path: Any) -> None:
    from linch import Agent, workspace_tools
    from linch.tools.execution import DockerBackend

    # DockerBackend is shell-only, so it reaches Agent via the deprecated
    # compatibility path — still the shape existing users pass today.
    with pytest.warns(DeprecationWarning, match="shell-only execution_backend"):
        agent = Agent(
            model="test",
            provider=_Provider(),
            cwd=str(tmp_path),
            tools=workspace_tools(),
            execution_backend=DockerBackend(image="alpine"),
            result_offload=None,
        )
    combined = "\n".join(block.text for block in agent.system_blocks)
    assert "declares sandbox confinement" in combined
    assert "has not declared sandbox confinement" not in combined
    assert _HOST_POSTURE_LINE not in combined


def test_remote_prompt_uses_explicit_confinement_metadata(tmp_path: Any) -> None:
    from linch import Agent, workspace_tools

    world = RemoteExecutionBackend(
        shell=_FakeShell(),
        fs=_FakeFs(),
        confinement={"boundary": "remote-sandbox"},
    )
    agent = Agent(
        model="test",
        provider=_Provider(),
        cwd=str(tmp_path),
        tools=workspace_tools(),
        execution_backend=world,
        result_offload=None,
    )
    combined = "\n".join(block.text for block in agent.system_blocks)
    assert "declares sandbox confinement" in combined
    assert "not an independently verified security boundary" in combined
    assert "has not declared sandbox confinement" not in combined
    assert _HOST_POSTURE_LINE not in combined


async def test_subagent_inherits_parent_execution_filesystem(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linch import Agent, FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.subagents import runner
    from linch.subagents.runner import RunSubagentArgs, RunSubagentResult
    from linch.subagents.types import AgentDefinition, AgentFrontmatter

    fs = _FakeFs()
    agent = Agent(
        model="test",
        provider=_Provider(),
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
        features=FeatureFlags(filesystem=True),
        execution_backend=RemoteExecutionBackend(shell=_FakeShell(), fs=fs),
        result_offload=None,
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
    from linch import Agent, ToolRegistry
    from linch.sessions import InMemorySessionStore

    seen: list[Any] = []

    class _CaptureTool:
        name = "capture_execution"
        description = "capture execution"
        input_schema = {"type": "object", "properties": {}}
        scope = "read"
        parallel = True

        def validate(self, raw: dict[str, object]) -> dict[str, object]:
            return raw

        async def execute(self, input: dict[str, object], ctx: ToolContext) -> ToolResult:
            seen.append(ctx.execution)
            return ToolResult(content="captured")

        def summarize(self, input: dict[str, object]) -> str:
            return "capture"

    class _ToolProvider(BaseProvider):
        id = "tool-provider"

        def __init__(self) -> None:
            self.calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any):
            from linch.types import Usage

            self.calls += 1
            yield {"type": "message_start", "model": req.model}
            if self.calls == 1:
                yield {"type": "tool_use_start", "id": "t1", "name": "capture_execution"}
                yield {"type": "tool_use_input_delta", "id": "t1", "json_delta": "{}"}
                yield {"type": "tool_use_end", "id": "t1"}
                stop_reason = "tool_use"
            else:
                yield {"type": "text_delta", "text": "done"}
                stop_reason = "end_turn"
            yield {"type": "message_end", "stop_reason": stop_reason, "usage": Usage()}

    registry = ToolRegistry()
    registry.register(_CaptureTool())  # type: ignore[arg-type]
    world = RemoteExecutionBackend(shell=_FakeShell(), fs=_FakeFs())
    agent = Agent(
        model="test",
        provider=_ToolProvider(),
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
        tools=registry,
        execution_backend=world,
        result_offload=None,
    )

    _ = [event async for event in (await agent.session()).run("capture")]
    assert seen == [world]


def test_local_world_must_match_agent_cwd(tmp_path: Any) -> None:
    from linch import Agent, ConfigError

    world = LocalExecutionBackend(cwd=tmp_path / "world-a")
    with pytest.raises(ConfigError, match=r"LocalExecutionBackend cwd.*Agent cwd"):
        Agent(
            model="test",
            provider=_Provider(),
            cwd=str(tmp_path / "world-b"),
            execution_backend=world,
            result_offload=None,
        )


def test_local_world_rejects_unrelated_in_memory_filesystem(tmp_path: Any) -> None:
    from linch.filesystem.backend import StateFileBackend

    with pytest.raises(ValueError, match=r"DiskFileBackend rooted at cwd"):
        LocalExecutionBackend(cwd=tmp_path, fs=StateFileBackend())


async def test_owned_world_close_retries_and_deduplicates_hook_fs(tmp_path: Any) -> None:
    from linch import Agent

    class _CloseableFs(_FakeFs):
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
    agent = Agent(
        model="test",
        provider=provider,
        cwd=str(tmp_path),
        execution_backend=world,
        hooks=[world.fs],
        result_offload=None,
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


async def test_legacy_shell_does_not_transfer_shell_ownership(tmp_path: Any) -> None:
    from linch import Agent

    class _CloseableShell(_FakeShell):
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    class _CloseableFs(_FakeFs):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    shell = _CloseableShell()
    fs = _CloseableFs()
    with pytest.warns(DeprecationWarning, match="shell-only execution_backend"):
        agent = Agent(
            model="test",
            provider=_Provider(),
            cwd=str(tmp_path),
            execution_backend=shell,
            filesystem=fs,
            result_offload=None,
        )
    await agent.close()
    assert shell.close_calls == 0
    assert fs.close_calls == 1


async def test_agents_share_parent_context_without_transferring_world_ownership(
    tmp_path: Any,
) -> None:
    from linch import Agent, Context

    class _SharedWorld:
        def __init__(self) -> None:
            self.shell = _FakeShell()
            self.fs = _FakeFs()
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    parent = Context(label="host")
    world = _SharedWorld()
    parent.register("execution", world)
    first = Agent(
        model="first",
        provider=_Provider(),
        cwd=str(tmp_path),
        context=parent,
        result_offload=None,
    )
    second = Agent(
        model="second",
        provider=_Provider(),
        cwd=str(tmp_path),
        context=parent,
        result_offload=None,
    )

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
    """An inherited world was never passed as ``execution_backend=``.

    Blaming that kwarg sends the caller to a construction site that does not
    exist; the message must point at the Context the world came from.
    """
    from linch import Agent, ConfigError, Context

    parent = Context(label="host")
    parent.register("execution", object())  # neither {shell, fs} nor a shell backend
    with pytest.raises(ConfigError, match=r"inherited from the supplied Context"):
        Agent(
            model="test",
            provider=_Provider(),
            cwd=str(tmp_path),
            context=parent,
            result_offload=None,
        )


def test_directly_passed_world_error_does_not_claim_inheritance(tmp_path: Any) -> None:
    from linch import Agent, ConfigError

    with pytest.raises(ConfigError) as excinfo:
        Agent(
            model="test",
            provider=_Provider(),
            cwd=str(tmp_path),
            execution_backend=object(),  # type: ignore[arg-type]
            result_offload=None,
        )
    assert "inherited from the supplied Context" not in str(excinfo.value)


def test_local_world_reports_host_posture_not_unverified(tmp_path: Any) -> None:
    """An explicitly local world runs on the host; that is known, not unverified.

    "unverified" means a backend is configured whose boundary Linch cannot
    describe. ``LocalExecutionBackend`` declares it runs on the host, so the
    prompt should say so rather than implying an undeclared boundary.
    """
    from linch import Agent, LocalExecutionBackend, workspace_tools

    world = LocalExecutionBackend(cwd=str(tmp_path))
    assert world.security_posture == "host"

    agent = Agent(
        model="test",
        provider=_Provider(),
        cwd=str(tmp_path),
        tools=workspace_tools(),
        execution_backend=world,
        result_offload=None,
    )
    assert agent._bash_security_mode() == "host"
    combined = "\n".join(block.text for block in agent.system_blocks)
    assert _HOST_POSTURE_LINE in combined
    assert "has not declared sandbox confinement" not in combined
    assert "declares sandbox confinement" not in combined


def test_undeclared_custom_world_still_reports_unverified() -> None:
    """A world that declares no posture and no confinement stays "unverified"."""
    world = RemoteExecutionBackend(shell=_FakeShell(), fs=_FakeFs())
    assert getattr(world, "security_posture", None) in (None, "unverified")
    assert world.sandboxed is False


def test_remote_backend_rejects_reserved_confinement_resume_key() -> None:
    """``confinement`` is owned by the seam; a caller key would be overwritten.

    ``resume_policy_config`` merges the world's confinement in under that name,
    so accepting a caller-supplied one would silently drop it from the resume
    fingerprint instead of failing.
    """
    with pytest.raises(ValueError, match="confinement"):
        RemoteExecutionBackend(
            shell=_FakeShell(),
            fs=_FakeFs(),
            confinement={"boundary": "sandbox"},
            resume_policy_id="custom",
            resume_policy_config={"confinement": "caller-value", "other": 1},
        )


def test_remote_backend_resume_config_still_merges_confinement() -> None:
    world = RemoteExecutionBackend(
        shell=_FakeShell(),
        fs=_FakeFs(),
        confinement={"boundary": "sandbox"},
        resume_policy_id="custom",
        resume_policy_config={"other": 1},
    )
    assert world.resume_policy_config == {"other": 1, "confinement": {"boundary": "sandbox"}}
