from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from .abort import AbortContext
from .events import CompactionEvent
from .types import Message, SystemBlock, TextBlock, ToolResultBlock


@dataclass
class CompactionContext:
    messages: list[Message]
    model: str
    signal: AbortContext


@dataclass(slots=True)
class CompactionLadder:
    """Opt-in LLM-free recovery rungs that run before full summarization.

    Pass via ``Agent(compaction_ladder=CompactionLadder())``.  When unset
    (default ``None``) the loop behaves exactly as before.

    Attributes:
        micro: Enable micro-compact — elide old tool-result contents (no LLM
            call) both proactively (in :func:`maybe_compact`) and reactively
            (on ``ContextLengthError``, once per turn).
        keep_recent_turns: Tool results within the last N assistant turns are
            never elided.
        max_forced_compactions: Per-run circuit breaker on forced (LLM)
            compactions triggered by ``ContextLengthError``; once exhausted
            the error surfaces.
        reset_read_tracker: After any compaction (micro or forced) elides or
            summarizes away earlier messages, clear ``session.file_read_tracker``
            so a file whose contents left the context is re-read before it can
            be edited (the ``Edit`` tool gates on ``has_read``). Cheap insurance
            against blind edits on stale content; worst case is a redundant
            re-read of a file still present in the kept recent tail.
    """

    micro: bool = True
    keep_recent_turns: int = 10
    max_forced_compactions: int = 3
    reset_read_tracker: bool = True


_ELIDED = "[tool result elided to save context]"


def micro_compact(
    messages: list[Message], *, keep_recent_turns: int = 10
) -> tuple[list[Message], int]:
    """Return ``(new_messages, n_elided)`` with old tool-result contents elided.

    Pure and copy-on-write: never mutates *messages* or their blocks (they are
    shared with ``session.full_history``); untouched messages are reused by
    identity.  Only ``ToolResultBlock`` contents older than the last
    *keep_recent_turns* assistant turns are replaced, so every ``tool_use_id``
    stays paired and the message structure remains provider-valid.  No LLM call.
    """
    recent = last_n_turn_boundaries(messages, keep_recent_turns)
    boundary = len(messages) - len(recent)
    if boundary <= 0:
        return messages, 0

    n_elided = 0
    out: list[Message] = []
    for i, message in enumerate(messages):
        if i >= boundary:
            out.append(message)
            continue
        new_blocks: list[Any] | None = None
        for j, block in enumerate(message.content):
            if not isinstance(block, ToolResultBlock):
                continue
            content = block.content
            worthwhile = (isinstance(content, str) and len(content) > len(_ELIDED)) or (
                isinstance(content, list) and len(content) > 0
            )
            if not worthwhile or content == _ELIDED:
                continue
            if new_blocks is None:
                new_blocks = list(message.content)
            new_blocks[j] = ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=_ELIDED,
                is_error=block.is_error,
            )
            n_elided += 1
        if new_blocks is None:
            out.append(message)
        else:
            out.append(
                Message(
                    role=message.role,
                    content=new_blocks,
                    provider_metadata=message.provider_metadata,
                )
            )

    if n_elided == 0:
        return messages, 0
    return out, n_elided


def apply_micro_compaction(session: Any, agent: Any, *, keep_recent_turns: int) -> bool:
    """Elide old tool results in ``session.provider_view`` in place.

    Returns ``True`` and sets ``session.last_compaction_info`` (strategy
    ``"micro"``) when anything was elided; ``False`` leaves the session
    untouched.
    """
    tokens_before = _estimate_tokens(agent, session.provider_view)
    new_view, n_elided = micro_compact(session.provider_view, keep_recent_turns=keep_recent_turns)
    if n_elided == 0:
        return False
    messages_count = len(session.provider_view)
    session.provider_view[:] = new_view
    session.last_compaction_info = {
        "type": "compaction",
        "messages_before": messages_count,
        "messages_after": messages_count,
        "tokens_before": tokens_before,
        "tokens_after": _estimate_tokens(agent, session.provider_view),
        "strategy": "micro",
    }
    return True


def reset_read_tracker_after_compaction(session: Any, agent: Any) -> None:
    """Clear ``session.file_read_tracker`` after a compaction, if opted in.

    A compaction (micro elision or forced summary) can remove a file's contents
    from the provider view while the read tracker still records it as read. The
    ``Edit`` tool gates on ``has_read``, so a stale entry would permit a blind
    edit on content the model can no longer see. Resetting the tracker forces a
    fresh ``Read`` before the next edit.

    Opt-in via ``CompactionLadder(reset_read_tracker=True)`` (the ladder default);
    a no-op when no ladder is configured, so default behavior is byte-identical.
    """
    ladder = getattr(agent, "compaction_ladder", None)
    if ladder is None or not getattr(ladder, "reset_read_tracker", True):
        return
    tracker = getattr(session, "file_read_tracker", None)
    if tracker is not None:
        tracker.clear()


