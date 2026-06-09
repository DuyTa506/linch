"""RED tests for Feature H — sandboxed execution backend (sub-phase 3b).

All imports are lazy (inside test bodies) to stay compatible with
test_hardening.py's sys.modules reset.
"""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_local_backend_abort_interrupts_running_command(tmp_path) -> None:
    """session.abort() must interrupt a running LocalBackend command (raises AbortError)."""
    from linch.abort import AbortContext
    from linch.errors import AbortError
    from linch.tools.execution import LocalBackend

    backend = LocalBackend()
    signal = AbortContext()
    task = asyncio.create_task(
        backend.run("sleep 60", cwd=str(tmp_path), timeout_s=10.0, signal=signal)
    )
    await asyncio.sleep(0.05)
    signal.abort()

    with pytest.raises(AbortError):
        await asyncio.wait_for(task, timeout=2.0)


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


class _FakeDockerProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self._final_returncode = returncode
        self.killed = False

    async def communicate(self):
        self.returncode = self._final_returncode
        return self.stdout, self.stderr

    async def wait(self):
        self.returncode = self._final_returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _docker_run_args(calls: list[list[str]]) -> list[str]:
    return next(call for call in calls if call[1] == "run")


def _docker_rm_args(calls: list[list[str]]) -> list[str]:
    return next(call for call in calls if call[1] == "rm")


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


@pytest.mark.asyncio
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


# ---------------------------------------------------------------------------
# DockerBackend missing docker raises at execution time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_docker_backend_default_args_preserve_legacy_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import linch.tools.execution as execution_mod
    from linch.tools.execution import DockerBackend

    calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return _FakeDockerProcess(stdout=b"docker-out\n")

    monkeypatch.setattr(execution_mod.asyncio, "create_subprocess_exec", fake_exec)

    backend = DockerBackend(docker_path="/usr/bin/docker")
    result = await backend.run("echo hi", cwd=str(tmp_path), timeout_s=10.0)

    assert result.stdout == "docker-out\n"
    args = _docker_run_args(calls)
    assert args[:5] == ["/usr/bin/docker", "run", "--rm", "--name", args[4]]
    assert args[4].startswith("linch-")
    assert args[5:] == [
        "-w",
        str(tmp_path),
        "-v",
        f"{tmp_path}:{tmp_path}",
        "python:3.12-slim",
        "sh",
        "-c",
        "echo hi",
    ]


