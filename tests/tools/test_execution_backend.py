from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider


def _agent(tmp_path, **overrides):
    return make_agent(
        ScriptedProvider([]),
        model="claude-opus-4-8",
        cwd=str(tmp_path),
        run_store=None,
        **overrides,
    )


def _make_ctx(tmp_path):
    from linch.tools.base import ToolContext

    return ToolContext(
        cwd=str(tmp_path),
        session_id="s",
        run_id="r",
        session_store=None,
    )


def _docker_usable() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        completed = subprocess.run(
            [docker, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@pytest.mark.parametrize(
    ("command", "returncode", "stdout"), [("echo hello", 0, "hello"), ("exit 42", 42, "")]
)
@pytest.mark.asyncio(loop_scope="module")
async def test_local_backend_reports_command_outcome(
    tmp_path, command: str, returncode: int, stdout: str
) -> None:
    from linch.tools.execution import LocalBackend

    backend = LocalBackend()
    result = await backend.run(command, cwd=str(tmp_path), timeout_s=10.0)
    assert result.returncode == returncode
    if stdout:
        assert stdout in result.stdout
    assert result.timed_out is False


@pytest.mark.asyncio(loop_scope="module")
async def test_local_backend_timeout_kills_and_flags(tmp_path) -> None:
    from linch.tools.execution import LocalBackend

    backend = LocalBackend()
    result = await backend.run("sleep 60", cwd=str(tmp_path), timeout_s=0.1)
    assert result.timed_out is True


@pytest.mark.asyncio(loop_scope="module")
async def test_local_backend_abort_interrupts_running_command(tmp_path) -> None:
    """Aborting local execution preserves its partial output and marks the result."""
    from linch.abort import AbortContext
    from linch.tools.execution import LocalBackend

    backend = LocalBackend()
    signal = AbortContext()
    task = asyncio.create_task(
        backend.run("sleep 60", cwd=str(tmp_path), timeout_s=10.0, signal=signal)
    )
    await asyncio.sleep(0.05)
    signal.abort()

    result = await asyncio.wait_for(task, timeout=4.0)
    assert result.aborted and not result.timed_out


@pytest.mark.asyncio(loop_scope="module")
async def test_bash_tool_defaults_to_local_backend(tmp_path) -> None:
    from linch.tools.builtin import BashTool

    tool = BashTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"command": "echo hi"})
    result = await tool.execute(validated, ctx)
    assert result.is_error is False
    assert "hi" in result.content
    assert result.summary == "Exited 0"


class _FakeBackend:
    """Records the command, returns canned result."""

    def __init__(self):
        self.calls: list[str] = []

    async def run(self, command: str, *, cwd: str, timeout_s: float, signal=None):
        from linch.tools.execution import ExecResult

        self.calls.append(command)
        return ExecResult(stdout="fake-out", stderr="", returncode=0, timed_out=False)


def _fake_world(shell: _FakeBackend):
    from linch.execution import RemoteExecutionBackend
    from linch.filesystem import StateFileBackend

    return RemoteExecutionBackend(shell=shell, fs=StateFileBackend())


@pytest.mark.asyncio(loop_scope="module")
async def test_bash_tool_timeout_returns_error_result(tmp_path) -> None:
    from linch.tools.builtin import BashTool

    tool = BashTool()
    ctx = _make_ctx(tmp_path)
    # 1 ms timeout — forces timed_out=True path
    validated = tool.validate({"command": "sleep 60", "timeout_ms": 1})
    result = await tool.execute(validated, ctx)
    assert result.is_error and "timed out" in result.content


@pytest.mark.asyncio(loop_scope="module")
async def test_bash_tool_negative_timeout_runs_command(tmp_path) -> None:
    """A negative timeout_ms must be floored to a positive value so the command runs."""
    from linch.tools.builtin import BashTool

    tool = BashTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"command": "echo ran", "timeout_ms": -1})
    assert validated["timeout_ms"] > 0
    result = await tool.execute(validated, ctx)
    assert result.is_error is False
    assert "ran" in result.content


