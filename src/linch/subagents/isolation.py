"""Private helpers for remapping isolated subagent tool worlds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..errors import ConfigError

if TYPE_CHECKING:
    from ..session import Session


@dataclass(slots=True, init=False)
class _IsolatedTool:
    """Run a child tool with child-scoped execution capabilities."""

    tool: Any
    execution: Any
    filesystem: Any

    def __init__(self, tool: Any, *, execution: Any, filesystem: Any) -> None:
        self.tool = tool
        self.execution = execution
        self.filesystem = filesystem

    def __getattr__(self, name: str) -> Any:
        return getattr(self.tool, name)

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        """Execute the wrapped tool with the isolated child context.

        Args:
            input: Validated input for the wrapped tool.
            ctx: Tool context whose execution and filesystem are replaced.

        Returns:
            The wrapped tool's execution result.
        """
        child_ctx = replace(ctx, execution=self.execution, filesystem=self.filesystem)
        return await self.tool.execute(input, child_ctx)


def _unwrap_isolated_tool(tool: Any) -> Any:
    """Return the original tool beneath any prior child-world wrappers."""
    while isinstance(tool, _IsolatedTool):
        tool = tool.tool
    return tool


def _bind_isolated_tools(tools: Any, *, execution: Any, filesystem: Any) -> Any:
    """Return a child registry whose tools cannot reach parent execution state."""
    from ..tools import ToolRegistry
    from ..tools.builtin import BashTool
    from ..tools.execution import LocalBackend

    bound = ToolRegistry()
    for registered_tool in tools.list():
        tool = _unwrap_isolated_tool(registered_tool)
        if isinstance(tool, BashTool) and not isinstance(tool._backend, LocalBackend):
            raise ConfigError("isolated subagent requires a local shell backend")
        bound.register(cast(Any, _IsolatedTool(tool, execution=execution, filesystem=filesystem)))
    return bound


def _isolated_dependencies(deps: Any, filesystem: Any) -> Any:
    """Replace any dependency-provided filesystem without changing other deps."""
    if deps is None:
        return None
    if isinstance(deps, Mapping):
        if "filesystem" not in deps:
            return deps
        isolated = dict(deps)
        isolated["filesystem"] = filesystem
        return isolated

    from ..filesystem.backend import FileBackend

    try:
        direct_backend = isinstance(deps, FileBackend)
        has_configured = getattr(deps, "filesystem", None) is not None
    except Exception as exc:
        raise ConfigError("isolated subagent cannot safely remap deps.filesystem") from exc

    if direct_backend:
        return filesystem
    if has_configured:
        raise ConfigError(
            "isolated subagent requires dict or direct filesystem dependencies; "
            "refusing to expose an object-backed parent filesystem"
        )
    return deps


def _dependency_filesystem(deps: Any) -> Any:
    """Resolve a dependency-provided filesystem for isolation validation."""
    if deps is None:
        return None
    from ..filesystem.backend import FileBackend, resolve_filesystem_backend

    try:
        if isinstance(deps, Mapping):
            return deps.get("filesystem")
        if isinstance(deps, FileBackend):
            return deps
        return resolve_filesystem_backend(deps)
    except Exception as exc:
        raise ConfigError("isolated subagent cannot safely remap deps.filesystem") from exc


def _isolated_world(
    parent_session: Session,
    parent_agent: Any,
    cwd: str,
    *,
    deps: Any = None,
) -> tuple[Any, Any]:
    """Build a child filesystem and execution overlay rooted at *cwd*.

    Only disk-backed local worlds can be remapped to a local isolation cwd. An
    opaque or remote filesystem is rejected rather than silently leaving the
    parent backend available through the scheduler's agent-level context.
    """
    from ..execution.local import LocalExecutionBackend
    from ..filesystem.disk import DiskFileBackend

    parent_filesystem = getattr(parent_session, "filesystem", None)
    dependency_filesystem = _dependency_filesystem(deps)
    parent_execution = getattr(parent_agent, "execution_backend", None)
    execution_filesystem = getattr(parent_execution, "fs", None)
    for configured_filesystem in (
        parent_filesystem,
        dependency_filesystem,
        execution_filesystem,
    ):
        if configured_filesystem is not None and not isinstance(
            configured_filesystem, DiskFileBackend
        ):
            raise ConfigError(
                "isolated subagent requires a disk-backed filesystem; refusing to expose the "
                "parent filesystem backend"
            )
    has_filesystem = any(
        filesystem is not None
        for filesystem in (parent_filesystem, dependency_filesystem, execution_filesystem)
    )
    if parent_execution is None:
        return None, DiskFileBackend(root=cwd) if has_filesystem else None
    if not isinstance(execution_filesystem, DiskFileBackend):
        raise ConfigError(
            "isolated subagent requires a disk-backed execution filesystem; refusing to "
            "expose the parent execution backend"
        )
    if not isinstance(parent_execution, LocalExecutionBackend):
        raise ConfigError(
            "isolated subagent requires a local execution workspace; refusing to expose "
            "the parent execution backend"
        )
    filesystem = DiskFileBackend(root=cwd)
    execution = LocalExecutionBackend(shell=parent_execution.shell, fs=filesystem, cwd=cwd)
    return execution, filesystem


def _continued_isolated_world(child_session: Session, parent_agent: Any) -> tuple[Any, Any]:
    """Rebuild the child execution world for a retained isolated session."""
    from ..execution.local import LocalExecutionBackend
    from ..filesystem.disk import DiskFileBackend

    cwd = child_session.cwd_override
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_dir():
        raise ConfigError("isolated subagent continuation requires its retained cwd to still exist")
    filesystem = getattr(child_session, "filesystem", None)
    if filesystem is not None:
        if not isinstance(filesystem, DiskFileBackend) or filesystem.root != Path(cwd).resolve():
            raise ConfigError(
                "isolated subagent continuation requires a disk filesystem rooted at its cwd"
            )

    parent_execution = getattr(parent_agent, "execution_backend", None)
    if parent_execution is None:
        return None, filesystem
    if not isinstance(parent_execution, LocalExecutionBackend) or not isinstance(
        getattr(parent_execution, "fs", None), DiskFileBackend
    ):
        raise ConfigError("isolated subagent continuation requires a local execution workspace")
    if filesystem is None:
        raise ConfigError(
            "isolated subagent continuation is missing its retained filesystem backend"
        )
    execution = LocalExecutionBackend(
        shell=parent_execution.shell,
        fs=filesystem,
        cwd=cwd,
    )
    return execution, filesystem
