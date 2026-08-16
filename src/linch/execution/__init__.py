"""Unified execution backends (capability seam): shell + filesystem as one world.

See :mod:`linch.execution.backend` for the rationale. Register a backend on an
agent's :class:`~linch.kernel.Context` as ``ctx.execution`` and have tools source
their shell and filesystem from it, so swapping local↔sandbox is one change.
"""

from __future__ import annotations

from .backend import ExecutionBackend, ShellBackend
from .local import LocalExecutionBackend
from .remote import RemoteExecutionBackend

__all__ = [
    "ExecutionBackend",
    "LocalExecutionBackend",
    "RemoteExecutionBackend",
    "ShellBackend",
]
