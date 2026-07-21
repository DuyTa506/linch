"""Custom/class tool skeleton contribution pack."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from ..contributions import FileContribution
from ..ir import CompilerIR, ToolIR
from .common import class_name, file, py


class ToolPack:
    capability_id = "tools.skeleton"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        if not ir.tools:
            return ()
        files: list[FileContribution] = [
            file(
                f"src/{ir.package}/tools/_validation.py",
                _validation_module(),
                "tools.validation",
            )
        ]
        symbols = _tool_symbols(ir.tools)
        imports: list[str] = []
        instances: list[str] = []
        for tool in ir.tools:
            symbol = symbols[tool.id]
            imports.append(f"from .{tool.id} import {symbol}")
            instances.append(f"{symbol}()")
            files.append(
                file(
                    f"src/{ir.package}/tools/{tool.id}.py",
                    _tool_module(tool, symbol),
                    f"tool.{tool.id}.skeleton",
                )
            )
        files.append(
            file(
                f"src/{ir.package}/tools/__init__.py",
                _init_module(imports, instances),
                "tools.registry",
            )
        )
        files.append(file("tests/test_tools.py", _tests(ir, symbols), "tests.tool_contracts"))
        return tuple(files)


def _tool_symbols(tools: tuple[ToolIR, ...]) -> dict[str, str]:
    """Map each tool id to a collision-free ``<Name>Tool`` class symbol.

    ``class_name`` drops underscores, so distinct ids can render to the same
    text (``tool_1`` and ``tool1`` both become ``Tool1``). An id that collides
    with another falls back to capitalizing the raw id instead, which stays
    unique because ids are already validated unique project-wide.
    """

    names = {tool.id: class_name(tool.id) for tool in tools}
    counts = Counter(names.values())
    return {
        tool_id: (f"{name}Tool" if counts[name] == 1 else f"{tool_id[0].upper()}{tool_id[1:]}Tool")
        for tool_id, name in names.items()
    }


def _tool_module(tool: ToolIR, symbol: str) -> str:
    schema = json.loads(tool.input_schema_json)
    rendered_schema = py(schema).replace("\n", "\n    ")
    resources = ", ".join(
        f"ResourceAccess(resource={py(item.resource)}, mode={py(item.mode)})"
        for item in tool.resources
    )
    execute_body = f"""\
        # TODO: implement the integration. Never return placeholder success.
        return ToolResult(
            is_error=True,
            content="TODO: implement {tool.id}; no external operation was performed.",
        )"""
    extra_import = ""
    if tool.kind == "database":
        extra_import = "import json\n"
        if tool.operation == "write":
            key = tool.idempotency_argument or "idempotency_key"
            execute_body = f"""\
        client = getattr(ctx.deps, "database", None)
        if client is None:
            return ToolResult(is_error=True, content="TODO: configure DatabaseClient in AppDeps.")
        idempotency_key = value.get({py(key)})
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return ToolResult(is_error=True, content="A non-empty idempotency key is required.")
        result = await client.write(
            {py(tool.id)},
            value,
            idempotency_key=f"{{idempotency_key}}:{{ctx.idempotency_key}}",
        )
        return ToolResult(content=json.dumps(result, ensure_ascii=False, sort_keys=True))"""
        else:
            execute_body = f"""\
        client = getattr(ctx.deps, "database", None)
        if client is None:
            return ToolResult(is_error=True, content="TODO: configure DatabaseClient in AppDeps.")
        rows = await client.read({py(tool.id)}, value)
        return ToolResult(content=json.dumps(rows, ensure_ascii=False, sort_keys=True))"""
    return f'''\
"""{tool.display_name} tool skeleton."""

from __future__ import annotations

{extra_import}from typing import Any

from linch import ResourceAccess, ToolContext, ToolResult

from ._validation import validate_json_object


class {symbol}:
    name = {py(tool.id)}
    description = {py(tool.description)}
    input_schema = {rendered_schema}
    scope = {py(tool.scope)}
    parallel = {py(tool.parallel)}
    retryable = {py(tool.retryable)}
    execution_timeout_ms = {py(tool.timeout_ms)}

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return validate_json_object(raw, self.input_schema)

    def summarize(self, value: dict[str, Any]) -> str:
        return f"{tool.id}: {{sorted(value)}}"

    def resources(self, value: dict[str, Any]) -> list[ResourceAccess]:
        return [{resources}]

    async def execute(self, value: dict[str, Any], ctx: ToolContext) -> ToolResult:
{execute_body}
'''


def _validation_module() -> str:
    return '''\
"""Small JSON-Schema subset validator for generated tool contract tests.