@pytest.mark.asyncio
async def test_docker_backend_policy_args(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import linch.tools.execution as execution_mod
    from linch.tools.execution import DockerBackend

    calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        return _FakeDockerProcess()

    monkeypatch.setenv("FORWARDED_TOKEN", "host-secret")
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    monkeypatch.setattr(execution_mod.asyncio, "create_subprocess_exec", fake_exec)

    backend = DockerBackend(
        docker_path="/usr/bin/docker",
        image="alpine:3.20",
        network="none",
        workspace_mount="ro",
        read_only_root=True,
        tmpfs=("/tmp:rw,noexec,nosuid,nodev,size=64m",),
        env={"APP_MODE": "test"},
        forward_env=("FORWARDED_TOKEN", "MISSING_TOKEN"),
        user="1000:1000",
    )
    await backend.run("id", cwd=str(tmp_path), timeout_s=10.0)

    args = _docker_run_args(calls)
    assert "--network" in args
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert "--tmpfs" in args
    assert args[args.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,nodev,size=64m"
    assert "--env" in args
    assert "APP_MODE=test" in args
    assert "FORWARDED_TOKEN=host-secret" in args
    assert "MISSING_TOKEN=" not in " ".join(args)
    assert "--user" in args
    assert args[args.index("--user") + 1] == "1000:1000"
    assert f"{tmp_path}:{tmp_path}:ro" in args
    assert args[-3:] == ["sh", "-c", "id"]
    assert args[-4] == "alpine:3.20"


@pytest.mark.asyncio
async def test_docker_backend_timeout_removes_named_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import linch.tools.execution as execution_mod
    from linch.tools.execution import DockerBackend

    calls: list[list[str]] = []
    run_proc = _FakeDockerProcess()

    async def slow_communicate():
        await asyncio.sleep(60)
        return b"", b""

    run_proc.communicate = slow_communicate

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        if args[1] == "run":
            return run_proc
        return _FakeDockerProcess()

    monkeypatch.setattr(execution_mod.asyncio, "create_subprocess_exec", fake_exec)

    backend = DockerBackend(docker_path="/usr/bin/docker")
    result = await backend.run("sleep 60", cwd=str(tmp_path), timeout_s=0.01)

    run_args = _docker_run_args(calls)
    rm_args = _docker_rm_args(calls)
    assert result.timed_out is True
    assert run_proc.killed is True
    assert rm_args == ["/usr/bin/docker", "rm", "-f", run_args[4]]


@pytest.mark.asyncio
async def test_docker_backend_abort_removes_named_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import linch.tools.execution as execution_mod
    from linch.abort import AbortContext
    from linch.errors import AbortError
    from linch.tools.execution import DockerBackend

    calls: list[list[str]] = []
    run_proc = _FakeDockerProcess()

    async def slow_communicate():
        await asyncio.sleep(60)
        return b"", b""

    run_proc.communicate = slow_communicate

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        if args[1] == "run":
            return run_proc
        return _FakeDockerProcess()

    monkeypatch.setattr(execution_mod.asyncio, "create_subprocess_exec", fake_exec)

    backend = DockerBackend(docker_path="/usr/bin/docker")
    signal = AbortContext()
    task = asyncio.create_task(
        backend.run("sleep 60", cwd=str(tmp_path), timeout_s=10.0, signal=signal)
    )
    await asyncio.sleep(0)
    signal.abort()

    with pytest.raises(AbortError):
        await task

    run_args = _docker_run_args(calls)
    rm_args = _docker_rm_args(calls)
    assert run_proc.killed is True
    assert rm_args == ["/usr/bin/docker", "rm", "-f", run_args[4]]


def test_docker_backend_invalid_workspace_mount_raises() -> None:
    from linch.tools.execution import DockerBackend

    with pytest.raises(ValueError, match="workspace_mount"):
        DockerBackend(docker_path="/usr/bin/docker", workspace_mount="bad")  # type: ignore[arg-type]


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


# ---------------------------------------------------------------------------
# Abort/timeout helper — task draining and public-wait API (code-review fixes)
# ---------------------------------------------------------------------------


class _HangingProcess:
    """A fake subprocess whose communicate() never returns until cancelled."""

    returncode = None

    def __init__(self, drained: asyncio.Event) -> None:
        self._drained = drained

    async def communicate(self) -> tuple[bytes, bytes]:
        try:
            await asyncio.Event().wait()  # blocks forever
            return b"", b""
        finally:
            # Runs only if the task is actually driven to completion (i.e. the
            # cancellation was awaited/drained, not just scheduled).
            self._drained.set()


@pytest.mark.asyncio
async def test_communicate_abort_raises_and_drains_communicate_task() -> None:
    """Aborting mid-communicate raises AbortError and drains the cancelled task."""
    from linch.abort import AbortContext
    from linch.errors import AbortError
    from linch.tools.execution import _communicate_with_timeout_and_abort

    drained = asyncio.Event()
    signal = AbortContext()
    signal.abort()  # already aborted before the call

    with pytest.raises(AbortError):
        await _communicate_with_timeout_and_abort(
            _HangingProcess(drained), timeout_s=5.0, signal=signal
        )

    # The fix awaits the cancelled communicate task in `finally`, so its own
    # finally has run by the time the call returns. Without draining this is False.
    assert drained.is_set()


@pytest.mark.asyncio
async def test_communicate_uses_public_wait_api_not_private_event() -> None:
    """Abort monitoring goes through the public AbortContext.wait() coroutine."""
    from linch.abort import AbortContext
    from linch.errors import AbortError
    from linch.tools.execution import _communicate_with_timeout_and_abort

    waited = asyncio.Event()

    class _Signal(AbortContext):
        async def wait(self) -> None:  # type: ignore[override]
            waited.set()
            await super().wait()

    signal = _Signal()
    drained = asyncio.Event()

    async def _abort_soon() -> None:
        await asyncio.sleep(0.01)
        signal.abort()

    asyncio.create_task(_abort_soon())
    with pytest.raises(AbortError):
        await _communicate_with_timeout_and_abort(
            _HangingProcess(drained), timeout_s=5.0, signal=signal
        )
    assert waited.is_set(), "public wait() must be used to monitor the abort signal"


@pytest.mark.asyncio
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
