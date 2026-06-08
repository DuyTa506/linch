"""Pluggable execution backends for BashTool.

``ExecutionBackend`` is a duck-typed protocol: any object with a matching
``run`` coroutine is acceptable — no base-class inheritance needed.

Two implementations ship:

- ``LocalBackend``  — exact current behaviour (subprocess shell in cwd).
- ``DockerBackend`` — thin stub; requires docker on PATH (guarded by
  ``shutil.which``).  No Docker SDK dependency.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal as signal_mod
from dataclasses import dataclass
from typing import Any

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
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout_s)
        except asyncio.TimeoutError:
            _kill_process_group(proc)
            await _wait_after_kill(proc)
            return ExecResult(stdout="", stderr="", returncode=-1, timed_out=True)
        return ExecResult(
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            returncode=proc.returncode or 0,
            timed_out=False,
        )


class DockerBackend:
    """Runs commands inside a Docker container via ``docker run --rm``.

    Requires ``docker`` on PATH.  No Docker Python SDK — just ``subprocess``.

    Args:
        image:       Docker image to use (default ``python:3.12-slim``).
        docker_path: Override path to the ``docker`` binary.  Auto-detected
                     via ``shutil.which("docker")`` when ``None``.
    """

    def __init__(
        self,
        *,
        image: str = "python:3.12-slim",
        docker_path: str | None = None,
    ) -> None:
        resolved = docker_path or shutil.which("docker")
        if resolved is None:
            raise ToolExecutionError(
                "docker is not available — install Docker or use the default LocalBackend"
            )
        self._docker = resolved
        self.image = image

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: float,
        signal: Any = None,
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            self._docker,
            "run",
            "--rm",
            "-w",
            cwd,
            "-v",
            f"{cwd}:{cwd}",
            self.image,
            "sh",
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await _wait_after_kill(proc)
            return ExecResult(stdout="", stderr="", returncode=-1, timed_out=True)
        return ExecResult(
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
            returncode=proc.returncode or 0,
            timed_out=False,
        )


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
