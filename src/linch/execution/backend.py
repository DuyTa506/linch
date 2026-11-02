"""The unified execution-world seam: shell + filesystem behind one backend.

Ported from dsh's capability seam (deepseek-harness/docs/capability-seams.md):
filesystem and subprocess providers share one execution world, so pointing them
at a remote sandbox moves Bash, PTY, and LSP together with no per-tool forks.

linch already had the two halves as *separate* worlds — ``LocalBackend`` /
``DockerBackend`` (shell, ``linch.tools.execution``) and ``FileBackend``
(``linch.filesystem.backend``). :class:`ExecutionBackend` unifies them: a single
object exposing ``.shell`` and ``.fs`` so a consumer swaps one backend and both
worlds move. It stays a duck-typed :class:`typing.Protocol` — any object with a
``shell`` and an ``fs`` attribute qualifies; no base class to inherit.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from linch.filesystem.backend import FileBackend
from linch.tools.execution import ExecResult, ShellRead


@runtime_checkable
class ShellBackend(Protocol):
    """The subprocess/shell half of an execution world."""

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout_s: float,
        signal: Any = None,
    ) -> ExecResult:
        """Run a shell command within the requested working directory.

        Args:
            command: Shell command to execute.
            cwd: Working directory for the command.
            timeout_s: Maximum execution time in seconds.
            signal: Optional abort signal observed by the implementation.

        Returns:
            Execution result containing stdout, stderr, exit status, and timeout state.
        """
        ...


@runtime_checkable
class ShellProcess(Protocol):
    """Optional live shell handle; timed waits do not terminate the command."""

    def read(self) -> ShellRead: ...
    async def wait(self, timeout_s: float | None) -> ExecResult | None: ...
    async def kill(self) -> None: ...


@runtime_checkable
class ShellStarter(Protocol):
    """Optional shell capability, independent of the run-only backend contract."""

    async def start(self, command: str, *, cwd: str) -> ShellProcess: ...


@runtime_checkable
class ExecutionBackend(Protocol):
    """One execution world: a shell backend and a filesystem backend together.

    Attributes:
        shell: Subprocess or remote shell transport.
        fs: Virtual filesystem transport for the same execution world.
    """

    shell: ShellBackend
    fs: FileBackend


def normalize_execution_backend(
    backend: ExecutionBackend,
    *,
    filesystem: FileBackend | None = None,
) -> ExecutionBackend:
    """Validate one indivisible shell/filesystem execution world.

    Args:
        backend: Candidate object exposing both ``shell`` and ``fs`` capabilities.
        filesystem: Optional filesystem that must be the exact ``backend.fs`` object.

    Returns:
        The validated execution backend.

    Raises:
        TypeError: If ``backend`` does not satisfy the protocol or ``filesystem``
            is a different object from ``backend.fs``.
    """
    if not isinstance(backend, ExecutionBackend):
        raise TypeError("execution_backend must expose both .shell and .fs")
    if filesystem is not None and filesystem is not backend.fs:
        raise TypeError(
            "filesystem= conflicts with execution_backend.fs; a unified execution "
            "backend is one indivisible shell/filesystem world"
        )
    return backend


def resume_policy_descriptor(component: Any) -> dict[str, object] | None:
    """Describe a transport's stable durable identity when it declares one.

    Args:
        component: Shell or filesystem transport to inspect.

    Returns:
        A JSON-compatible policy descriptor, or ``None`` when the component is
        opaque or has not opted into a resume identity.
    """
    policy_id = getattr(component, "resume_policy_id", None)
    if not isinstance(policy_id, str) or not policy_id:
        return None
    # No blanket except here: a component that declared ``resume_policy_id``
    # opted into durable identity, so a raising config is a misconfiguration,
    # not an opaque transport. Swallowing it silently drops that component's
    # contribution to the fingerprint and lets a resume through that should
    # have been denied.
    config = getattr(component, "resume_policy_config", None)
    if config is None:
        return None
    return {
        "type": f"{component.__class__.__module__}.{component.__class__.__qualname__}",
        "policy_id": policy_id,
        "policy_version": getattr(component, "resume_policy_version", None),
        "config": config,
    }
