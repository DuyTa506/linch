"""Host tools: async Python functions the server calls with `tool.call`."""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

Scope = Literal["read", "write", "exec"]
ToolFn = Callable[..., Awaitable[Any]]

_SCOPES = ("read", "write", "exec")
_JSON_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass(frozen=True)
class ToolOutput:
    """What a host tool returns to attach metadata to its value.

    The model reads `value`; `metadata` is for the host only and arrives in
    the call's `ToolCallEndEvent.metadata`. A tool that returns anything
    else returns a value with no metadata.

    Attributes:
        value: Any JSON value; what the model reads.
        metadata: JSON-safe details for the host, never shown to the model.
    """

    value: Any
    metadata: Mapping[str, Any] | None = None

    def wire(self) -> dict[str, Any]:
        """The `tool.call` result: `{"value", "metadata"?}`."""
        if self.metadata is None:
            return {"value": self.value}
        return {"value": self.value, "metadata": dict(self.metadata)}


@dataclass(frozen=True)
class HostTool:
    """A host tool declaration and the function that runs it; made by `tool`."""

    name: str
    description: str
    input_schema: dict[str, Any]
    scope: Scope
    timeout_ms: int | None
    fn: ToolFn

    def spec(self) -> dict[str, Any]:
        """The declaration as `agent.open` takes it in `tools`."""
        spec: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "scope": self.scope,
        }
        if self.timeout_ms is not None:
            spec["timeout_ms"] = self.timeout_ms
        return spec


def tool(
    *,
    scope: Scope,
    name: str | None = None,
    description: str | None = None,
    input_schema: Mapping[str, Any] | None = None,
    timeout: float | None = None,
) -> Callable[[ToolFn], HostTool]:
    """Declare an async function as a host tool.

    The server calls it with the model's input, already checked against the
    schema, as keyword arguments; its return value (any JSON value) is what
    the model reads, and an exception becomes a tool error whose message the
    model reads. Return a `ToolOutput` to add metadata only the host sees. A
    `$/cancelRequest` from the server cancels its task.

    Args:
        scope: `read`, `write` or `exec`; required, with no default. It
            decides approval like any tool's, and only `read` calls run in
            parallel.
        name: Tool name; the function's name when absent.
        description: What the model is told; the docstring when absent.
        input_schema: JSON schema of the input; built from the parameters'
            type hints (`str`, `int`, `float`, `bool`, `list`, `dict`,
            `Optional`) when absent.
        timeout: Seconds the server waits for an answer; 300 when absent.

    Returns:
        A decorator that turns the function into a `HostTool`.

    Raises:
        ValueError: The scope is not one of the three, or the timeout is
            not positive.
        TypeError: The function is not `async`, or a parameter has no
            supported type hint and no `input_schema` was given.
    """
    if scope not in _SCOPES:
        raise ValueError(f"scope must be one of {_SCOPES}, got {scope!r}")
    timeout_ms = None if timeout is None else to_millis(timeout, "timeout")

    def wrap(fn: ToolFn) -> HostTool:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"host tool {fn.__name__!r} must be an async function")
        schema = dict(input_schema) if input_schema is not None else schema_of(fn)
        return HostTool(
            name=name or fn.__name__,
            description=description if description is not None else inspect.getdoc(fn) or "",
            input_schema=schema,
            scope=scope,
            timeout_ms=timeout_ms,
            fn=fn,
        )

    return wrap


def schema_of(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build an object schema from a function's parameters and type hints.

    A parameter without a default and not `Optional` is required.

    Raises:
        TypeError: A parameter is positional-only, variadic, or has no
            supported type hint.
    """
    hints = typing.get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in inspect.signature(fn).parameters.values():
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            raise TypeError(f"parameter {param.name!r} must be passable by keyword")
        if param.name not in hints:
            raise TypeError(f"parameter {param.name!r} needs a type hint")
        json_type, optional = _json_type(hints[param.name], param.name)
        properties[param.name] = {"type": json_type}
        if param.default is param.empty and not optional:
            required.append(param.name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _json_type(hint: Any, name: str) -> tuple[str, bool]:
    args: tuple[Any, ...] = typing.get_args(hint)
    origin: Any = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType) and type(None) in args:
        rest = [arg for arg in args if arg is not type(None)]
        if len(rest) == 1:
            return _json_type(rest[0], name)[0], True
    json_type = _JSON_TYPES.get(origin if origin is not None else hint)
    if json_type is None:
        raise TypeError(f"parameter {name!r}: unsupported type hint {hint!r}; pass input_schema")
    return json_type, False


def to_millis(seconds: float, what: str) -> int:
    """Convert a positive number of seconds to whole milliseconds, at least 1."""
    if seconds <= 0:
        raise ValueError(f"{what} must be positive, got {seconds!r}")
    return max(1, round(seconds * 1000))
