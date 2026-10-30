"""Optional mem0 semantic recall layered onto Linch's public context hooks.

This recipe intentionally has no mem0 dependency.  Hosts provide an
``AsyncMemory``-compatible client in run dependencies under ``mem0`` and a
``user_id`` alongside it::

    deps = {"mem0": memory, "user_id": "user-123"}
    agent = Agent(hooks=[ContextInjectionHook(Mem0ContextBuilder()),
                         Mem0RecallHook()], ...)

The builder searches before each provider call.  The terminal hook stores the
successful conversation after the run.  Both operations carry the run's user
identity so one agent can safely serve concurrent users.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from linch import (
    ContextBuildResult,
    ContextBuildTurn,
    Message,
    TextBlock,
)


@runtime_checkable
class AsyncMemoryClient(Protocol):
    async def search(
        self, query: str, *, filters: dict[str, str]
    ) -> Mapping[str, Sequence[Any]] | Sequence[Any]: ...

    async def add(self, messages: list[dict[str, str]], *, user_id: str) -> Any: ...


def _dependency(deps: Any, name: str) -> Any:
    if isinstance(deps, Mapping):
        return deps.get(name)
    return getattr(deps, name, None)


def _latest_user_text(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role != "user":
            continue
        text = "\n".join(
            block.text for block in message.content if isinstance(block, TextBlock) and block.text
        )
        text = _strip_environment(text)
        if text:
            return text
    return ""


def _hit_text(hit: Any) -> str:
    if isinstance(hit, str):
        return hit
    if isinstance(hit, Mapping):
        for key in ("memory", "text", "content"):
            value = hit.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _strip_environment(text: str) -> str:
    marker = "</env>"
    if marker in text:
        return text.split(marker, 1)[1].lstrip()
    return text


def _history_messages(messages: Sequence[Message]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for message in messages:
        text = "\n".join(
            block.text for block in message.content if isinstance(block, TextBlock) and block.text
        )
        text = _strip_environment(text)
        if text:
            result.append({"role": message.role, "content": text})
    return result


class Mem0ContextBuilder:
    """Search the run's mem0 client with the latest user message."""

    async def build(self, turn: ContextBuildTurn) -> ContextBuildResult:
        client = _dependency(turn.deps, "mem0")
        user_id = _dependency(turn.deps, "user_id")
        if client is None or not isinstance(user_id, str) or not user_id:
            return ContextBuildResult(metadata={"mem0_hits": 0})

        query = _latest_user_text(turn.messages)
        if not query:
            return ContextBuildResult(metadata={"mem0_hits": 0})

        raw_hits = await client.search(query, filters={"user_id": user_id})
        hits = raw_hits.get("results", ()) if isinstance(raw_hits, Mapping) else raw_hits
        if isinstance(hits, (str, bytes)) or not isinstance(hits, Sequence):
            hits = ()
        memories = [memory for hit in hits if (memory := _hit_text(hit))]
        if not memories:
            return ContextBuildResult(metadata={"mem0_hits": 0})

        memory_text = "Relevant memories:\n" + "\n".join(f"- {memory}" for memory in memories)
        return ContextBuildResult(
            messages=[
                Message(
                    role="user",
                    content=[TextBlock(text=memory_text)],
                )
            ],
            metadata={"mem0_hits": len(memories)},
        )


class Mem0RecallHook:
    """Store each successful run once through the run's mem0 client."""

    name = "mem0_recall"

    async def on_agent_stop(self, ctx: Any) -> None:
        if getattr(ctx.result, "subtype", None) != "success":
            return

        client = _dependency(ctx.deps, "mem0")
        user_id = _dependency(ctx.deps, "user_id")
        if client is None or not isinstance(user_id, str) or not user_id:
            return
        messages = _history_messages(getattr(ctx.session, "full_history", ()))
        if not messages:
            return

        await client.add(messages, user_id=user_id)