The provider receives the complete declared schema. Runtime validation here is
intentionally conservative and covers object fields/types without executing code.
"""

from __future__ import annotations

from typing import Any


def validate_json_object(raw: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("tool input must be an object")
    required = schema.get("required", [])
    for name in required if isinstance(required, list) else []:
        if name not in raw:
            raise ValueError(f"missing required input: {name}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(raw) - set(properties))
        if unexpected:
            raise ValueError(f"unexpected input: {unexpected[0]}")
    expected_types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    for name, value in raw.items():
        declaration = properties.get(name)
        if not isinstance(declaration, dict):
            continue
        expected = declaration.get("type")
        if not isinstance(expected, str):
            continue
        python_type = expected_types.get(expected)
        if python_type is None:
            continue
        if expected in {"integer", "number"} and isinstance(value, bool):
            raise ValueError(f"{name} has the wrong type")
        if not isinstance(value, python_type):
            raise ValueError(f"{name} has the wrong type")
    return dict(raw)
'''


def _init_module(imports: list[str], instances: list[str]) -> str:
    exports = [line.rsplit(" ", 1)[-1] for line in imports]
    if len(instances) == 1:
        tools_tuple = f"({instances[0]},)"
    else:
        rendered_instances = "\n".join(f"    {item}," for item in instances)
        tools_tuple = f"(\n{rendered_instances}\n)"
    return (
        '"""Project-defined tool skeletons."""\n\n'
        + "\n".join(sorted(imports))
        + f"\n\nALL_TOOLS = {tools_tuple}\n\n"
        + f"__all__ = {py(['ALL_TOOLS', *exports])}\n"
    )


def _tests(ir: CompilerIR, symbols: dict[str, str]) -> str:
    imports: list[str] = []
    tests: list[str] = []
    for tool in ir.tools:
        symbol = symbols[tool.id]
        imports.append(f"from {ir.package}.tools.{tool.id} import {symbol}")
        schema = json.loads(tool.input_schema_json)
        valid = _example(schema)
        tests.append(
            f"""\
async def test_{tool.id}_contract_is_explicit_todo() -> None:
    result = await assert_tool_contract(
        {symbol}(),
        valid_input={py(valid)},
    )
    assert result.is_error is True
    assert result.content.startswith("TODO:")
"""
        )
    return (
        '"""Contract tests for generated tools; implementations must replace '
        'these TODO checks."""\n\n'
        "from linch import assert_tool_contract\n\n"
        + "\n".join(sorted(imports))
        + "\n\n\n"
        + "\n\n".join(tests)
    )


def _example(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return {}
    required = schema.get("required", [])
    names = required if isinstance(required, list) else []
    example: dict[str, Any] = {}
    for name in names:
        if isinstance(name, str):
            example[name] = _example_value(properties.get(name, {}))
    return example


def _example_value(declaration: Any) -> Any:
    if not isinstance(declaration, dict):
        return "example"
    if "const" in declaration:
        return declaration["const"]
    enum = declaration.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    value_type = declaration.get("type")
    if not isinstance(value_type, str):
        return "example"
    return {
        "string": "example",
        "integer": 1,
        "number": 1.0,
        "boolean": True,
        "array": [],
        "object": {},
        "null": None,
    }.get(value_type, "example")


__all__ = ["ToolPack"]
