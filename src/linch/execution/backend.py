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

from collections.abc import Mapping
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


class UnavailableFileBackend:
    """Filesystem placeholder for the deprecated shell-only compatibility path.

    A shell-only backend cannot honestly provide a filesystem in the same
    execution world.  This object deliberately fails every operation instead
    of silently pairing (for example) a Docker shell with unrelated in-memory
    state.  Applications using the compatibility path can pass an explicit
    ``filesystem=`` while they migrate to a unified backend.
    """

    _MESSAGE = (
        "this legacy shell-only execution backend has no filesystem transport; "
        "pass filesystem= explicitly or migrate to an ExecutionBackend with shell and fs"
    )

    async def read(self, path: str, *, offset: int = 0, limit: int | None = None) -> str:
        raise RuntimeError(self._MESSAGE)

    async def write(self, path: str, content: str) -> None:
        raise RuntimeError(self._MESSAGE)

    async def ls(self, prefix: str = "") -> list[str]:
        raise RuntimeError(self._MESSAGE)

    async def edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        replace_all: bool = False,
    ) -> int:
        raise RuntimeError(self._MESSAGE)

    async def exists(self, path: str) -> bool:
        raise RuntimeError(self._MESSAGE)

    async def delete(self, path: str) -> None:
        raise RuntimeError(self._MESSAGE)


class ExecutionBackendView:
    """Deprecated shell-only backend adapted to the unified shape.

    It never rewrites a genuine execution world's filesystem. An explicit
    filesystem is accepted here only as a compatibility aid for legacy shells
    while applications migrate to a backend that natively owns both transports.
    """

    def __init__(
        self,
        *,
        shell: ShellBackend,
        fs: FileBackend,
        source: Any,
        legacy_shell_only: bool = False,
    ) -> None:
        self.shell = shell
        self.fs = fs
        self.source = source
        self.legacy_shell_only = legacy_shell_only

    @property
    def confinement(self) -> Mapping[str, object] | None:
        value = getattr(self.source, "confinement", None)
        return value if isinstance(value, Mapping) and value else None

    @property
    def sandboxed(self) -> bool:
        """Whether the provider made a non-empty confinement declaration."""
        return self.confinement is not None

    @property
    def resume_policy_id(self) -> str | None:
        policy_id = getattr(self.source, "resume_policy_id", None)
        return policy_id if isinstance(policy_id, str) and policy_id else None

    @property
    def resume_policy_version(self) -> str | None:
        version = getattr(self.source, "resume_policy_version", None)
        return str(version) if version is not None else None

    @property
    def resume_policy_config(self) -> dict[str, object] | None:
        try:
            source_config = getattr(self.source, "resume_policy_config", None)
        except Exception:
            return None
        source_fs = getattr(self.source, "fs", None)
        if source_fs is self.fs or self.legacy_shell_only:
            return source_config if isinstance(source_config, dict) else None
        fs_policy = resume_policy_descriptor(self.fs)
        if not isinstance(source_config, dict) or fs_policy is None:
            return None
        return {"source": source_config, "filesystem_override": fs_policy}


def normalize_execution_backend(
    backend: Any,
    *,
    filesystem: FileBackend | None = None,
) -> ExecutionBackend:
    """Return a unified execution view, adapting legacy shell-only backends.

    A genuine unified backend is already one indivisible world: a different
    explicit ``filesystem=`` is rejected. The explicit filesystem escape hatch
    exists only for the deprecated shell-only adapter.
    """
    if isinstance(backend, ExecutionBackend):
        if filesystem is None or filesystem is backend.fs:
            return backend
        raise TypeError(
            "filesystem= conflicts with execution_backend.fs; a unified execution "
            "backend is one indivisible shell/filesystem world"
        )

    if not isinstance(backend, ShellBackend):
        raise TypeError(
            "execution_backend must expose {shell, fs}, or implement the deprecated "
            "shell-only run(...) protocol"
        )

    return ExecutionBackendView(
        shell=backend,
        fs=filesystem if filesystem is not None else UnavailableFileBackend(),
        source=backend,
        legacy_shell_only=True,
    )


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
