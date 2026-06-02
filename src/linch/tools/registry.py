from __future__ import annotations

from typing import Any

from linch.errors import ConfigError

from .base import Tool
from .builtin import BashTool, EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from .tasks import TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register a new tool.  Raises :exc:`ConfigError` if the name is taken."""
        if tool.name in self._tools:
            raise ConfigError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def remove(self, name: str) -> Tool | None:
        return self.unregister(name)

    def unregister(self, name: str) -> Tool | None:
        """Remove the tool with *name* and return it, or ``None`` if not found.

        Example — strip Bash from the default toolset::

            registry = default_tools()
            registry.unregister("Bash")
        """
        return self._tools.pop(name, None)

    def replace(self, tool: Tool) -> None:
        """Register *tool*, overwriting any existing tool with the same name.

        Unlike :meth:`register` this does **not** raise if the name exists;
        use it to hot-swap a built-in with a custom implementation.
        """
        self._tools[tool.name] = tool

    def copy(self) -> ToolRegistry:
        """Return a shallow copy of this registry.

        The new registry contains the same tool instances; mutations to the
        copy (register / unregister) do not affect the original.
        """
        new = ToolRegistry()
        for tool in self._tools.values():
            new._tools[tool.name] = tool
        return new

    def subset(
        self,
        *,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
    ) -> ToolRegistry:
        """Return a new registry containing a filtered subset of tools.

        Args:
            include: If given, only tools whose names are in this set are kept.
            exclude: Tool names to drop from the result.  Applied after
                *include*.

        Example — RAG agent with only a custom ``RetrieveDocs`` tool plus
        tasks::

            registry = default_tools().subset(
                include={"RetrieveDocs", "TaskCreate", "TaskGet"}
            )
        """
        new = ToolRegistry()
        for name, tool in self._tools.items():
            if include is not None and name not in include:
                continue
            if exclude is not None and name in exclude:
                continue
            new._tools[name] = tool
        return new

    def select(
        self,
        *,
        names: set[str] | None = None,
        tags: set[str] | None = None,
    ) -> ToolRegistry:
        """Return tools matching any supplied name or tag.

        With no filters this returns a shallow copy.  Tool tags are read from a
        ``tags`` attribute when present.
        """
        if names is None and tags is None:
            return self.copy()
        selected = ToolRegistry()
        for name, tool in self._tools.items():
            tool_tags = set(getattr(tool, "tags", ()) or ())
            if names is not None and name in names:
                selected._tools[name] = tool
                continue
            if tags is not None and tool_tags.intersection(tags):
                selected._tools[name] = tool
        return selected

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": getattr(tool, "schema", getattr(tool, "input_schema", {})),
            }
            for tool in self.list()
        ]


def default_tools() -> ToolRegistry:
    """Return a :class:`ToolRegistry` populated with all built-in tools."""
    registry = ToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(BashTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(TaskCreateTool())
    registry.register(TaskListTool())
    registry.register(TaskGetTool())
    registry.register(TaskUpdateTool())
    return registry


def tools_from_defaults(
    *,
    exclude: set[str] | None = None,
    extra: list[Tool] | None = None,
) -> ToolRegistry:
    """Return a modified copy of the default toolset.

    Args:
        exclude: Tool names to remove from the defaults.
        extra: Additional tools to register (after removing *exclude*).

    Example::

        registry = tools_from_defaults(
            exclude={"Bash", "Write"},
            extra=[MySearchTool()],
        )
    """
    registry = default_tools().subset(exclude=exclude)
    for tool in extra or []:
        registry.register(tool)
    return registry


def empty_tools(*extra: Tool) -> ToolRegistry:
    """Return a :class:`ToolRegistry` with only the supplied tools registered.

    Example::

        registry = empty_tools(RetrieveDocs(store=my_store), RunSQL(db=conn))
    """
    registry = ToolRegistry()
    for tool in extra:
        registry.register(tool)
    return registry
