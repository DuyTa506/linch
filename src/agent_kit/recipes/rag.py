"""RAG (Retrieval-Augmented Generation) recipe.

Creates an agent that retrieves relevant documents before each provider call
and returns a structured ``{answer, citations}`` response.

Exercises: :class:`~agent_kit.context_hooks.ContextInjector`, ``deps``,
:class:`~agent_kit.types.OutputSchema`, tool-aware system prompt.

Quick start::

    from agent_kit.recipes.rag import rag_agent, RagDeps

    # Your vector store only needs a .search(query) -> str method
    agent = rag_agent(
        model="gpt-5",
        deps=RagDeps(vector_store=my_store),
    )

    session = await agent.session()
    async for event in session.run("What is our refund policy?"):
        if event.type == "result":
            print(event.structured_output)
            # {'answer': '...', 'citations': [...]}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agent import Agent
from ..context_hooks import TurnContext, prune_tagged
from ..tools.registry import empty_tools
from ..types import Message, OutputSchema, TextBlock
from . import build_agent

_RAG_TAG = "[[rag-context]]"

_ANSWER_SCHEMA = OutputSchema(
    name="rag_answer",
    schema={
        "type": "object",
        "properties": {
            "answer": {"type": "string", "description": "Answer to the user's question."},
            "citations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source identifiers or passages supporting the answer.",
            },
        },
        "required": ["answer", "citations"],
        "additionalProperties": False,
    },
    strict=True,
)


@dataclass
class RagDeps:
    """Dependency container for the RAG recipe.

    Attributes:
        vector_store: Any object with a ``search(query: str) -> str`` method.
            The return value is injected verbatim as context before each turn.
        top_k: Optional hint passed to ``vector_store.search`` if it accepts
            a ``top_k`` keyword argument.
    """

    vector_store: Any
    top_k: int = 5


def _last_user_text(provider_view: list[Message]) -> str:
    """Return the text of the most recent user message."""
    for msg in reversed(provider_view):
        if msg.role == "user":
            for block in msg.content:
                if isinstance(block, TextBlock) and not block.text.startswith("<env>"):
                    return block.text
    return ""


class _RagInjector:
    """Context injector that retrieves documents and prepends them each turn."""

    async def before_turn(self, ctx: TurnContext) -> None:
        # Remove previous turn's injected context
        prune_tagged(ctx.provider_view, _RAG_TAG)

        deps: RagDeps | None = ctx.deps if isinstance(ctx.deps, RagDeps) else None
        if deps is None or deps.vector_store is None:
            return

        query = _last_user_text(ctx.provider_view)
        if not query:
            return

        # Call the vector store; accept both .search(query) and .search(query, top_k=n)
        try:
            import inspect

            sig = inspect.signature(deps.vector_store.search)
            if "top_k" in sig.parameters:
                retrieved = await _maybe_await(deps.vector_store.search(query, top_k=deps.top_k))
            else:
                retrieved = await _maybe_await(deps.vector_store.search(query))
        except Exception:
            return

        if retrieved:
            ctx.provider_view.append(
                Message(
                    role="user",
                    content=[TextBlock(text=f"{_RAG_TAG}\nRetrieved context:\n{retrieved}")],
                )
            )


async def _maybe_await(value: Any) -> Any:
    """Await *value* if it is a coroutine, otherwise return as-is."""
    import asyncio

    if asyncio.iscoroutine(value):
        return await value
    return value


def rag_agent(
    *,
    model: str,
    deps: RagDeps | None = None,
    output_schema: OutputSchema | None = None,
    extra_instructions: str | None = None,
    **agent_kwargs: Any,
) -> Agent:
    """Create a RAG agent.

    Args:
        model: LLM model identifier.
        deps: :class:`RagDeps` containing the vector store.  You can also
            pass ``deps`` per-run via ``RunOptions(deps=RagDeps(...))`` if
            you need per-request stores.
        output_schema: Override the default ``{answer, citations}`` schema.
        extra_instructions: Additional system instructions appended after the
            base RAG prompt.
        **agent_kwargs: Forwarded to :func:`~agent_kit.recipes.build_agent`.

    Returns:
        An :class:`~agent_kit.agent.Agent` with no SWE tools and per-turn
        document injection.
    """
    base_instructions = (
        "You are a helpful assistant that answers questions based on the "
        "retrieved context provided before each turn.  Always cite your "
        "sources.  If the context does not contain enough information, say so "
        "clearly rather than guessing."
    )
    if extra_instructions:
        base_instructions = f"{base_instructions}\n\n{extra_instructions}"

    # No file/shell tools — retrieval is done via the injector
    registry = empty_tools()

    return build_agent(
        model=model,
        system_instructions=base_instructions,
        tools=registry,
        output_schema=output_schema or _ANSWER_SCHEMA,
        context_injectors=[_RagInjector()],
        deps=deps,
        replace_default_system=True,
        **agent_kwargs,
    )
