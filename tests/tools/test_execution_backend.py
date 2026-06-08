"""RED tests for Feature H — sandboxed execution backend (sub-phase 3b).

All imports are lazy (inside test bodies) to stay compatible with
test_hardening.py's sys.modules reset.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# LocalBackend unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_runs_command_returns_stdout(tmp_path) -> None:
    from linch.tools.execution import LocalBackend

    backend = LocalBackend()
    result = await backend.run("echo hello", cwd=str(tmp_path), timeout_s=10.0)
    assert result.returncode == 0
    assert "hello" in result.stdout
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_local_backend_nonzero_returncode(tmp_path) -> None:
    from linch.tools.execution import LocalBackend

    backend = LocalBackend()
    result = await backend.run("exit 42", cwd=str(tmp_path), timeout_s=10.0)
    assert result.returncode == 42
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_local_backend_timeout_kills_and_flags(tmp_path) -> None:
    from linch.tools.execution import LocalBackend

    backend = LocalBackend()
    result = await backend.run("sleep 60", cwd=str(tmp_path), timeout_s=0.1)
    assert result.timed_out is True


# ---------------------------------------------------------------------------
# BashTool with default (LocalBackend) — must be byte-identical to current
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_tool_defaults_to_local_backend(tmp_path) -> None:
    from linch.tools.builtin import BashTool

    tool = BashTool()
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"command": "echo hi"})
    result = await tool.execute(validated, ctx)
    assert result.is_error is False
    assert "hi" in result.content
    assert result.summary == "Exited 0"


# ---------------------------------------------------------------------------
# BashTool with injected fake backend
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Records the command, returns canned result."""

    def __init__(self):
        self.calls: list[str] = []

    async def run(self, command: str, *, cwd: str, timeout_s: float, signal=None):
        from linch.tools.execution import ExecResult

        self.calls.append(command)
        return ExecResult(stdout="fake-out", stderr="", returncode=0, timed_out=False)


@pytest.mark.asyncio
async def test_bash_tool_uses_injected_backend(tmp_path) -> None:
    from linch.tools.builtin import BashTool

    fake = _FakeBackend()
    tool = BashTool(backend=fake)
    ctx = _make_ctx(tmp_path)
    validated = tool.validate({"command": "echo injected"})
    result = await tool.execute(validated, ctx)
    assert fake.calls == ["echo injected"]
    assert "fake-out" in result.content
    assert result.summary == "Exited 0"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_bash_tool_timeout_raises_tool_execution_error(tmp_path) -> None:
    from linch.errors import ToolExecutionError
    from linch.tools.builtin import BashTool

    tool = BashTool()
    ctx = _make_ctx(tmp_path)
    # 1 ms timeout — forces timed_out=True path
    validated = tool.validate({"command": "sleep 60", "timeout_ms": 1})
    with pytest.raises(ToolExecutionError, match="timed out"):
        await tool.execute(validated, ctx)


# ---------------------------------------------------------------------------
# DockerBackend missing docker raises
# ---------------------------------------------------------------------------


def test_docker_backend_missing_docker_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import linch.tools.execution as execution_mod
    from linch.errors import ToolExecutionError

    monkeypatch.setattr(execution_mod.shutil, "which", lambda _: None)
    with pytest.raises(ToolExecutionError, match="docker"):
        from linch.tools.execution import DockerBackend

        DockerBackend()


# ---------------------------------------------------------------------------
# DockerBackend smoke (skipped when docker absent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _docker_usable(), reason="docker daemon not available")
async def test_docker_backend_smoke(tmp_path) -> None:
    from linch.tools.execution import DockerBackend

    backend = DockerBackend()
    result = await backend.run("echo docker-ok", cwd=str(tmp_path), timeout_s=30.0)
    assert result.returncode == 0
    assert "docker-ok" in result.stdout


# ---------------------------------------------------------------------------
# Agent replaces BashTool with injected backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_replaces_bash_with_backend(tmp_path) -> None:
    from linch.tools.builtin import BashTool

    fake = _FakeBackend()

    from linch import Agent

    agent = Agent(
        model="claude-opus-4-8",
        cwd=str(tmp_path),
        execution_backend=fake,
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


# ---------------------------------------------------------------------------
# System prompt: "no sandbox" present by default, absent with a backend
# ---------------------------------------------------------------------------


def _block_texts(agent) -> list[str]:
    return [b.text for b in agent.system_blocks if hasattr(b, "text")]


def test_system_prompt_no_sandbox_present_by_default(tmp_path) -> None:
    from linch import Agent

    agent = Agent(model="claude-opus-4-8", cwd=str(tmp_path))
    combined = "\n".join(_block_texts(agent))
    assert "There is no sandbox" in combined


def test_system_prompt_sandbox_note_when_backend_injected(tmp_path) -> None:
    fake = _FakeBackend()
    from linch import Agent

    agent = Agent(model="claude-opus-4-8", cwd=str(tmp_path), execution_backend=fake)
    combined = "\n".join(_block_texts(agent))
    assert "There is no sandbox" not in combined
    assert "sandbox" in combined.lower()


def test_execution_backend_not_injected_into_restricted_registry(tmp_path) -> None:
    """execution_backend must not grant shell access to a registry that deliberately omits Bash."""
    fake = _FakeBackend()
    from linch import Agent
    from linch.tools import ToolRegistry

    no_bash = ToolRegistry()  # intentionally empty — caller wants no shell
    agent = Agent(
        model="claude-opus-4-8",
        cwd=str(tmp_path),
        tools=no_bash,
        execution_backend=fake,
    )
    assert agent.tools.get("Bash") is None, (
        "execution_backend must not inject BashTool into a registry that did not include it"
    )
