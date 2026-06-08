"""Chat agent — a pure conversation agent with no tools.

Run:
    DEEPSEEK_API_KEY=sk-... python3 examples/core/chat_agent.py
    OPENAI_API_KEY=sk-... python3 examples/core/chat_agent.py

Demonstrates:
  - empty_tools() — no file or shell access whatsoever
  - replace_defaults=True to strip the SWE identity and write your own persona
  - OutputSchema for structured JSON replies (Path A: text-parse)
  - Multi-turn: the agent remembers earlier messages in the session
  - deps: injecting application state (a knowledge snippet) into the prompt
    via a ContextBuilder — the clean alternative to stuffing it in system_prompt

Use this pattern for: customer support, document Q&A, interview prep,
domain-specific assistants, or any agent that reasons without touching files.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# ── A trivial ContextBuilder that injects a static knowledge snippet ──────────
# In production this would call your vector store / knowledge graph.


class _PolicyContextBuilder:
    _POLICY = (
        "Refund policy: customers may return any item within 30 days. "
        "Digital downloads are non-refundable once accessed. "
        "Contact support@example.com to initiate a return.\n"
        "Shipping: standard 3–5 business days; express 1–2 days for $9.99; "
        "free standard shipping on orders over $50."
    )

    async def build(self, turn):
        from linch.context import ContextBuildResult
        from linch.types import Message, TextBlock

        # Inject only when it looks like a customer question (simple heuristic).
        last = _last_user_text(turn.messages)
        if not last:
            return ContextBuildResult()
        return ContextBuildResult(
            messages=[
                Message(
                    role="user",
                    content=[TextBlock(text=f"[policy]\n{self._POLICY}")],
                )
            ],
            metadata={"source": "policy_db"},
        )


def _last_user_text(messages) -> str:
    from linch.types import TextBlock

    for msg in reversed(messages):
        if msg.role == "user":
            for block in msg.content:
                if isinstance(block, TextBlock) and not block.text.startswith("<env>"):
                    return block.text
    return ""


# ── Agent factory ─────────────────────────────────────────────────────────────


def _make_agent():
    from linch import Agent
    from linch.config import FeatureFlags, SystemPromptConfig
    from linch.providers import OpenAIChatCompletionsProvider
    from linch.providers.openai_chat import OpenAIChatProviderOptions
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools
    from linch.types import OutputSchema

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAPI_KEY")
    if not deepseek_key and not openai_key:
        raise SystemExit("Set DEEPSEEK_API_KEY or OPENAI_API_KEY and re-run.")

    if deepseek_key:
        # DeepSeek supports json_object but not json_schema enforcement.
        # json_mode=True sends response_format={type:"json_object"};
        # the loop text-parses and validates the result against OutputSchema.
        provider = OpenAIChatCompletionsProvider(
            OpenAIChatProviderOptions(
                api_key=deepseek_key,
                base_url="https://api.deepseek.com",
                json_mode=True,
            )
        )
        model = "deepseek-v4-flash"
    else:
        # Real OpenAI: json_schema enforcement is supported natively.
        provider = OpenAIChatCompletionsProvider(OpenAIChatProviderOptions(api_key=openai_key))
        model = "gpt-5-nano-2025-08-07"

    return Agent(
        model=model,
        provider=provider,
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        loop_guard=None,
        system_prompt_config=SystemPromptConfig(
            replace_defaults=True,
            append=(
                "You are a friendly customer-support assistant for an online shop. "
                "Answer questions about orders, refunds, and shipping. "
                "Be concise and warm. If you don't know something, say so — never guess. "
                "Always respond with a JSON object containing 'answer' and 'topic'."
            ),
        ),
        # Structured output: every reply is {answer, topic}.
        # Because AnthropicProvider has structured_output=False, this works via
        # text-parse (the model writes JSON, the loop parses it).
        # For OpenAI providers with structured_output=True it is enforced natively.
        output_schema=OutputSchema(
            name="support_reply",
            schema={
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "topic": {
                        "type": "string",
                        "description": "Category: refund, shipping, or other.",
                    },
                },
                "required": ["answer", "topic"],
                "additionalProperties": False,
            },
        ),
        context_builder=_PolicyContextBuilder(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
    )


# ── Entry point ───────────────────────────────────────────────────────────────


async def main() -> None:
    agent = _make_agent()
    session = await agent.session()

    questions = [
        "Hi, I bought a jacket last week — can I return it?",
        "How long does express shipping take and what does it cost?",
        "What if I already opened the digital download I bought?",
    ]

    for question in questions:
        print(f"\nCustomer: {question}")
        async for event in session.run(question):
            if event.type == "result":
                out = event.structured_output
                if out:
                    print(f"Agent [{out['topic']}]: {out['answer']}")
                else:
                    # Fallback when JSON parse fails (shouldn't happen for gpt/deepseek)
                    print(f"Agent: {event.final_text}")
                    if event.structured_error:
                        print(f"  (parse error: {event.structured_error})")
            elif event.type == "error":
                print(f"ERROR: {event.error.get('message', '')}")

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
