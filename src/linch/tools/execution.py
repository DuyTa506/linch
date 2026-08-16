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
import hashlib
import hmac
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

    resume_policy_id = "linch.execution.local"
    resume_policy_version = "1"

    @property
    def resume_policy_config(self) -> dict[str, object]:
        """Stable, JSON-safe identity used by durable run contracts."""
        return {}

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
        resume_fingerprint_key: Host secret used to HMAC environment values in
                         durable run contracts. Required for durable runs when
                         ``env`` or ``forward_env`` is non-empty; never stored.
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
        resume_fingerprint_key: bytes | None = None,
    ) -> None:
        if workspace_mount not in ("rw", "ro"):
            raise ValueError('workspace_mount must be "rw" or "ro"')
        if resume_fingerprint_key is not None and (
            not isinstance(resume_fingerprint_key, bytes) or len(resume_fingerprint_key) < 16
        ):
            raise ValueError("resume_fingerprint_key must be at least 16 bytes")
        self._docker = docker_path
        self.image = image
        self.network = network
        self.workspace_mount = workspace_mount
        self.read_only_root = read_only_root
        self.tmpfs = tuple(tmpfs)
        self.env = dict(env or {})
        self.forward_env = tuple(forward_env)
        self.user = user
        self._resume_fingerprint_key = resume_fingerprint_key

    resume_policy_id = "linch.execution.docker"
    resume_policy_version = "1"

    @property
    def confinement(self) -> dict[str, object]:
        """Declare the container boundary commands run inside.

        Consumed by :class:`~linch.execution.backend.ExecutionBackend` views to
        report ``sandboxed``. This records what this backend configures, not an
        independently verified guarantee — a container is only as isolated as
        its image, mounts, and daemon allow.
        """
        return {
            "kind": "docker",
            "image": self.image,
            "network": self.network,
            "workspace_mount": self.workspace_mount,
            "read_only_root": self.read_only_root,
            "user": self.user,
        }

    @property
    def resume_policy_config(self) -> dict[str, object]:
        """Return replay-relevant configuration without persisting environment values.

        Environment values participate through a caller-keyed HMAC. This makes
        a changed environment fail resume without exposing a reversible plain
        digest in run-store metadata. Image contents remain external state;
        callers who need reproducible containers should configure an immutable
        digest.
        """

        if (self.env or self.forward_env) and self._resume_fingerprint_key is None:
            raise ValueError(
                "durable DockerBackend with env or forward_env requires resume_fingerprint_key"
            )

        def digest(value: str) -> str:
            key = self._resume_fingerprint_key
            assert key is not None
            value_digest = hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
            return f"hmac-sha256:{value_digest}"

        forwarded = {
            key: digest(os.environ[key]) if key in os.environ else None for key in self.forward_env
        }
        return {
            # The configured value is policy. PATH resolution is host state and
            # stays in _docker_path(), immediately before execution.
            "docker_path": self._docker,
            "image": self.image,
            "network": self.network,
            "workspace_mount": self.workspace_mount,
            "read_only_root": self.read_only_root,
            "tmpfs": self.tmpfs,
            "env": {key: digest(value) for key, value in sorted(self.env.items())},
            "forward_env": forwarded,
            "user": self.user,
        }

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
