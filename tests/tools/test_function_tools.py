from __future__ import annotations

from typing import Any

import pytest


def _ctx(deps: Any = None):
    from linch.tools import ToolContext

    return ToolContext(cwd=".", session_id="s1", run_id="r1", session_store=None, deps=deps)


def test_bare_decorator_infers_schema_from_signature():
    from linch.tools import FunctionTool, tool

    @tool
    def greet(name: str, excited: bool = False) -> str:
        """Greet a user."""
        return f"hello {name}{'!' if excited else ''}"

    assert isinstance(greet, FunctionTool)
    assert greet.name == "greet"
    assert greet.description == "Greet a user."
    assert greet.input_schema == {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "excited": {"type": "boolean", "default": False},
        },
        "required": ["name"],
    }


def test_inferred_schema_preserves_default_none_as_json_null():
    from linch.tools import tool

    @tool
    def search(query: str, limit: int | None = None) -> str:
        return query

    assert search.input_schema["properties"]["limit"]["default"] is None


def test_configured_decorator_uses_explicit_metadata_and_schema():
    from linch.tools import tool

    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    @tool(
        name="SearchDocs",
        description="Search project docs.",
        input_schema=schema,
        scope="read",
        parallel=True,
        tags=("rag",),
    )
    def search_docs(query: str) -> str:
        return query

    assert search_docs.name == "SearchDocs"
    assert search_docs.description == "Search project docs."
    assert search_docs.input_schema is schema
    assert search_docs.tags == ("rag",)


def test_validate_requires_inferred_required_fields_and_filters_unknowns():
    from linch.tools import tool

    @tool
    def add(a: int, b: int = 1) -> str:
        return str(a + b)

    with pytest.raises(ValueError, match="a is required"):
        add.validate({"b": 2})

    assert add.validate({"a": 2, "b": 3, "ignored": True}) == {"a": 2, "b": 3}


@pytest.mark.asyncio
async def test_execute_supports_sync_async_and_return_conversion():
    from linch.tools import ToolResult, tool

    @tool
    def string_tool(value: str) -> str:
        return value

    @tool
    async def dict_tool(value: str) -> dict[str, str]:
        return {"value": value}

    @tool
    def result_tool() -> ToolResult:
        return ToolResult(content="rich", summary="custom")

    string_result = await string_tool.execute({"value": "ok"}, _ctx())
    dict_result = await dict_tool.execute({"value": "ok"}, _ctx())
    rich_result = await result_tool.execute({}, _ctx())

    assert string_result == ToolResult(content="ok", summary="string_tool")
    assert dict_result.content == '{"value": "ok"}'
    assert dict_result.summary == "dict_tool"
    assert rich_result == ToolResult(content="rich", summary="custom")


async def test_execute_runs_sync_function_off_the_event_loop_thread():
    """Sync function tools must not block the event loop — they run in a thread."""
    import threading

    from linch.tools import tool

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    @tool
    def blocking(value: str) -> str:
        seen["thread"] = threading.get_ident()
        return value

    await blocking.execute({"value": "ok"}, _ctx())

    assert seen["thread"] != loop_thread


@pytest.mark.asyncio
async def test_execute_injects_tool_context_when_requested():
    from linch.tools import ToolContext, tool

    deps = {"prefix": "value"}

    @tool
    def read_deps(key: str, ctx: ToolContext) -> str:
        return str(ctx.deps[key])

    result = await read_deps.execute({"key": "prefix"}, _ctx(deps))

    assert "ctx" not in read_deps.input_schema["properties"]
    assert result.content == "value"


def test_summary_resources_retry_timeout_and_tags_passthrough():
    from linch.tools import ResourceAccess, tool

    def resources(input: dict[str, Any]) -> list[ResourceAccess]:
        return [ResourceAccess(resource=f"record:{input['id']}", mode="read")]

    @tool(
        summary=lambda input: f"Lookup {input['id']}",
        resources=resources,
        retryable=True,
        execution_timeout_ms=2500,
        tags=("lookup",),
    )
    def lookup(id: str) -> str:
        return id

    assert lookup.summarize({"id": "a"}) == "Lookup a"
    assert lookup.resources({"id": "a"}) == [ResourceAccess(resource="record:a", mode="read")]
    assert lookup.retryable is True
    assert lookup.execution_timeout_ms == 2500
    assert lookup.tags == ("lookup",)


def test_function_tool_explicit_construction_and_registry_schema_export():
    from linch.tools import FunctionTool
    from linch.tools.registry import empty_tools

    def ping() -> str:
        return "pong"

    tool = FunctionTool(ping, name="Ping", description="Ping service.")
    registry = empty_tools(tool)

    assert registry.get("Ping") is tool
    assert registry.schemas() == [
        {
            "name": "Ping",
            "description": "Ping service.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
