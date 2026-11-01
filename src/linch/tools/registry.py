from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from linch.errors import ConfigError
from linch.kernel import Disposable

from .base import Tool
from .builtin import BashTool, EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from .tasks import TaskCreateTool, TaskGetTool, TaskListTool, TaskUpdateTool

_REQUIRED_METHODS = ("validate", "execute", "summarize")


@dataclass(slots=True)
class _Registration:
    """One ownership claim for a registry slot."""

    token: object
    tool: Tool
    previous: _Registration | None = None
    disposed: bool = False


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
    input_schema = getattr(tool, "input_schema", None)
    if not isinstance(input_schema, dict):
        raise ConfigError(f"tool {name!r}.input_schema must be a dict")
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
    """Own named tool registrations and reversible replacements.

    Registrations preserve insertion order for list and schema output; prompt
    contributions are ordered by tool name and declaration order. Registration
    and replacement operations return disposable ownership handles.
    """

    def __init__(self) -> None:
        """Create an empty registry."""

        self._tools: dict[str, Tool] = {}
        self._registrations: dict[str, _Registration] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        """Return the monotonic structural revision used by caches.

        Returns:
            Revision incremented whenever the registry structure changes.
        """
        return self._generation

    def register(self, tool: Tool) -> Disposable:
        """Register a new tool; returns a disposer that unregisters it.

        Args:
            tool: Tool to add under its model-visible name.

        Returns:
            Disposable that removes this registration while it still owns the slot.

        Raises:
            ConfigError: If the tool shape is invalid or its name is already
                registered.

        The returned :class:`~linch.kernel.Disposable` removes only this
        registration. A later registration owns the slot even when it reuses
        the same tool object.
        """
        _check_tool_shape(tool)
        if tool.name in self._tools:
            raise ConfigError(f"tool {tool.name!r} already registered")
        registration = _Registration(token=object(), tool=tool)
        self._set_current(tool.name, registration)
        self._generation += 1
        return self._disposer(tool.name, registration)

    def unregister(self, name: str) -> Tool | None:
        """Remove the tool with *name* and return it, or ``None`` if not found.

        Args:
            name: Model-visible name of the tool to remove.

        Returns:
            The removed tool, or ``None`` when no registration has that name.

        Example — strip Bash from the default toolset::

            registry = workspace_tools()
            registry.unregister("Bash")
        """
        removed = self._tools.pop(name, None)
        if removed is not None:
            self._registrations.pop(name, None)
            self._generation += 1
        return removed

    def replace(self, tool: Tool) -> Disposable:
        """Register *tool*, overwriting any existing tool with the same name.

        Args:
            tool: Replacement tool to install under its model-visible name.

        Returns:
            Disposable that restores the preceding registration while this
                replacement still owns the slot.

        Unlike :meth:`register` this does **not** raise if the name exists;
        use it to hot-swap a built-in with a custom implementation.  Returns a
        disposer that restores the preceding registration if this replacement
        still owns the slot.

        Raises:
            ConfigError: If the replacement tool shape is invalid.
        """
        _check_tool_shape(tool)
        registration = _Registration(
            token=object(),
            tool=tool,
            previous=self._registrations.get(tool.name),
        )
        self._set_current(tool.name, registration)
        self._generation += 1
        return self._disposer(tool.name, registration)

    def _set_current(self, name: str, registration: _Registration) -> None:
        self._tools[name] = registration.tool
        self._registrations[name] = registration

    def _disposer(self, name: str, registration: _Registration) -> Disposable:
        """Build an ownership-aware teardown handle for *registration*."""

        def _undo() -> None:
            registration.disposed = True
            current = self._registrations.get(name)
            if current is None or current.token is not registration.token:
                return

            previous = registration.previous
            while previous is not None and previous.disposed:
                previous = previous.previous

            if previous is None:
                self._tools.pop(name, None)
                self._registrations.pop(name, None)
            else:
                self._set_current(name, previous)
            self._generation += 1

        return Disposable(_undo)

    def _copy_entry(self, name: str, tool: Tool) -> None:
        """Install an independent base registration without changing generation."""
        self._set_current(name, _Registration(token=object(), tool=tool))

    def copy(self) -> ToolRegistry:
        """Return a shallow copy of this registry.

        The new registry contains the same tool instances; mutations to the
        copy (register / unregister) do not affect the original.

        Returns:
            Independent registry containing the same tool instances.
        """
        new = ToolRegistry()
        for name, tool in self._tools.items():
            new._copy_entry(name, tool)
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

        Returns:
            Independent registry containing the filtered tool registrations.

        Example — RAG agent with only a custom ``RetrieveDocs`` tool plus
        tasks::

            registry = workspace_tools().subset(
                include={"RetrieveDocs", "TaskCreate", "TaskGet"}
            )
        """
        new = ToolRegistry()
        for name, tool in self._tools.items():
            if include is not None and name not in include:
                continue
            if exclude is not None and name in exclude:
                continue
            new._copy_entry(name, tool)
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

        Args:
            names: Optional names to include.
            tags: Optional tags whose intersection selects a tool.

        Returns:
            New registry containing selected tools.
        """
        if names is None and tags is None:
            return self.copy()
        selected = ToolRegistry()
        for name, tool in self._tools.items():
            tool_tags = set(getattr(tool, "tags", ()) or ())
            if names is not None and name in names:
                selected._copy_entry(name, tool)
                continue
            if tags is not None and tool_tags.intersection(tags):
                selected._copy_entry(name, tool)
        return selected

    def get(self, name: str) -> Tool | None:
        """Look up a registered tool by name.

        Args:
            name: Registered tool name.

        Returns:
            Tool instance, or ``None`` when no tool has that name.
        """

        return self._tools.get(name)

    def list(self) -> list[Tool]:
        """Return registered tools in insertion order.

        Returns:
            Shallow list of registered tool instances.
        """

        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        """Return model-facing schemas for all registered tools.

        Returns:
            Tool name, description, and input-schema mappings.
        """

        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
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

        Args:
            active_names: Optional tool names to include; ``None`` includes all
                registered tools.

        Returns:
            Validated prompt sections in deterministic tool-name and declaration
            order.

        Raises:
            ConfigError: If an active tool contributes malformed sections.
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

        Args:
            active_names: Optional tool names whose prompt contributions are
                included; ``None`` includes all registered tools.

        Returns:
            Stable tuples containing section name, text, cacheability, and
            placement.

        Raises:
            ConfigError: If an active tool contributes malformed sections.
        """
        return tuple(
            (section.name, section.text, section.cacheable, section.placement)
            for section in self.system_prompt_sections(active_names=active_names)
        )


def workspace_tools(
    *,
    exclude: set[str] | None = None,
    extra: Iterable[Tool] | None = None,
) -> ToolRegistry:
    """Return Linch's explicit software-workspace tool preset.

    ``Agent`` no longer installs this registry implicitly.  Coding harnesses
    should pass ``tools=workspace_tools()`` (or use a higher-level coding/deep
    agent preset), making filesystem and shell authority visible at the call
    site.

    Args:
        exclude: Built-in tool names to omit.
        extra: Additional tools to register after the preset.

    Returns:
        Registry containing the workspace preset and requested extras.
    """
    registry = ToolRegistry()
    for tool in (
        ReadTool(),
        WriteTool(),
        EditTool(),
        BashTool(),
        GlobTool(),
        GrepTool(),
        TaskCreateTool(),
        TaskListTool(),
        TaskGetTool(),
        TaskUpdateTool(),
    ):
        registry.register(tool)
    if exclude:
        registry = registry.subset(exclude=exclude)
    for tool in extra or ():
        registry.register(tool)
    return registry


def empty_tools(*extra: Tool) -> ToolRegistry:
    """Return a :class:`ToolRegistry` with only the supplied tools registered.

    Example::

        registry = empty_tools(RetrieveDocs(store=my_store), RunSQL(db=conn))

    Args:
        *extra: Tools to register in the otherwise empty registry.

    Returns:
        Registry containing only the supplied tools.
    """
    registry = ToolRegistry()
    for tool in extra:
        registry.register(tool)
    return registry
