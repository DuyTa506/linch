"""Redaction governance example.

Run:
    python3 examples/core/governance_redaction.py

Demonstrates the :class:`RedactionHook` governance seam:
  1. Host-supplied regex rules — Linch ships *no* default patterns.
  2. Tool results scrubbed before they re-enter provider history (PostToolUse).
  3. The final answer scrubbed before it reaches the caller (BeforeFinalAnswer).

The *mechanism* (apply rules) lives in Linch; the *policy* (which patterns are
sensitive, what to mask them with) stays with the embedder. This keeps core
domain-neutral — there is no built-in PII/PHI classifier.

Uses ``ScriptedProvider`` so it runs without an API key.
"""

from __future__ import annotations

import asyncio
from typing import Any

from linch import (
    Agent,
    RedactionConfig,
    RedactionHook,
    RedactionRule,
    ResultEvent,
    ToolCallEndEvent,
    ToolContext,
    ToolResult,
)
from linch.config import FeatureFlags
from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
from linch.sessions import InMemorySessionStore
from linch.tools.registry import empty_tools


class LookupUserTool:
    """A tool whose raw output contains an email and an API key."""

    name = "lookup_user"
    description = "Look up a user record."
    input_schema = {
        "type": "object",
        "properties": {"user_id": {"type": "string"}},
        "required": ["user_id"],
    }
    scope = "read"
    parallel = True

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        user_id = raw.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id is required")
        return {"user_id": user_id}

    def summarize(self, input: dict[str, Any]) -> str:
        return f"lookup_user({input['user_id']!r})"

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            content=(
                f"user {input['user_id']}: "
                "email=alice@corp.com, api_key=sk-live-ABCDEF0123456789XYZ"
            )
        )


# Policy lives HERE, in the host — not in Linch.
GOVERNANCE = RedactionConfig(
    rules=(
        RedactionRule(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[EMAIL]"),
        RedactionRule(r"sk-[A-Za-z0-9-]{16,}", "[API_KEY]"),
    )
)


async def main() -> None:
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="lookup_user", tool_input={"user_id": "u-42"}),
            # The model parrots a secret back into its final answer.
            TextTurn("Found the user. Their key is sk-live-ABCDEF0123456789XYZ."),
        ]
    )
    agent = Agent(
        model="fake-model",
        provider=provider,
        tools=empty_tools(LookupUserTool()),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        hooks=[RedactionHook(GOVERNANCE)],
    )

    session = await agent.session()
    async for event in session.run("Look up user u-42."):
        if isinstance(event, ToolCallEndEvent):
            print(f"tool result (scrubbed): {event.result}")
        elif isinstance(event, ResultEvent):
            print(f"final answer (scrubbed): {event.final_text}")


if __name__ == "__main__":
    asyncio.run(main())
