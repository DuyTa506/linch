"""A remote/sandbox execution world: shell + filesystem both routed off-host."""

from __future__ import annotations

from collections.abc import Mapping

from linch.filesystem.backend import FileBackend

from .backend import ShellBackend


class RemoteExecutionBackend:
    """Bundles an off-host shell and filesystem so tools run in a sandbox.

    linch ships no concrete remote transport — supply a shell backend (for
    example ``DockerBackend`` from ``linch.tools.execution``, or an E2B adapter)
    and a remote :class:`~linch.filesystem.backend.FileBackend`. Pointing a
    tool's ``ctx.execution`` at this instance moves Bash and file access together
    to the same sandbox, which is the whole point of the seam.
    """

    def __init__(
        self,
        *,
        shell: ShellBackend,
        fs: FileBackend,
        confinement: Mapping[str, object] | None = None,
        resume_policy_id: str | None = None,
        resume_policy_version: str | None = None,
        resume_policy_config: Mapping[str, object] | None = None,
    ) -> None:
        self.shell: ShellBackend = shell
        self.fs: FileBackend = fs
        self.confinement = dict(confinement) if confinement else None
        self.resume_policy_id = resume_policy_id
        self.resume_policy_version = resume_policy_version
        self._resume_policy_config = (
            dict(resume_policy_config) if resume_policy_config is not None else None
        )

    @property
    def sandboxed(self) -> bool:
        """True only when the embedder declared concrete confinement metadata."""
        return self.confinement is not None

    @property
    def resume_policy_config(self) -> dict[str, object] | None:
        if self._resume_policy_config is None:
            return None
        return {
            **self._resume_policy_config,
            "confinement": self.confinement,
        }
