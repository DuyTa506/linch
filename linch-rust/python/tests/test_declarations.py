"""Host tool and callback declarations, built without a server."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from linch_rust import ToolContext, callback, tool


def test_the_schema_comes_from_simple_type_hints() -> None:
    @tool(scope="read")
    async def search(
        query: str,
        limit: int,
        ratio: float,
        exact: bool,
        tags: list[str],
        filters: dict[str, Any],
        cursor: Optional[str],  # noqa: UP045 - the typing spelling is supported too
        page: int | None = None,
        size: int = 10,
    ) -> list[str]:
        """Search the catalog."""
        return []

    assert search.name == "search"
    assert search.description == "Search the catalog."
    assert search.spec() == {
        "name": "search",
        "description": "Search the catalog.",
        "scope": "read",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "ratio": {"type": "number"},
                "exact": {"type": "boolean"},
                "tags": {"type": "array"},
                "filters": {"type": "object"},
                "cursor": {"type": "string"},
                "page": {"type": "integer"},
                "size": {"type": "integer"},
            },
            "required": ["query", "limit", "ratio", "exact", "tags", "filters"],
            "additionalProperties": False,
        },
    }


def test_an_explicit_schema_name_and_timeout_are_used_as_given() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "integer", "minimum": 0}}}

    @tool(scope="exec", name="run_job", description="Run a job.", input_schema=schema, timeout=1.5)
    async def job(**input: Any) -> None:
        return None

    assert job.spec() == {
        "name": "run_job",
        "description": "Run a job.",
        "input_schema": schema,
        "scope": "exec",
        "timeout_ms": 1500,
    }


def test_a_context_parameter_is_left_out_of_the_schema() -> None:
    @tool(scope="read")
    async def lookup(email: str, ctx: ToolContext) -> str:
        return email

    @tool(scope="read")
    async def audit(note: str, context: ToolContext) -> str:
        return note

    @tool(scope="exec", input_schema={"type": "object"})
    async def job(ctx: Any, **input: Any) -> None:
        return None

    @tool(scope="read")
    async def plain(email: str) -> str:
        return email

    assert lookup.input_schema["properties"] == {"email": {"type": "string"}}
    assert lookup.input_schema["required"] == ["email"]
    assert audit.input_schema["properties"] == {"note": {"type": "string"}}
    assert (lookup.context_param, audit.context_param, job.context_param) == (
        "ctx",
        "context",
        "ctx",
    )
    assert plain.context_param is None


def test_a_tool_needs_a_valid_scope_and_an_async_function() -> None:
    with pytest.raises(TypeError):
        tool()  # type: ignore[call-arg]  # scope has no default (#99)
    with pytest.raises(ValueError, match="scope"):
        tool(scope="admin")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout"):
        tool(scope="read", timeout=0)

    def sync(q: str) -> str:
        return q

    with pytest.raises(TypeError, match="async"):
        tool(scope="read")(sync)  # type: ignore[arg-type]


def test_an_unsupported_hint_asks_for_an_explicit_schema() -> None:
    async def odd(when: complex) -> None:
        return None

    async def untyped(q) -> None:  # type: ignore[no-untyped-def]
        return None

    with pytest.raises(TypeError, match="input_schema"):
        tool(scope="read")(odd)
    with pytest.raises(TypeError, match="type hint"):
        tool(scope="read")(untyped)


def test_a_callback_declaration_carries_only_what_was_set() -> None:
    @callback("stop", "keep-going", 2)
    async def bare(request: dict[str, Any]) -> dict[str, Any]:
        return {"action": "allow"}

    @callback(
        "permission_request",
        "approvals",
        3,
        matcher=["Bash", "mcp__git__*"],
        timeout=0.25,
        fail_mode="open",
    )
    async def full(request: dict[str, Any]) -> dict[str, Any]:
        return {"decision": "allow"}

    assert bare.spec() == {"point": "stop", "policy_id": "keep-going", "policy_version": 2}
    assert full.spec() == {
        "point": "permission_request",
        "policy_id": "approvals",
        "policy_version": 3,
        "matcher": ["Bash", "mcp__git__*"],
        "timeout_ms": 250,
        "fail_mode": "open",
    }
