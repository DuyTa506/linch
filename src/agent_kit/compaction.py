from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .abort import AbortContext
from .events import CompactionEvent
from .types import Message, SystemBlock, TextBlock


@dataclass
class CompactionContext:
    messages: list[Message]
    model: str
    signal: AbortContext


@runtime_checkable
class CompactionStrategy(Protocol):
    id: str

    async def compact(self, ctx: CompactionContext, provider: Any) -> list[Message]:
        """Compact *ctx.messages* using *provider* (a :class:`~agent_kit.providers.BaseProvider`).

        .. note::
            The parameter was historically named ``openai`` in the 0.1.x
            series.  :class:`DefaultCompaction` and ``_run_compaction_impl``
            pass ``agent.provider`` now, but for backward compatibility
            ``agent.openai`` (an alias for ``agent.provider``) still works if
            you reference it by keyword.
        """
        ...


def last_n_turn_boundaries(messages: list[Message], n: int) -> list[Message]:
    assistants_found = 0
    boundary_idx = -1

    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            assistants_found += 1
            if assistants_found == n:
                boundary_idx = i
                break

    if assistants_found < n:
        return messages

    return messages[boundary_idx:]


_SUMMARY_PROMPT = (
    "Summarize the conversation so far. Capture:\n"
    "- Decisions made\n"
    "- Files read, written, or edited (with paths)\n"
    "- Tool calls made and their outcomes\n"
    "- Open questions and current task state\n"
    "Keep it factual and dense. Omit greetings, restated goals, and meta commentary."
)


async def summarize_with_provider(
    provider: Any,
    model: str,
    older: list[Message],
    signal: AbortContext,
) -> str:
    """Summarise *older* messages using *provider* and return the summary text.

    The provider's ``stream()`` already yields normalized wire events
    (``text_delta``, ``message_end``, …) so we consume them directly without
    wrapping through any provider-specific translator.
    """
    from .types import ProviderRequest

    req = ProviderRequest(
        model=model,
        system=[SystemBlock(text=_SUMMARY_PROMPT, cacheable=False)],
        tools=[],
        messages=older,
        max_output_tokens=4096,
        signal=signal,
    )

    text = ""
    async for ev in provider.stream(req):
        if ev["type"] == "text_delta":
            text += ev["text"]
        elif ev["type"] == "message_end":
            break

    return text.strip()


class DefaultCompaction:
    id = "default-keep-recent-10"

    async def compact(self, ctx: CompactionContext, provider: Any) -> list[Message]:
        recent = last_n_turn_boundaries(ctx.messages, 10)
        boundary = len(ctx.messages) - len(recent)
        older = ctx.messages[:boundary] if boundary > 0 else []

        if not older:
            return ctx.messages

        summary_text = await summarize_with_provider(provider, ctx.model, older, ctx.signal)

        summary_msg = Message(
            role="user",
            content=[TextBlock(text=f"<summary of earlier conversation>\n\n{summary_text}")],
        )

        return [summary_msg, *recent]


default_compaction = DefaultCompaction()


async def _run_compaction_impl(
    session: Any,
    agent: Any,
    signal: AbortContext,
    strategy: Any,
) -> None:
    messages_before = len(session.provider_view)
    tokens_before = _estimate_tokens(agent, session.provider_view)

    snapshot = list(session.provider_view)
    ctx = CompactionContext(
        messages=snapshot,
        model=agent.model,
        signal=signal,
    )
    compacted = await strategy.compact(ctx, agent.provider)

    session.provider_view.clear()
    session.provider_view.extend(compacted)

    session.last_compaction_info = {
        "type": "compaction",
        "messages_before": messages_before,
        "messages_after": len(session.provider_view),
        "tokens_before": tokens_before,
        "tokens_after": _estimate_tokens(agent, session.provider_view),
        "strategy": strategy.id,
    }


async def maybe_compact(
    session: Any,
    agent: Any,
    signal: AbortContext,
) -> bool:
    if session.last_usage is None:
        return False

    strategy = getattr(agent, "compaction", None) or default_compaction
    limit = agent.provider.context_window(agent.model)
    reserve = agent.max_output_tokens or 32768
    projected = _estimate_tokens(agent, session.provider_view) + reserve

    if projected < 0.8 * limit:
        return False

    length_before = len(session.provider_view)
    head_before = session.provider_view[0] if session.provider_view else None

    await _run_compaction_impl(session, agent, signal, strategy)

    if len(session.provider_view) == length_before:
        if session.provider_view and head_before is session.provider_view[0]:
            session.last_compaction_info = None
            return False

    return True


def _estimate_tokens(agent: Any, messages: list[Message]) -> int:
    estimator = getattr(agent, "token_estimator", None)
    if callable(estimator):
        try:
            value = estimator(messages, agent.model)
            return max(0, int(value))
        except Exception:
            pass

    # Simple fallback heuristic for compaction decisions and telemetry.
    chars = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                chars += len(block.text)
    return max(0, chars // 4)


async def run_forced_compaction(
    session: Any,
    agent: Any,
    signal: AbortContext,
) -> None:
    strategy = getattr(agent, "compaction", None) or default_compaction
    await _run_compaction_impl(session, agent, signal, strategy)


def build_compaction_event(session: Any) -> CompactionEvent:
    info = session.last_compaction_info
    if info is None:
        raise RuntimeError("last_compaction_info not set after compaction")
    return CompactionEvent(
        messages_before=info["messages_before"],
        messages_after=info["messages_after"],
        tokens_before=info["tokens_before"],
        tokens_after=info["tokens_after"],
        strategy=info["strategy"],
    )