@runtime_checkable
class CompactionStrategy(Protocol):
    id: str

    async def compact(self, ctx: CompactionContext, provider: Any) -> list[Message]:
        """Compact *ctx.messages* using *provider* (a :class:`~linch.providers.BaseProvider`).

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

_DETAILED_SUMMARY_PROMPT = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
    "\n"
    "Create a continuation-safe summary of the earlier conversation. Capture enough "
    "technical detail for another agent to continue the work without access to the "
    "compacted messages.\n"
    "\n"
    "Before the final summary, you may use an <analysis> block to organize details. "
    "The final answer must include a <summary> block with these exact numbered "
    "sections:\n"
    "\n"
    "1. Primary Request and Intent: the user's explicit requests and intent.\n"
    "2. Files, Artifacts, and Code Sections: files, artifacts, data, or code read, "
    "modified, created, or discussed; include paths and relevant identifiers when known.\n"
    "3. Errors and Fixes: errors, failed commands, failed assumptions, and how they "
    "were resolved.\n"
    "4. Pending Tasks: outstanding tasks explicitly requested or discovered.\n"
    "5. Current Work: what was being worked on immediately before compaction.\n"
    "6. Next Step: the next concrete action aligned with the latest user request; "
    "write 'None' if the prior task was complete.\n"
    "\n"
    "Be factual and dense. Preserve exact paths, commands, API names, public "
    "interfaces, test results, and user corrections. Do not invent work that was "
    "not done."
)


async def summarize_with_provider(
    provider: Any,
    model: str,
    older: list[Message],
    signal: AbortContext,
    prompt: str = _SUMMARY_PROMPT,
    max_output_tokens: int = 4096,
) -> str:
    """Summarise *older* messages using *provider* and return the summary text.

    The provider's ``stream()`` already yields normalized wire events
    (``text_delta``, ``message_end``, …) so we consume them directly without
    wrapping through any provider-specific translator.
    """
    from .types import ProviderRequest

    req = ProviderRequest(
        model=model,
        system=[SystemBlock(text=prompt, cacheable=False)],
        tools=[],
        messages=older,
        max_output_tokens=max_output_tokens,
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


class DetailedCompaction:
    """Continuation-safe compaction with detailed technical handoff sections."""

    id = "detailed-continuation-keep-recent-10"

    def __init__(self, *, keep_recent_turns: int = 10, max_output_tokens: int = 8192) -> None:
        self.keep_recent_turns = keep_recent_turns
        self.max_output_tokens = max_output_tokens

    async def compact(self, ctx: CompactionContext, provider: Any) -> list[Message]:
        recent = last_n_turn_boundaries(ctx.messages, self.keep_recent_turns)
        boundary = len(ctx.messages) - len(recent)
        older = ctx.messages[:boundary] if boundary > 0 else []

        if not older:
            return ctx.messages

        summary_text = await summarize_with_provider(
            provider,
            ctx.model,
            older,
            ctx.signal,
            prompt=_DETAILED_SUMMARY_PROMPT,
            max_output_tokens=self.max_output_tokens,
        )

        summary_msg = Message(
            role="user",
            content=[
                TextBlock(text=(f"<detailed summary of earlier conversation>\n\n{summary_text}"))
            ],
        )

        return [summary_msg, *recent]


default_compaction = DefaultCompaction()


def _compaction_hook_dispatcher(agent: Any) -> Any:
    """A live :class:`HookDispatcher` for *agent* iff it has active hooks, else None.

    Built lazily (and imported lazily) so compaction stays zero-overhead and free
    of a hooks import when no hooks are configured — preserving byte-identical
    behavior for the no-hook path.
    """
    hooks = getattr(agent, "hooks", None)
    if not hooks:
        return None
    from .hooks import HookDispatcher

    dispatcher = HookDispatcher(hooks)
    return dispatcher if dispatcher.active else None


async def _run_compaction_impl(
    session: Any,
    agent: Any,
    signal: AbortContext,
    strategy: Any,
) -> None:
    messages_before = len(session.provider_view)
    tokens_before = _estimate_tokens(agent, session.provider_view)

    dispatcher = _compaction_hook_dispatcher(agent)
    run_id = getattr(session, "active_run_id", None) or "unknown"
    if dispatcher is not None:
        from .hooks import HookEvent, PreCompactContext

        await dispatcher.dispatch(
            HookEvent.PRE_COMPACT,
            PreCompactContext(
                session=session,
                run_id=run_id,
                turn_index=None,
                deps=getattr(session, "run_deps", None),
                messages=messages_before,
                tokens=tokens_before,
                strategy=strategy.id,
            ),
        )

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

    if dispatcher is not None:
        from .hooks import HookEvent, PostCompactContext

        info = session.last_compaction_info
        await dispatcher.dispatch(
            HookEvent.POST_COMPACT,
            PostCompactContext(
                session=session,
                run_id=run_id,
                turn_index=None,
                deps=getattr(session, "run_deps", None),
                messages_before=info["messages_before"],
                messages_after=info["messages_after"],
                tokens_before=info["tokens_before"],
                tokens_after=info["tokens_after"],
                strategy=strategy.id,
            ),
        )


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

    # Ladder rung 1 — micro-compact (no LLM call).  If eliding old tool
    # results brings the projection back under threshold, skip summarization
    # entirely; otherwise keep the savings and fall through.
    micro_info: dict[str, Any] | None = None
    ladder = getattr(agent, "compaction_ladder", None)
    if ladder is not None and ladder.micro:
        if apply_micro_compaction(session, agent, keep_recent_turns=ladder.keep_recent_turns):
            projected = _estimate_tokens(agent, session.provider_view) + reserve
            if projected < 0.8 * limit:
                return True
            micro_info = session.last_compaction_info

    length_before = len(session.provider_view)
    head_before = session.provider_view[0] if session.provider_view else None

    await _run_compaction_impl(session, agent, signal, strategy)

    if len(session.provider_view) == length_before:
        if session.provider_view and head_before is session.provider_view[0]:
            # Full strategy was a no-op; if micro elided something, that is
            # still a reportable compaction.
            if micro_info is not None:
                session.last_compaction_info = micro_info
                return True
            session.last_compaction_info = None
            return False

    return True


def _estimate_tokens(agent: Any, messages: list[Message]) -> int:
    estimator = getattr(agent, "token_estimator", None)
    if callable(estimator):
        try:
            value = estimator(messages, agent.model)
            return max(0, int(cast(Any, value)))
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
