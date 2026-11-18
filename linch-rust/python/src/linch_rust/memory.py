"""External and long-term memory through the `context_build` callback.

A `Retriever` is any store you search: a vector index, Notion, an Obsidian
vault, a SQL table. `retrieval` turns one into a `context_build` callback
that, before every model call, searches the store with the user's latest
question and hands the model what it found::

    agent = await server.open_agent(config, callbacks=[retrieval(store, k=3)])

The found text rides only that one request, after the conversation, as a
block of the last user message; it is never logged in the session, so the
cached prefix of every later request stays the same and the session does
not grow with retrieved text. The ids and sources land on the call's
`ContextBuildEvent` as `metadata["memory"]`, for observers and evals.

Writing memories back is a host tool the model calls (see the
`external_memory.py` example). Python Linch's `RagConnector` and
`RetrievedChunk` have the same shape; its blocks are system blocks, these
ride the tail of the request so they never move the cached prefix.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .callbacks import Callback, callback

_REMINDER = "<system-reminder>"


@dataclass(frozen=True)
class Chunk:
    """One piece of text a `Retriever` found.

    Attributes:
        id: Stable id, quoted to the model as `[doc:{id}]`; no `]` or line
            break.
        source: Where it came from (a path, a URL, a page title).
        text: What the model reads.
        score: The store's relevance score; only reported.
        metadata: Anything else the store wants observers to see.
    """

    id: str
    source: str
    text: str
    score: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict[str, Any])


class Retriever(Protocol):
    """A store the agent's memory is searched in."""

    def retrieve(self, query: str, *, k: int) -> Sequence[Chunk] | Awaitable[Sequence[Chunk]]:
        """At most `k` chunks for `query`, best first; a coroutine is awaited."""
        ...


def latest_question(messages: Sequence[Mapping[str, Any]]) -> str:
    """The text of the latest user message that the user wrote.

    Tool results and `<system-reminder>` feedback are skipped, so during a
    run's tool calls the question stays the one that started them.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        texts = [
            str(block.get("text", ""))
            for block in message.get("content", [])
            if block.get("type") == "text" and not str(block.get("text", "")).startswith(_REMINDER)
        ]
        if texts:
            return "\n".join(texts)
    return ""


def render(chunk: Chunk) -> str:
    """The block the model reads for `chunk`, as Python Linch's RAG writes it."""
    return f"[doc:{chunk.id}]\nSource: {chunk.source}\n{chunk.text}"


def retrieval(
    retriever: Retriever,
    *,
    k: int = 4,
    policy_id: str = "memory",
    policy_version: int = 1,
    timeout: float | None = None,
) -> Callback:
    """A `context_build` callback that adds what `retriever` finds to each request.

    Args:
        retriever: The store to search.
        k: Most chunks per request.
        policy_id: Names the callback in the run contract.
        policy_version: Bump it when the store's behaviour changes, so a
            resumed run notices.
        timeout: Seconds the server waits for the store; no limit when
            absent. A failure or a timeout adds nothing: `context_build`
            fails open.

    Returns:
        The callback to pass to `Server.open_agent(callbacks=...)`.

    Raises:
        ValueError: `k` is not positive, or a chunk id holds `]` or a line
            break (raised in the callback, so the request goes without
            memory).
    """
    if k < 1:
        raise ValueError(f"k must be positive, got {k}")

    @callback("context_build", policy_id, policy_version, timeout=timeout)
    async def build(request: dict[str, Any]) -> dict[str, Any]:
        query = latest_question(request.get("messages", []))
        if not query:
            return {}
        found = retriever.retrieve(query, k=k)
        chunks = list(await found if isinstance(found, Awaitable) else found)[:k]
        for chunk in chunks:
            if not chunk.id or any(c in chunk.id for c in "]\r\n"):
                message = f"chunk id {chunk.id!r} must be non-empty, with no ] or line break"
                raise ValueError(message)
        if not chunks:
            return {}
        return {
            "blocks": [{"text": render(chunk)} for chunk in chunks],
            "metadata": {
                "memory": {
                    "ids": [chunk.id for chunk in chunks],
                    "sources": [chunk.source for chunk in chunks],
                }
            },
        }

    return build
