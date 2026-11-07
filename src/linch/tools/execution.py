"""Shell backends and bounded local process execution."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import shutil
import signal as signal_mod
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from linch.abort import throw_if_aborted
from linch.errors import ToolExecutionError

from ._output import BoundedOutput


@dataclass(slots=True)
class ExecResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    truncated: bool = False
    stdout_path: str | None = None
    stderr_path: str | None = None
    aborted: bool = False
    signal: int | None = None


@dataclass(slots=True)
class ShellRead:
    """New stdout/stderr since the previous read; lossy marks dropped bytes."""

    stdout: str = ""
    stderr: str = ""
    lossy: bool = False
    stdout_path: str | None = None
    stderr_path: str | None = None


class _LocalProcess:
    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        cwd: str,
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.proc = proc
        self._cleanup = cleanup
        self.outputs = [BoundedOutput(cwd, name) for name in ("stdout", "stderr")]
        self._readers = [
            asyncio.create_task(self._pump(pipe, output))
            for pipe, output in zip((proc.stdout, proc.stderr), self.outputs, strict=True)
        ]
        self._done = asyncio.create_task(self._finish())

    async def _pump(self, pipe: asyncio.StreamReader | None, output: BoundedOutput) -> None:
        assert pipe is not None
        try:
            while chunk := await pipe.read(65536):
                await output.append_async(chunk)
        finally:
            await output.finish_async()

    async def _finish(self) -> ExecResult:
        waiter = asyncio.create_task(self.proc.wait())
        try:
            # asyncio's wait also waits for inherited pipe descriptors to close.
            while self.proc.returncode is None:
                await asyncio.sleep(0.01)
            _, pending = await asyncio.wait(self._readers, timeout=0.25)
            for task, output in zip(self._readers, self.outputs, strict=True):
                if task in pending:
                    output.drop()
        finally:
            for task in (*self._readers, waiter):
                await _cancel_and_drain(task)
            for pipe in (self.proc.stdout, self.proc.stderr):
                if pipe is not None:
                    cast(Any, pipe)._transport.close()
        out, err = self.outputs
        code = self.proc.returncode or 0
        return ExecResult(
            out.snapshot(),
            err.snapshot(),
            code,
            False,
            out.truncated or err.truncated,
            out.path,
            err.path,
            signal=-code if code < 0 else None,
        )

    @property
    def _leader_exited(self) -> bool:
        return self.proc.returncode is not None

    @property
    def _group_alive(self) -> bool:
        if os.name == "nt":
            return self.proc.returncode is None
        try:
            os.killpg(self.proc.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def read(self) -> ShellRead:
        out, lost_out = self.outputs[0].read()
        err, lost_err = self.outputs[1].read()
        return ShellRead(out, err, lost_out or lost_err, self.outputs[0].path, self.outputs[1].path)

    async def wait(self, timeout_s: float | None) -> ExecResult | None:
        done, _ = await asyncio.wait({self._done}, timeout=timeout_s)
        return self._done.result() if done else None

    async def kill(self) -> None:
        try:
            await self._kill()
        finally:
            if self._cleanup is not None:
                await self._cleanup()

    async def _kill(self) -> None:
        _kill_process_group(self.proc, signal_mod.SIGTERM)
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            try:
                if os.name == "nt":
                    if self.proc.returncode is not None:
                        break
                else:
                    os.killpg(self.proc.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        _kill_process_group(self.proc)
        await self._done


class LocalBackend:
    """Local shell execution with bounded output and explicit environment forwarding."""

    resume_policy_id = "linch.execution.local"
    resume_policy_version = "2"
    _allowlist = ("PATH", "HOME", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "USER", "SHELL", "TMPDIR")

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        forward_env: tuple[str, ...] = (),
        resume_fingerprint_key: bytes | None = None,
    ) -> None:
        self._policy = DockerBackend(
            env=env, forward_env=forward_env, resume_fingerprint_key=resume_fingerprint_key
        )

    @property
    def resume_policy_config(self) -> dict[str, object]:
        config = self._policy.resume_policy_config
        return {
            "allowlist": self._allowlist,
            "env": config["env"],
            "forward_env": config["forward_env"],
        }

    async def start(self, command: str, *, cwd: str) -> _LocalProcess:
        environment = {
            key: os.environ[key]
            for key in (*self._allowlist, *self._policy.forward_env)
            if key in os.environ
        }
        environment.update(self._policy.env)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        return _LocalProcess(proc, cwd)

    async def run(
        self, command: str, *, cwd: str, timeout_s: float, signal: Any = None
    ) -> ExecResult:
        handle = await self.start(command, cwd=cwd)
        return await _run_process(handle, timeout_s=timeout_s, signal=signal)


async def _run_process(handle: _LocalProcess, *, timeout_s: float, signal: Any) -> ExecResult:
    abort = asyncio.create_task(signal.wait()) if callable(getattr(signal, "wait", None)) else None
    timed_out = aborted = False
    try:
        done, _ = await asyncio.wait(
            {handle._done, *([abort] if abort else [])},
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if handle._done in done:
            return handle._done.result()
        aborted = abort in done
        timed_out = not aborted
    finally:
        await _cancel_and_drain(abort)
        if not handle._done.done() or timed_out or aborted:
            cleanup = asyncio.create_task(handle.kill())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled and not (timed_out or aborted):
                raise asyncio.CancelledError
    result = handle._done.result()
    result.timed_out, result.aborted = timed_out, aborted
    return result


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
        memory: str | None = "2g",
        cpus: float | None = 2,
        pids_limit: int | None = 512,
        cap_drop: tuple[str, ...] | None = ("ALL",),
        no_new_privileges: bool | None = True,
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
        self.memory, self.cpus, self.pids_limit = memory, cpus, pids_limit
        self.cap_drop, self.no_new_privileges = cap_drop, no_new_privileges
        self._resume_fingerprint_key = resume_fingerprint_key

    resume_policy_id = "linch.execution.docker"
    resume_policy_version = "2"

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
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "cap_drop": self.cap_drop,
            "no_new_privileges": self.no_new_privileges,
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
        handle = await self.start(command, cwd=cwd)
        return await _run_process(handle, timeout_s=timeout_s, signal=signal)

    async def start(self, command: str, *, cwd: str) -> _LocalProcess:
        docker = self._docker_path()
        container_name = f"linch-{uuid.uuid4().hex}"
        args = self._docker_args(
            docker=docker, container_name=container_name, cwd=cwd, command=command
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
            env={**os.environ, **self.env},
        )
        return _LocalProcess(proc, cwd, lambda: self._remove_container(docker, container_name))

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
        for flag, value in (
            ("--memory", self.memory),
            ("--cpus", self.cpus),
            ("--pids-limit", self.pids_limit),
        ):
            if value is not None:
                args.extend([flag, str(value)])
        for capability in self.cap_drop or ():
            args.extend(["--cap-drop", capability])
        if self.no_new_privileges:
            args.extend(["--security-opt", "no-new-privileges"])
        if self.network is not None:
            args.extend(["--network", self.network])
        if self.read_only_root:
            args.append("--read-only")
        for entry in self.tmpfs:
            args.extend(["--tmpfs", entry])
        for key in self.env:
            args.extend(["--env", key])
        for key in self.forward_env:
            if key in os.environ:
                args.extend(["--env", key])
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


def _kill_process_group(proc: asyncio.subprocess.Process, sig: int | None = None) -> None:
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal_mod.SIGKILL if sig is None else sig)
            return
        except ProcessLookupError:
            return
    if proc.returncode is None:
        if sig is None:
            proc.kill()
        else:
            proc.terminate()


async def _wait_after_kill(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except (TimeoutError, ProcessLookupError):
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
