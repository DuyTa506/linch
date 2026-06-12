from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin

from .base import ResourceAccess, ToolContext, ToolResult, ToolScope

_NON_JSON_DEFAULT = object()


def _json_type(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Signature.empty:
        return {}
    if isinstance(annotation, str):
        if annotation in {"str", "builtins.str"}:
            return {"type": "string"}
        if annotation in {"int", "builtins.int"}:
            return {"type": "integer"}
        if annotation in {"float", "builtins.float"}:
            return {"type": "number"}
        if annotation in {"bool", "builtins.bool"}:
            return {"type": "boolean"}
        if annotation.startswith(("list[", "tuple[", "set[")) or annotation in {
            "list",
            "tuple",
            "set",
        }:
            return {"type": "array", "items": {}}
        if annotation.startswith("dict[") or annotation == "dict":
            return {"type": "object"}
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)
    target = origin or annotation
    if target is str:
        return {"type": "string"}
    if target is int:
        return {"type": "integer"}
    if target is float:
        return {"type": "number"}
    if target is bool:
        return {"type": "boolean"}
    if target in {list, tuple, set}:
        item_schema = _json_type(args[0]) if args else {}
        return {"type": "array", "items": item_schema}
    if target is dict:
        return {"type": "object"}
    return {}


def _is_ctx_param(name: str, annotation: Any) -> bool:
    if name == "ctx":
        return True
    if annotation is ToolContext:
        return True
    return isinstance(annotation, str) and annotation in {"ToolContext", "linch.tools.ToolContext"}


def _json_default(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return _NON_JSON_DEFAULT
    return value


def _make_schema(signature: inspect.Signature) -> tuple[dict[str, Any], list[str], list[str], bool]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    params: list[str] = []
    accepts_kwargs = False

    for name, param in signature.parameters.items():
        if _is_ctx_param(name, param.annotation):
            continue
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            accepts_kwargs = True
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise TypeError(f"tool function parameter {name!r} must be callable as a keyword")

        params.append(name)
        schema = _json_type(param.annotation)
        if param.default is inspect.Signature.empty:
            required.append(name)
        else:
            default = _json_default(param.default)
            if default is not _NON_JSON_DEFAULT:
                schema = dict(schema)
                schema["default"] = default
        properties[name] = schema

    schema_out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema_out["required"] = required
    return schema_out, required, params, accepts_kwargs


def _first_doc_line(fn: Callable[..., Any]) -> str:
    doc = inspect.getdoc(fn) or ""
    return doc.splitlines()[0] if doc else ""


def _summary_text(
    summary: str | Callable[[dict[str, Any]], str] | None,
    input: dict[str, Any],
    name: str,
) -> str:
    if callable(summary):
        return str(summary(input))
    if isinstance(summary, str) and summary:
        return summary
    return name


def _result_from_value(value: Any, summary: str) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult(content=value, summary=summary)
    if isinstance(value, dict | list):
        return ToolResult(content=json.dumps(value, ensure_ascii=False), summary=summary)
    return ToolResult(content=str(value), summary=summary)


async def _run_sync_callable(fn: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    # Bounded daemon-thread offload: keeps blocking user code off the event loop
    # without unbounded thread creation or interpreter-teardown hangs.
    from .._blocking import run_blocking

    return await run_blocking(fn, **kwargs)


@dataclass(slots=True)
class FunctionTool:
    fn: Callable[..., Any]
    name: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    scope: ToolScope = "read"
    parallel: bool = True
    tags: tuple[str, ...] = ()
    summary: str | Callable[[dict[str, Any]], str] | None = None
    resources_fn: Callable[[dict[str, Any]], list[ResourceAccess] | list[dict[str, Any]]] | None = (
        None
    )
    retryable: bool = False
    execution_timeout_ms: float | None = None
    _signature: inspect.Signature = field(init=False, repr=False)
    _required: list[str] = field(init=False, repr=False)
    _params: list[str] = field(init=False, repr=False)
    _accepts_kwargs: bool = field(init=False, repr=False)
    _ctx_param: str | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._signature = inspect.signature(self.fn)
        inferred, required, params, accepts_kwargs = _make_schema(self._signature)
        self.input_schema = self.input_schema or inferred
        self.name = self.name or self.fn.__name__
        self.description = self.description or _first_doc_line(self.fn) or str(self.name)
        self._required = required
        self._params = params
        self._accepts_kwargs = accepts_kwargs
        self._ctx_param = None
        for name, param in self._signature.parameters.items():
            if _is_ctx_param(name, param.annotation):
                self._ctx_param = name
                break

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        for name in self._required:
            if name not in raw:
                raise ValueError(f"{name} is required")
        if self._accepts_kwargs:
            return dict(raw)
        return {name: raw[name] for name in self._params if name in raw}

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        kwargs = dict(input)
        if self._ctx_param is not None:
            kwargs[self._ctx_param] = ctx
        if inspect.iscoroutinefunction(self.fn):
            value = await self.fn(**kwargs)
        else:
            # Run sync user functions off the event loop so blocking I/O or
            # CPU work doesn't stall the agent loop.
            value = await _run_sync_callable(self.fn, kwargs)
            if inspect.isawaitable(value):
                value = await value
        return _result_from_value(value, self.summarize(input))

    def summarize(self, input: dict[str, Any]) -> str:
        return _summary_text(self.summary, input, str(self.name))

    def resources(self, input: dict[str, Any]) -> list[ResourceAccess] | list[dict[str, Any]]:
        if self.resources_fn is None:
            return []
        return self.resources_fn(input)


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    input_schema: dict[str, Any] | None = None,
    scope: ToolScope = "read",
    parallel: bool = True,
    tags: tuple[str, ...] = (),
    summary: str | Callable[[dict[str, Any]], str] | None = None,
    resources: (
        Callable[[dict[str, Any]], list[ResourceAccess] | list[dict[str, Any]]] | None
    ) = None,
    retryable: bool = False,
    execution_timeout_ms: float | None = None,
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    def wrap(inner: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            inner,
            name=name,
            description=description,
            input_schema=input_schema,
            scope=scope,
            parallel=parallel,
            tags=tags,
            summary=summary,
            resources_fn=resources,
            retryable=retryable,
            execution_timeout_ms=execution_timeout_ms,
        )

    if fn is None:
        return wrap
    return wrap(fn)
