"""Virtual filesystem subsystem for offloading large tool results.

Public API::

    from linch.filesystem import (
        FileBackend,
        StateFileBackend,
        SqliteFileBackend,
        CompositeFileBackend,
        OffloadConfig,
        filesystem_tools,
    )

See :mod:`linch.filesystem.backend` for the backend protocol and
implementations, :mod:`linch.filesystem.offload` for the auto-offload
mechanism, and :mod:`linch.filesystem.tools` for the ls/read_file/
write_file/edit_file tools.
"""

from .backend import (
    CompositeFileBackend,
    FileBackend,
    StateFileBackend,
    normalize_path,
    resolve_filesystem_backend,
)
from .disk import DiskFileBackend
from .offload import OffloadConfig, estimate_tokens, maybe_offload
from .sqlite import SqliteFileBackend
from .tools import (
    EditFileTool,
    LsTool,
    ReadFileTool,
    WriteFileTool,
    filesystem_tools,
)

__all__ = [
    "CompositeFileBackend",
    "DiskFileBackend",
    "EditFileTool",
    "FileBackend",
    "LsTool",
    "OffloadConfig",
    "ReadFileTool",
    "SqliteFileBackend",
    "StateFileBackend",
    "WriteFileTool",
    "estimate_tokens",
    "filesystem_tools",
    "maybe_offload",
    "normalize_path",
    "resolve_filesystem_backend",
]
