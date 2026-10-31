"""The local execution world: subprocess shell + a filesystem, on this host."""

from __future__ import annotations

from pathlib import Path

from linch.filesystem.backend import FileBackend
from linch.filesystem.disk import DiskFileBackend

from .backend import ShellBackend


class LocalExecutionBackend:
    """Bundles the local subprocess shell with a filesystem into one backend.

    The default filesystem is disk-backed under the same local workspace.  A
    host subprocess paired with an in-memory filesystem would be two unrelated
    worlds: a file written through ``fs`` could not be read by Bash.  Imports
    stay lazy to avoid a tools↔execution import cycle.

    Args:
        shell: Optional shell transport. A local shell transport is created when omitted.
        fs: Optional filesystem transport. It must be a :class:`DiskFileBackend`
            rooted at ``cwd`` when supplied.
        cwd: Workspace root shared by the local shell and filesystem transports.

    Attributes:
        host_workspace_root: Resolved workspace path used by the local world.
        shell: Shell transport used for command execution.
        fs: Filesystem transport rooted at the same workspace.
    """

    resume_policy_id = "linch.execution.local-world"
    resume_policy_version = "1"

    def __init__(
        self,
        *,
        shell: ShellBackend | None = None,
        fs: FileBackend | None = None,
        cwd: str | Path = ".",
    ) -> None:
        """Create a local shell/filesystem execution world.

        Args:
            shell: Optional shell transport; defaults to the built-in local shell.
            fs: Optional disk filesystem; when supplied it must be rooted at ``cwd``.
            cwd: Workspace root shared by the shell and filesystem.

        Raises:
            ValueError: If a supplied filesystem is not a disk backend rooted at ``cwd``.
        """
        workspace_root = Path(cwd).resolve()
        if shell is None:
            from linch.tools.execution import LocalBackend

            shell = LocalBackend()
        if fs is None:
            fs = DiskFileBackend(root=workspace_root)
        elif not isinstance(fs, DiskFileBackend) or fs.root != workspace_root:
            raise ValueError(
                "LocalExecutionBackend fs must be a DiskFileBackend rooted at cwd; "
                "host shell and filesystem must share one workspace"
            )
        self.host_workspace_root = str(workspace_root)
        self.shell: ShellBackend = shell
        self.fs: FileBackend = fs

    @property
    def confinement(self) -> None:
        """Return the local world's confinement declaration.

        Returns:
            ``None`` because this backend executes directly on the host.
        """
        return None

    @property
    def sandboxed(self) -> bool:
        """Report whether this execution world is sandboxed.

        Returns:
            Always ``False`` for the host-local backend.
        """
        return False

    @property
    def security_posture(self) -> str:
        """Declare the host posture explicitly.

        Without this a local world is indistinguishable from a custom backend
        that simply declared nothing, and would be described to the model as
        "unverified" rather than as running directly on the host.

        Returns:
            The literal ``"host"`` posture declaration.
        """
        return "host"

    @property
    def resume_policy_config(self) -> dict[str, object] | None:
        """Return the combined durable identity of the local transports.

        Returns:
            A descriptor for the shell and filesystem transports, or ``None`` if
            either transport does not declare a resume identity.
        """
        from .backend import resume_policy_descriptor

        shell = resume_policy_descriptor(self.shell)
        fs = resume_policy_descriptor(self.fs)
        if shell is None or fs is None:
            return None
        return {"shell": shell, "fs": fs}
