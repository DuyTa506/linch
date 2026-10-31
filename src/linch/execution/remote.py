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

    Args:
        shell: Shell transport that executes inside the remote world.
        fs: Filesystem transport for the same remote world.
        confinement: Optional embedder-supplied confinement metadata. Its presence
            is what makes :attr:`sandboxed` true.
        resume_policy_id: Optional stable policy identifier for durable resumes.
        resume_policy_version: Optional version for ``resume_policy_id``.
        resume_policy_config: Optional replay-relevant configuration. The
            ``confinement`` key is reserved and must not be supplied here.

    Attributes:
        shell: Shell transport used for command execution.
        fs: Filesystem transport used for virtual files.
        confinement: Declared boundary metadata, or ``None`` when unspecified.
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
        """Create a remote shell/filesystem execution world.

        Args:
            shell: Shell transport for the remote world.
            fs: Filesystem transport for the same world.
            confinement: Optional metadata describing the declared boundary.
            resume_policy_id: Optional durable policy identifier.
            resume_policy_version: Optional version of that policy.
            resume_policy_config: Optional replay configuration, excluding the
                reserved ``confinement`` key.

        Raises:
            ValueError: If ``resume_policy_config`` attempts to define
                ``confinement``.
        """
        self.shell: ShellBackend = shell
        self.fs: FileBackend = fs
        self.confinement = dict(confinement) if confinement else None
        self.resume_policy_id = resume_policy_id
        self.resume_policy_version = resume_policy_version
        if resume_policy_config is not None and "confinement" in resume_policy_config:
            # resume_policy_config merges this world's confinement under that
            # name, so a caller key would be dropped from the fingerprint.
            raise ValueError(
                "resume_policy_config must not define 'confinement'; it is reserved "
                "for the execution world's own confinement declaration"
            )
        self._resume_policy_config = (
            dict(resume_policy_config) if resume_policy_config is not None else None
        )

    @property
    def sandboxed(self) -> bool:
        """Report whether the embedder declared a confinement boundary.

        Returns:
            ``True`` when ``confinement`` is not ``None``; otherwise ``False``.
        """
        return self.confinement is not None

    @property
    def resume_policy_config(self) -> dict[str, object] | None:
        """Return replay configuration including this world's confinement.

        Returns:
            The configured resume-policy mapping with the reserved
            ``confinement`` entry, or ``None`` when no policy configuration was set.
        """
        if self._resume_policy_config is None:
            return None
        return {
            **self._resume_policy_config,
            "confinement": self.confinement,
        }
