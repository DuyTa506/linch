"""Pluggable execution backends for BashTool.

``ExecutionBackend`` is a duck-typed protocol: any object with a matching
``run`` coroutine is acceptable — no base-class inheritance needed.

Two implementations ship:

- ``LocalBackend``  — exact current behaviour (subprocess shell in cwd).
- ``DockerBackend`` — Docker CLI backend; requires docker on PATH (guarded by
  ``shutil.which``).  No Docker SDK dependency.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal as signal_mod
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from linch.abort import throw_if_aborted
from linch.errors import ToolExecutionError


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool


class LocalBackend:
    """Runs commands in a subprocess shell — identical to the original BashTool body."""

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: float,
        signal: Any = None,
    ) -> ExecResult:
        kwargs: dict[str, Any] = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        try:
            stdout_b, stderr_b = await _communicate_with_timeout_and_abort(
                proc, timeout_s=timeout_s, signal=signal
            )
        except asyncio.TimeoutError:
            _kill_process_group(proc)
            await _wait_after_kill(proc)
            return ExecResult(stdout="", stderr="", returncode=-1, timed_out=True)
        except BaseException:
            _kill_process_group(proc)
            await _wait_after_kill(proc)
            raise
        return ExecResult(
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            returncode=proc.returncode or 0,
            timed_out=False,
        )


class DockerBackend:
    """Runs commands inside a Docker container via ``docker run --rm``.

    Requires ``docker`` on PATH when the backend runs.  No Docker Python SDK —
    just ``subprocess``.

    Args:
        image:           Docker image to use (default ``python:3.12-slim``).
        docker_path:     Override path to the ``docker`` binary.  When ``None``,
                         ``shutil.which("docker")`` is checked at execution time.
        network:         Optional Docker network mode, for example ``"none"``.
        workspace_mount: Workspace bind mount mode. ``"rw"`` preserves the
                         historical mount shape; ``"ro"`` appends ``:ro``.
        read_only_root:  Pass ``--read-only`` to Docker when true.
        tmpfs:           Docker ``--tmpfs`` entries.
        env:             Explicit environment variables to pass into Docker.
        forward_env:     Allowlist of host environment variables to forward.
        user:            Optional Docker ``--user`` value.
    """

    def __init__(
        self,
        *,
        image: str = "python:3.12-slim",
        docker_path: str | None = None,
        network: str | None = None,
        workspace_mount: Literal["rw", "ro"] = "rw",
        read_only_root: bool = False,
        tmpfs: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        forward_env: tuple[str, ...] = (),
        user: str | None = None,
    ) -> None:
        if workspace_mount not in ("rw", "ro"):
            raise ValueError('workspace_mount must be "rw" or "ro"')
        self._docker = docker_path
        self.image = image
        self.network = network
        self.workspace_mount = workspace_mount
        self.read_only_root = read_only_root
        self.tmpfs = tuple(tmpfs)
        self.env = dict(env or {})
        self.forward_env = tuple(forward_env)
        self.user = user

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: float,
        signal: Any = None,
    ) -> ExecResult:
        throw_if_aborted(signal)
        docker = self._docker_path()
        container_name = f"linch-{uuid.uuid4().hex}"
        args = self._docker_args(
            docker=docker, container_name=container_name, cwd=cwd, command=command
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await _communicate_with_timeout_and_abort(
                proc, timeout_s=timeout_s, signal=signal
            )
        except asyncio.TimeoutError:
            proc.kill()
            await _wait_after_kill(proc)
            await self._remove_container(docker, container_name)
            return ExecResult(stdout="", stderr="", returncode=-1, timed_out=True)
        except BaseException:
            proc.kill()
            await _wait_after_kill(proc)
            await self._remove_container(docker, container_name)
            raise
        return ExecResult(
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            returncode=proc.returncode or 0,
            timed_out=False,
        )

    def _docker_path(self) -> str:
        resolved = self._docker or shutil.which("docker")
        if resolved is None:
            raise ToolExecutionError(
                "docker is not available — install Docker or use the default LocalBackend"
            )
        return resolved

    def _docker_args(
        self, *, docker: str, container_name: str, cwd: str, command: str
    ) -> list[str]:
        volume = f"{cwd}:{cwd}" if self.workspace_mount == "rw" else f"{cwd}:{cwd}:ro"
        args = [
            docker,
            "run",
            "--rm",
            "--name",
            container_name,
        ]
        if self.network is not None:
            args.extend(["--network", self.network])
        if self.read_only_root:
            args.append("--read-only")
        for entry in self.tmpfs:
            args.extend(["--tmpfs", entry])
        for key, value in self.env.items():
            args.extend(["--env", f"{key}={value}"])
        for key in self.forward_env:
            if key in os.environ:
                args.extend(["--env", f"{key}={os.environ[key]}"])
        if self.user is not None:
            args.extend(["--user", self.user])
        args.extend(
            [
                "-w",
                cwd,
                "-v",
                volume,
                self.image,
                "sh",
                "-c",
                command,
            ]
        )
        return args

    async def _remove_container(self, docker: str, container_name: str) -> None:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                docker,
                "rm",
                "-f",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            # Best-effort cleanup. CancelledError (a BaseException) is deliberately
            # NOT caught here so task cancellation propagates to the caller.
            if proc is not None and proc.returncode is None:
                proc.kill()
                await _wait_after_kill(proc)


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal_mod.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    proc.kill()


async def _wait_after_kill(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def _cancel_and_drain(task: asyncio.Task[Any] | None) -> None:
    """Cancel *task* if pending and await it so its resources are released."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _communicate_with_timeout_and_abort(
    proc: asyncio.subprocess.Process,
    *,
    timeout_s: float,
    signal: Any = None,
) -> tuple[bytes, bytes]:
    communicate_task = asyncio.create_task(proc.communicate())
    abort_task: asyncio.Task[Any] | None = None
    # Prefer the public AbortContext.wait() API; fall back to no abort monitoring
    # for signal objects that don't expose it.
    wait_for_abort = getattr(signal, "wait", None)
    if callable(wait_for_abort):
        abort_task = asyncio.create_task(cast(Any, wait_for_abort()))

    try:
        if abort_task is None:
            return await asyncio.wait_for(communicate_task, timeout=timeout_s)

        done, _pending = await asyncio.wait(
            {communicate_task, abort_task},
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if communicate_task in done:
            return communicate_task.result()
        if abort_task in done:
            throw_if_aborted(signal)
        raise asyncio.TimeoutError
    finally:
        # Cancel AND await both tasks so the subprocess pipe readers held by
        # proc.communicate() are released — an un-awaited cancelled task leaks
        # those pipes until GC and emits "Task was destroyed but it is pending".
        for task in (communicate_task, abort_task):
            await _cancel_and_drain(task)
