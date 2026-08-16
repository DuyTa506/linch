from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from linch.errors import ConfigError

from .base import Tool
from .builtin import BashTool, EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from .tasks import TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool

_REQUIRED_METHODS = ("validate", "execute", "summarize")


def _check_tool_shape(tool: Any) -> None:
    """Fail fast with a clear message when a tool doesn't fulfil the Tool protocol.

    The scheduler and permission engine call ``validate``/``summarize``
    unconditionally with no fallback, so a hand-rolled class tool missing one
    silently fails at call time with a cryptic AttributeError or a "tool is
    invalid" permission denial instead of a clear registration-time error.
    """
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name:
        raise ConfigError("tool.name must be a non-empty string")
    scope = getattr(tool, "scope", None)
    if scope not in {"read", "write", "exec"}:
        raise ConfigError(f"tool {name!r}.scope must be 'read', 'write', or 'exec', got {scope!r}")
    for method in _REQUIRED_METHODS:
        if not callable(getattr(tool, method, None)):
            raise ConfigError(f"tool {name!r} is missing a callable {method!r} method")
    output_schema = getattr(tool, "output_schema", None)
    renderer = getattr(tool, "render_output", None)
    renderer_id = getattr(tool, "renderer_id", None)
    renderer_version = getattr(tool, "renderer_version", None)
    if output_schema is None:
        if renderer is not None or renderer_id is not None or renderer_version is not None:
            raise ConfigError(f"tool {name!r} configures output rendering without an output_schema")
        return
    if not isinstance(output_schema, dict):
        raise ConfigError(f"tool {name!r}.output_schema must be a JSON Schema object")
    try:
        Draft202012Validator.check_schema(output_schema)
    except SchemaError as exc:
        raise ConfigError(f"tool {name!r}.output_schema is invalid: {exc.message}") from exc
    if renderer is None:
        if renderer_id is not None or renderer_version is not None:
            raise ConfigError(
                f"tool {name!r} sets renderer identity without a custom render_output"
            )
        return
    if (
        not callable(renderer)
        or inspect.iscoroutinefunction(renderer)
        or inspect.iscoroutinefunction(type(renderer).__call__)
    ):
        raise ConfigError(f"tool {name!r}.render_output must be a synchronous callable")
    if not isinstance(renderer_id, str) or not renderer_id.strip():
        raise ConfigError(f"tool {name!r}.renderer_id must be a non-empty stable string")
    if not isinstance(renderer_version, str) or not renderer_version.strip():
        raise ConfigError(f"tool {name!r}.renderer_version must be a non-empty stable string")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._generation = 0

    def add(self, tool: Tool) -> None:
        self.register(tool)

    @property
    def generation(self) -> int:
        """Monotonic structural revision used by prompt/request caches."""
        return self._generation

    def register(self, tool: Tool) -> None:
        """Register a new tool.  Raises :exc:`ConfigError` if the name is taken."""
        _check_tool_shape(tool)
        if tool.name in self._tools:
            raise ConfigError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        self._generation += 1

    def remove(self, name: str) -> Tool | None:
        return self.unregister(name)

    def unregister(self, name: str) -> Tool | None:
        """Remove the tool with *name* and return it, or ``None`` if not found.

        Example — strip Bash from the default toolset::

            registry = default_tools()
            registry.unregister("Bash")
        """
        removed = self._tools.pop(name, None)
        if removed is not None:
            self._generation += 1
        return removed

    def replace(self, tool: Tool) -> None:
        """Register *tool*, overwriting any existing tool with the same name.

        Unlike :meth:`register` this does **not** raise if the name exists;
        use it to hot-swap a built-in with a custom implementation.
        """
        _check_tool_shape(tool)
        self._tools[tool.name] = tool
        self._generation += 1

    def copy(self) -> ToolRegistry:
        """Return a shallow copy of this registry.

        The new registry contains the same tool instances; mutations to the
        copy (register / unregister) do not affect the original.
        """
        new = ToolRegistry()
        for tool in self._tools.values():
            new._tools[tool.name] = tool
        new._generation = self._generation
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

    def system_prompt_sections(self, *, active_names: Iterable[str] | None = None) -> list[Any]:
        """Return validated prompt sections contributed by active tools.

        A tool may expose a ``system_prompt_sections`` iterable or a zero-arg
        callable returning one.  The protocol stays optional and duck typed;
        tools without it are unchanged.  Contributions are ordered by tool
        name and then declaration order so registry insertion timing cannot
        perturb the provider prefix.

        Validation intentionally happens here, while the prompt is built, not
        when a tool is registered.  An inactive tool therefore cannot break a
        filtered request, while an active malformed contribution fails before
        the provider is called.
        """
        from ..config import SystemPromptSection

        selected = set(active_names) if active_names is not None else None
        sections: list[SystemPromptSection] = []
        seen_names: set[str] = set()
        for tool_name in sorted(self._tools):
            if selected is not None and tool_name not in selected:
                continue
            tool = self._tools[tool_name]
            raw = getattr(tool, "system_prompt_sections", None)
            if raw is None:
                continue
            if callable(raw):
                try:
                    raw = raw()
                except Exception as exc:
                    raise ConfigError(
                        f"tool {tool_name!r}.system_prompt_sections() failed: {exc}"
                    ) from exc
            if isinstance(raw, (str, bytes, SystemPromptSection)) or not isinstance(raw, Iterable):
                raise ConfigError(
                    f"tool {tool_name!r}.system_prompt_sections must be an iterable "
                    "of SystemPromptSection"
                )
            for index, section in enumerate(raw):
                if not isinstance(section, SystemPromptSection):
                    raise ConfigError(
                        f"tool {tool_name!r}.system_prompt_sections[{index}] must be "
                        "SystemPromptSection"
                    )
                if not isinstance(section.name, str) or not section.name.strip():
                    raise ConfigError(
                        f"tool {tool_name!r}.system_prompt_sections[{index}].name must be "
                        "a non-empty string"
                    )
                if section.name in seen_names:
                    raise ConfigError(
                        f"duplicate active tool system prompt section name {section.name!r}"
                    )
                if not isinstance(section.text, str) or not section.text:
                    raise ConfigError(
                        f"tool {tool_name!r}.system_prompt_sections[{index}].text must be "
                        "a non-empty string"
                    )
                if not isinstance(section.cacheable, bool):
                    raise ConfigError(
                        f"tool {tool_name!r}.system_prompt_sections[{index}].cacheable must be bool"
                    )
                if section.placement not in {
                    "before_defaults",
                    "after_defaults",
                    "after_env",
                }:
                    raise ConfigError(
                        f"tool {tool_name!r}.system_prompt_sections[{index}].placement is invalid"
                    )
                seen_names.add(section.name)
                sections.append(section)
        return sections

    def prompt_signature(
        self, *, active_names: Iterable[str] | None = None
    ) -> tuple[tuple[str, str, bool, str], ...]:
        """Stable signature of active tool prompt contributions.

        This is separate from provider tool schemas: changing contribution text
        invalidates the Agent's system-block cache even when the tool schema is
        unchanged.
        """
        return tuple(
            (section.name, section.text, section.cacheable, section.placement)
            for section in self.system_prompt_sections(active_names=active_names)
        )


def workspace_tools() -> ToolRegistry:
    """Return Linch's explicit software-workspace tool preset.

    ``Agent`` no longer installs this registry implicitly.  Coding harnesses
    should pass ``tools=workspace_tools()`` (or use a higher-level coding/deep
    agent preset), making filesystem and shell authority visible at the call
    site.
    """
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


def coding_tools() -> ToolRegistry:
    """Alias for :func:`workspace_tools` with a coding-oriented name."""
    return workspace_tools()


def default_tools() -> ToolRegistry:
    """Deprecated compatibility alias for :func:`workspace_tools`.

    The name is retained for source compatibility, but these are no longer the
    defaults of :class:`linch.Agent` in Linch 2.0.
    """
    return workspace_tools()


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
    registry = workspace_tools().subset(exclude=exclude)
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
