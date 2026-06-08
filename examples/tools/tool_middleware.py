"""Tool middleware example.

Run:
    python3 examples/tools/tool_middleware.py

Demonstrates:
  1. Rewriting validated tool input before execution
  2. Blocking a tool call with a normal tool error result
  3. Redacting tool output before it enters provider history

This example uses a tiny fake provider so it runs without an API key.
In a real agent, pass the same middleware objects to Agent(middleware=...).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from typing import Any

from linch import (
    Agent,
    BaseProvider,
    MiddlewareContext,
    ToolCallMiddlewareInput,
    ToolCallMiddlewareResult,
    ToolContext,
    ToolResult,
    Usage,
)
from linch.sessions import InMemorySessionStore
from linch.tools.registry import empty_tools


class SearchDocsTool:
    name = "search_docs"
    description = "Search internal docs."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }
    scope = "read"
    parallel = True

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        query = raw.get("query")
        if not isinstance(query, str) or query.strip() == "":
            raise ValueError("query is required")
        return {
            "query": query,
            "max_results": int(raw.get("max_results", 5)),
        }

    def summarize(self, input: dict[str, Any]) -> str:
        return f"search_docs({input['query']!r}, max_results={input['max_results']})"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        content = (
            f"Returned {input['max_results']} docs for {input['query']!r}.\n"
            "Owner email: owner@example.com\n"
            "API token: sk-demo-secret-token"
        )
        return ToolResult(content=content, metadata={"raw_results": input["max_results"]})


class ToolGovernanceMiddleware:
    """Clamp search breadth, block sensitive queries, and redact secrets."""

    def before_tool_call(
        self,
        call: ToolCallMiddlewareInput,
        ctx: MiddlewareContext,
    ) -> ToolCallMiddlewareResult | None:
        if call.tool_name != "search_docs":
            return None

        query = call.input["query"].lower()
        if "private" in query or "credential" in query:
            return ToolCallMiddlewareResult(error="Blocked search for sensitive material.")

        return ToolCallMiddlewareResult(
            input={
                **call.input,
                "max_results": min(call.input.get("max_results", 5), 3),
            }
        )

    async def after_tool_result(
        self,
        call: ToolCallMiddlewareInput,
        result: ToolResult,
        ctx: MiddlewareContext,
    ) -> ToolResult:
        redacted = re.sub(r"[\w.-]+@[\w.-]+", "[redacted-email]", result.content)
        redacted = re.sub(r"sk-[A-Za-z0-9-]+", "[redacted-token]", redacted)
        metadata = {**result.metadata, "middleware": "redacted"}
        return replace(result, content=redacted, metadata=metadata)


class FakeProvider(BaseProvider):
    def __init__(self, *, query: str, max_results: int) -> None:
        self.query = query
        self.max_results = max_results
        self.requests: list[Any] = []

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any) -> Any:
        self.requests.append(req)
        yield {"type": "message_start", "model": req.model}

        if len(self.requests) == 1:
            yield {"type": "tool_use_start", "id": "tool-1", "name": "search_docs"}
            yield {
                "type": "tool_use_input_delta",
                "id": "tool-1",
                "json_delta": json.dumps({"query": self.query, "max_results": self.max_results}),
            }
            yield {"type": "tool_use_end", "id": "tool-1"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            return

        yield {"type": "text_delta", "text": "Done."}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


async def run_scenario(label: str, *, query: str, max_results: int) -> None:
    print(f"\n== {label} ==")

    provider = FakeProvider(query=query, max_results=max_results)
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=empty_tools(SearchDocsTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        middleware=ToolGovernanceMiddleware(),
    )
    session = await agent.session()

    async for event in session.run("Search the docs."):
        if event.type == "tool_call_start":
            print("tool input:", event.input)
        elif event.type == "tool_call_end":
            print("is error:", event.is_error)
            print("result:")
            print(event.result)

    await agent.close()


async def main() -> None:
    await run_scenario(
        "rewrite input and redact result",
        query="deployment guide",
        max_results=25,
    )
    await run_scenario(
        "block sensitive query",
        query="private credentials",
        max_results=5,
    )


if __name__ == "__main__":
    asyncio.run(main())
