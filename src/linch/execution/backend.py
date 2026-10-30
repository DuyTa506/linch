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
from linch.tools.execution import ExecResult


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
    ) -> ExecResult: ...


@runtime_checkable
class ExecutionBackend(Protocol):
    """One execution world: a shell backend and a filesystem backend together."""

    shell: ShellBackend
    fs: FileBackend


def normalize_execution_backend(
    backend: ExecutionBackend,
    *,
    filesystem: FileBackend | None = None,
) -> ExecutionBackend:
    """Validate one indivisible shell/filesystem execution world."""
    if not isinstance(backend, ExecutionBackend):
        raise TypeError("execution_backend must expose both .shell and .fs")
    if filesystem is not None and filesystem is not backend.fs:
        raise TypeError(
            "filesystem= conflicts with execution_backend.fs; a unified execution "
            "backend is one indivisible shell/filesystem world"
        )
    return backend


def resume_policy_descriptor(component: Any) -> dict[str, object] | None:
    """Return a stable durable identity for one transport, or ``None`` if opaque."""
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
