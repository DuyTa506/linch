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
        return None

    @property
    def sandboxed(self) -> bool:
        return False

    @property
    def resume_policy_config(self) -> dict[str, object] | None:
        from .backend import resume_policy_descriptor

        shell = resume_policy_descriptor(self.shell)
        fs = resume_policy_descriptor(self.fs)
        if shell is None or fs is None:
            return None
        return {"shell": shell, "fs": fs}