@pytest.mark.asyncio(loop_scope="module")
async def test_docker_backend_missing_docker_raises_on_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import linch.tools.execution as execution_mod
    from linch.errors import ToolExecutionError
    from linch.tools.execution import DockerBackend

    monkeypatch.setattr(execution_mod.shutil, "which", lambda _: None)
    backend = DockerBackend()
    with pytest.raises(ToolExecutionError, match="docker"):
        await backend.run("echo hi", cwd=str(tmp_path), timeout_s=10.0)


def test_docker_backend_missing_docker_does_not_raise_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import linch.tools.execution as execution_mod
    from linch.tools.execution import DockerBackend

    monkeypatch.setattr(execution_mod.shutil, "which", lambda _: None)

    DockerBackend()


def test_docker_backend_invalid_workspace_mount_raises() -> None:
    from linch.tools.execution import DockerBackend

    with pytest.raises(ValueError, match="workspace_mount"):
        DockerBackend(docker_path="/usr/bin/docker", workspace_mount="bad")  # type: ignore[arg-type]


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.skipif(not _docker_usable(), reason="docker daemon not available")
async def test_docker_backend_smoke(tmp_path) -> None:
    from linch.tools.execution import DockerBackend

    image = subprocess.run(
        ["docker", "image", "inspect", "python:3.12-slim"],
        capture_output=True,
        check=False,
    )
    if image.returncode:
        pytest.skip("Docker smoke image not cached; tests do not pull images")
    backend = DockerBackend()
    result = await backend.run("echo docker-ok", cwd=str(tmp_path), timeout_s=30.0)
    assert result.returncode == 0
    assert "docker-ok" in result.stdout


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_replaces_bash_with_backend(tmp_path) -> None:
    from linch import workspace_tools
    from linch.tools.builtin import BashTool

    fake = _FakeBackend()
    world = _fake_world(fake)

    agent = _agent(
        tmp_path,
        tools=workspace_tools(),
        execution_backend=world,
    )
    bash_tool = agent.tools.get("Bash")
    assert bash_tool is not None
    assert isinstance(bash_tool, BashTool)
    # Confirm the fake backend is wired in
    ctx = _make_ctx(tmp_path)
    validated = bash_tool.validate({"command": "echo wired"})
    result = await bash_tool.execute(validated, ctx)
    assert fake.calls == ["echo wired"]
    assert result.is_error is False


def _block_texts(agent) -> list[str]:
    return [b.text for b in agent.system_blocks if hasattr(b, "text")]


def test_system_prompt_is_neutral_without_workspace_tools(tmp_path) -> None:
    agent = _agent(tmp_path)
    combined = "\n".join(_block_texts(agent))
    assert "There is no sandbox" not in combined
    assert "Tools available: none" in combined


def test_system_prompt_sandbox_note_when_backend_injected(tmp_path) -> None:
    fake = _FakeBackend()
    world = _fake_world(fake)
    from linch import workspace_tools

    agent = _agent(
        tmp_path,
        tools=workspace_tools(),
        execution_backend=world,
    )
    combined = "\n".join(_block_texts(agent))
    assert "There is no sandbox" not in combined
    assert "sandbox" in combined.lower()


def test_execution_backend_not_injected_into_restricted_registry(tmp_path) -> None:
    """execution_backend must not grant shell access to a registry that deliberately omits Bash."""
    fake = _FakeBackend()
    world = _fake_world(fake)
    from linch.tools import ToolRegistry

    no_bash = ToolRegistry()  # intentionally empty — caller wants no shell
    agent = _agent(
        tmp_path,
        tools=no_bash,
        execution_backend=world,
    )
    assert agent.tools.get("Bash") is None, (
        "execution_backend must not inject BashTool into a registry that did not include it"
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_abort_context_wait_unblocks_on_abort() -> None:
    """AbortContext.wait() returns once the signal is aborted."""
    from linch.abort import AbortContext

    signal = AbortContext()

    async def _abort_soon() -> None:
        await asyncio.sleep(0.01)
        signal.abort()

    asyncio.create_task(_abort_soon())
    await asyncio.wait_for(signal.wait(), timeout=1.0)
    assert signal.aborted
